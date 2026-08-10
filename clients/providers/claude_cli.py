"""The locally installed Claude Code CLI, driven headlessly as a provider.

The other three providers are HTTP clients. This one is a subprocess: it runs
`claude -p` with the prompt on stdin and reads a JSON envelope back off stdout.
The pattern is well established outside this repo - several projects wrap the
CLI and re-expose it over HTTP - but the HTTP half is dropped here. The pool is
already an in-process abstraction, so a server in front of the binary would be
a networked service bought for nothing.

Two things make it worth having rather than merely cheap:

- **Research is grounded.** `--allowed-tools WebSearch,WebFetch` gives the model
  live search and page fetching, which is the same capability
  `clients/gemini_research.py` buys with Google Search grounding.
- **Research output is schema-checked.** `--json-schema` returns a validated
  object in `structured_output`, so the research reply cannot come back in a
  shape `parse_research` has to rescue.

Three things it is not:

- **Fast.** `claude -p` is an agent loop, not a completion. Tens of seconds is
  normal where Groq answers in under a second, which is why nothing routes here
  by default and why `research` is the sensible task to point at it: few calls
  per cycle, each worth the wait.
- **Isolated for free.** See `workdir` below.
- **Trusted with tools.** See `DENIED_TOOLS`.

On authentication: run without `--bare`, the CLI uses the signed-in
subscription. Anthropic's documentation asks developers building on the Agent
SDK to use API-key authentication instead, and states that subscription limits
assume ordinary individual use. That is the operator's decision to make and
`CLAUDE_CLI_BARE=1` switches to API-key mode; it is recorded here because it is
the reason for the isolation work below rather than as advice.
"""

import json
import logging
import os
import re
import shutil
import subprocess

from clients.providers.base import (
    ProviderBudgetExhausted,
    ProviderNotConfigured,
    ProviderRateLimited,
)

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised only without the deps
    load_dotenv = None

log = logging.getLogger(__name__)

DISPLAY_NAME = "Claude Code"

#: The binary looked for on PATH when `CLAUDE_CLI_PATH` is unset.
BINARY_NAME = "claude"

#: Left to the CLI's own default when unset, so this module does not pin a
#: model the installed CLI may not offer. `CLAUDE_CLI_MODEL` overrides.
DEFAULT_MODEL = ""

#: What `.model` reports before a call has named anything better. The pool
#: writes this into `provider_usage.model`, and "the CLI's default" is a more
#: honest answer there than a model id this module guessed.
UNKNOWN_MODEL = "claude-code-cli"

#: One agent turn for classification: a single structured answer, no tool use,
#: nothing to iterate on. Research gets more because searching, fetching a
#: posting and synthesising is genuinely multi-turn.
DEFAULT_MAX_TURNS = 1
RESEARCH_MAX_TURNS = 6

#: Wall-clock ceilings. Cycles cannot overlap - the scheduler awaits
#: `run_once` - but a hung call still holds an executor thread for its whole
#: duration, so neither of these is optional.
DEFAULT_TIMEOUT = 90.0
RESEARCH_TIMEOUT = 240.0

#: How long to cool the provider when it refuses and says nothing useful about
#: when to come back. A Pro/Max limit is a rolling five-hour window, so a
#: pessimistic default costs one cycle and an optimistic one costs a hot loop.
DEFAULT_LIMIT_COOLDOWN = 900.0

#: A crash, a timeout, or output that would not parse. Short, because the cause
#: is usually transient and the chain has already moved on by then.
TRANSIENT_COOLDOWN = 60.0

#: Tools the research call may use. Nothing else searches the web, and nothing
#: here can write.
RESEARCH_TOOLS = ("WebSearch", "WebFetch")

#: The denylist floor. `--permission-mode dontAsk` is the real gate - it refuses
#: anything not explicitly allowed - but that leaves a read-only command set
#: still reachable, and this pipeline feeds the model untrusted third-party
#: email. Naming the tools costs one constant and closes the capability rather
#: than arguing with the prompt about it.
DENIED_TOOLS = (
    "Bash", "BashOutput", "KillShell", "Read", "Edit", "Write", "NotebookEdit",
    "Glob", "Grep", "Task", "Agent", "TodoWrite", "SlashCommand", "WebSearch",
    "WebFetch",
)

MISSING_BINARY_HINT = (
    "The Claude Code CLI was not found. Install it and sign in with `claude`, "
    "or set CLAUDE_CLI_PATH in .env to the binary's full path."
)


# Configuration ------------------------------------------------------------


def _load_env():
    """Load `.env` if python-dotenv is installed.

    Summary:
        Populate the environment from the project's .env file.

    Note:
        Guarded on the module-level `load_dotenv` name rather than importing
        locally, because the tests null that name out to keep a developer's
        real .env from leaking into a run. Three `dirname` calls, not two:
        this module sits one directory deeper than `clients/llm_client.py`.
    """
    if load_dotenv:
        env_path = os.path.join(
            os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            ),
            ".env",
        )
        load_dotenv(dotenv_path=env_path)


def binary_path():
    """Resolve the CLI binary. The analogue of the other providers' `api_key`.

    Summary:
        Return the path to the Claude Code executable.

    Returns:
        str: An absolute path, or a bare name PATH will resolve.

    Raises:
        ProviderNotConfigured: When no binary is configured or discoverable.
            There is no key to store, so "not installed" is this provider's
            entire unconfigured state.
    """
    _load_env()
    configured = (os.environ.get("CLAUDE_CLI_PATH") or "").strip()
    if configured:
        if os.path.isfile(configured) or shutil.which(configured):
            return configured
        raise ProviderNotConfigured(
            f"CLAUDE_CLI_PATH points at {configured}, which is not executable."
        )
    found = shutil.which(BINARY_NAME)
    if not found:
        raise ProviderNotConfigured(MISSING_BINARY_HINT)
    return found


def model_name():
    """
    Summary:
        The model to pass to `--model`, if one is configured.

    Returns:
        str: A model id or alias, or "" to accept the CLI's own default.
    """
    _load_env()
    return (os.environ.get("CLAUDE_CLI_MODEL") or "").strip() or DEFAULT_MODEL


def workdir():
    """The directory the CLI runs in, which is a safety control, not a detail.

    Without `--bare` the CLI loads CLAUDE.md, hooks, MCP servers and auto
    memory from wherever it is started. Started in this repo, every email
    classification would quietly inherit `.claude/CLAUDE.md` and the project's
    MCP configuration as context. So it is never started in this repo.

    Summary:
        Return a neutral, empty working directory for the subprocess.

    Returns:
        str: The directory path, created if it does not exist. Falls back to
            the user's home directory when it cannot be created, which is
            still not this repository.
    """
    _load_env()
    configured = (os.environ.get("CLAUDE_CLI_WORKDIR") or "").strip()
    target = configured or os.path.join(
        os.path.expanduser("~"), ".job_builder", "claude_cli"
    )
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        log.warning("Could not create %s (%s); running the CLI from home "
                    "instead", target, exc)
        return os.path.expanduser("~")
    return target


def _positive_number(raw, default, cast):
    """
    Summary:
        Cast an environment value, falling back on anything unusable.

    Parameters:
        raw (str | None): The raw environment value.
        default: What to return when `raw` is absent, unparseable, or <= 0.
        cast (Callable): `int` or `float`.

    Returns:
        The cast value when it is positive, otherwise `default`.
    """
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def timeout_seconds(default=DEFAULT_TIMEOUT):
    """
    Summary:
        The wall-clock ceiling for one invocation.

    Parameters:
        default (float): Used when `CLAUDE_CLI_TIMEOUT` is unset or unusable.

    Returns:
        float: Seconds.
    """
    _load_env()
    return _positive_number(os.environ.get("CLAUDE_CLI_TIMEOUT"), default, float)


def max_budget_usd():
    """
    Summary:
        The per-call spend cap handed to `--max-budget-usd`.

    Returns:
        float: Dollars, or 0.0 to pass no cap at all.

    Note:
        Reported by the CLI as a client-side estimate even on a subscription,
        where no per-call charge exists. It is still worth setting: it bounds a
        prompt that sends the agent loop somewhere expensive.
    """
    _load_env()
    return _positive_number(os.environ.get("CLAUDE_CLI_MAX_BUDGET_USD"), 0.0, float)


def requests_per_day():
    """
    Summary:
        The configured daily request ceiling.

    Returns:
        int: Requests per day. 0, the default, disables the ceiling - which is
            why this does not go through `_positive_number`, where 0 would be
            treated as a bad value rather than a meaningful one.
    """
    _load_env()
    raw = os.environ.get("CLAUDE_CLI_REQUESTS_PER_DAY")
    if raw is None or not str(raw).strip():
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if value >= 0 else 0


def use_bare():
    """
    Summary:
        Whether to pass `--bare`, which switches the CLI to API-key auth.

    Returns:
        bool: True when `CLAUDE_CLI_BARE` is set to a truthy value.

    Note:
        `--bare` skips CLAUDE.md, hooks, plugins and MCP discovery, so it makes
        the isolation below redundant - but it also stops the CLI reading the
        subscription login, and needs ANTHROPIC_API_KEY instead.
    """
    _load_env()
    return (os.environ.get("CLAUDE_CLI_BARE") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def is_configured():
    """
    Summary:
        Whether the CLI can be run at all.

    Returns:
        bool: True when a binary resolves.
    """
    try:
        binary_path()
    except ProviderNotConfigured:
        return False
    return True


# Invocation ---------------------------------------------------------------


#: Patterns that mean "come back later" rather than "this call was bad".
_LIMIT_PATTERN = re.compile(
    r"rate limit|usage limit|limit reached|quota|too many requests", re.I
)
_AUTH_PATTERN = re.compile(
    r"not logged in|log in|unauthorized|authentication|invalid api key|"
    r"credit balance", re.I
)
_BUDGET_PATTERN = re.compile(r"budget", re.I)
_DURATION_PATTERN = re.compile(r"(\d+)\s*(second|minute|hour)s?", re.I)
_SECONDS_PER = {"second": 1, "minute": 60, "hour": 3600}


def parse_retry_after(text, default=DEFAULT_LIMIT_COOLDOWN):
    """Read a wait out of a refusal message.

    Summary:
        Find how long the CLI said to wait, falling back on a default.

    Parameters:
        text (str): The CLI's message.
        default (float): Used when nothing parses.

    Returns:
        float: Seconds. The largest duration mentioned, because a message
            naming both a window and a reset names the reset second.
    """
    found = [
        int(value) * _SECONDS_PER[unit.lower()]
        for value, unit in _DURATION_PATTERN.findall(text or "")
    ]
    return float(max(found)) if found else float(default)


def build_argv(binary, *, system_prompt, model, tools, schema, max_turns,
               budget_usd, bare):
    """Assemble the command line for one headless run.

    Split out from `run_cli` so the tests can assert on it directly. What is
    absent matters as much as what is present: the prompt is never here, it
    goes on stdin.

    Summary:
        Build the argument vector for a `claude -p` invocation.

    Parameters:
        binary (str): Path to the executable.
        system_prompt (str): Replaces the CLI's own system prompt entirely.
        model (str): Model id or alias; skipped when empty.
        tools (Sequence[str]): Tools to allow. Everything else is denied.
        schema (dict | None): A JSON Schema for `--json-schema`.
        max_turns (int): Agent-turn ceiling.
        budget_usd (float): Per-call spend cap; skipped when 0.
        bare (bool): Whether to pass `--bare`.

    Returns:
        list[str]: The argument vector.
    """
    argv = [binary, "-p", "--output-format", "json"]
    if bare:
        argv.append("--bare")
    if model:
        argv += ["--model", model]
    # Replace rather than append: an email classifier should not also be
    # carrying Claude Code's instructions about being a coding agent.
    argv += [
        "--system-prompt", system_prompt,
        "--permission-mode", "dontAsk",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--max-turns", str(max_turns),
    ]
    if budget_usd:
        argv += ["--max-budget-usd", str(budget_usd)]
    if schema is not None:
        argv += ["--json-schema", json.dumps(schema)]
    if tools:
        argv += ["--allowed-tools", ",".join(tools)]
    denied = [name for name in DENIED_TOOLS if name not in set(tools)]
    if denied:
        argv += ["--disallowed-tools", ",".join(denied)]
    return argv


def _refuse(message, text):
    """Turn a failed run into the exception the pool knows how to act on.

    The mapping is the whole point of this function, and every branch is a
    deliberate choice about failover:

    - A spend ceiling is `ProviderBudgetExhausted`, which `pipeline/prepare.py`
      already treats as "stop the stage" rather than "this lead failed".
    - Everything else is `ProviderRateLimited`, including crashes and timeouts.
      That looks odd until you notice that any *other* exception leaves
      `ProviderPool.call` without being caught, taking the whole stage down
      with no failover. A hung subprocess should move the work to Gemini.
    - An auth failure is emphatically not `ProviderNotConfigured`: the pool
      responds to that by deleting the client for the rest of the process,
      which is far too final for an expired token.

    Summary:
        Raise the provider exception matching a failed CLI run.

    Parameters:
        message (str): Prefix for the raised message.
        text (str): Whatever the CLI said.

    Raises:
        ProviderBudgetExhausted: When a spend ceiling stopped the run.
        ProviderRateLimited: In every other case.
    """
    detail = (text or "").strip()[:300]
    if _BUDGET_PATTERN.search(detail) and not _LIMIT_PATTERN.search(detail):
        raise ProviderBudgetExhausted(f"{message}: {detail}")
    if _LIMIT_PATTERN.search(detail):
        retry_after = parse_retry_after(detail)
        log.warning("Claude Code CLI refused the call (retry in about %ss): %s",
                    int(retry_after), detail)
        raise ProviderRateLimited(
            f"{message}: {detail}",
            retry_after=int(retry_after),
            provider=DISPLAY_NAME,
            # Minute scope on purpose. "day" closes the whole daily budget,
            # and a five-hour rolling window is not a day - the cooldown from
            # retry_after already holds it off for exactly as long as it said.
            scope="minute",
        )
    if _AUTH_PATTERN.search(detail):
        log.warning("Claude Code CLI is not authenticated: %s", detail)
        raise ProviderRateLimited(
            f"{message}: {detail}. Run `claude` once to sign in.",
            retry_after=int(DEFAULT_LIMIT_COOLDOWN),
            provider=DISPLAY_NAME,
            scope="minute",
        )
    log.warning("Claude Code CLI call failed: %s", detail)
    raise ProviderRateLimited(
        f"{message}: {detail}",
        retry_after=int(TRANSIENT_COOLDOWN),
        provider=DISPLAY_NAME,
        scope="minute",
    )


def run_cli(system_prompt, prompt, *, tools=(), schema=None,
            max_turns=DEFAULT_MAX_TURNS, timeout=None, model=None,
            budget_usd=None, runner=None, binary=None):
    """Run one headless invocation and return its parsed envelope.

    Summary:
        Execute `claude -p` with the prompt on stdin and decode the JSON it
        prints.

    Parameters:
        system_prompt (str): Replaces the CLI's system prompt.
        prompt (str): The user prompt. Passed on **stdin**, never in argv -
            it carries untrusted email text, and stdin has neither a length
            limit worth worrying about nor any quoting to get wrong.
        tools (Sequence[str]): Tools to allow; everything else is denied.
        schema (dict | None): JSON Schema for `--json-schema`.
        max_turns (int): Agent-turn ceiling.
        timeout (float | None): Wall-clock ceiling. Defaults to
            `timeout_seconds()`.
        model (str | None): Model override. Defaults to `model_name()`.
        budget_usd (float | None): Spend cap. Defaults to `max_budget_usd()`.
        runner (Callable | None): Defaults to `subprocess.run`. Injectable so
            the tests never spawn anything, the same reason the HTTP clients
            take a `poster`.
        binary (str | None): Executable path. Defaults to `binary_path()`.

    Returns:
        dict: The decoded result envelope, with `result`, `structured_output`,
            `usage`, `total_cost_usd` and friends.

    Raises:
        ProviderNotConfigured: When no binary resolves.
        ProviderBudgetExhausted: When a spend ceiling stopped the run.
        ProviderRateLimited: On a refusal, a crash, a timeout, or output that
            could not be decoded.
    """
    run = runner or subprocess.run
    argv = build_argv(
        binary or binary_path(),
        system_prompt=system_prompt,
        model=model if model is not None else model_name(),
        tools=tuple(tools),
        schema=schema,
        max_turns=max_turns,
        budget_usd=(budget_usd if budget_usd is not None else max_budget_usd()),
        bare=use_bare(),
    )

    try:
        completed = run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=(timeout if timeout is not None else timeout_seconds()),
            cwd=workdir(),
        )
    except subprocess.TimeoutExpired:
        _refuse("Claude Code CLI timed out", "the call exceeded its timeout")
    except OSError as exc:
        # The binary resolved a moment ago and is gone or unrunnable now.
        raise ProviderNotConfigured(f"Could not run the Claude Code CLI: {exc}")

    stdout = (getattr(completed, "stdout", "") or "").strip()
    stderr = (getattr(completed, "stderr", "") or "").strip()
    if getattr(completed, "returncode", 0) != 0 and not stdout:
        _refuse("Claude Code CLI exited non-zero", stderr or "no output")

    try:
        envelope = json.loads(stdout)
    except (TypeError, ValueError):
        _refuse("Claude Code CLI returned no usable JSON", stderr or stdout)

    if not isinstance(envelope, dict):
        _refuse("Claude Code CLI returned no usable JSON", stdout)
    if envelope.get("is_error") or envelope.get("subtype") not in (None, "success"):
        _refuse("Claude Code CLI reported a failure",
                str(envelope.get("result") or envelope.get("subtype") or stderr))
    return envelope


def envelope_tokens(envelope):
    """
    Summary:
        Total tokens the CLI reported for a call.

    Parameters:
        envelope (dict): A decoded result envelope.

    Returns:
        int: Input plus output tokens, or 0 when the CLI reported neither.
    """
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in ("input_tokens", "output_tokens"):
        try:
            total += int(usage.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


def envelope_model(envelope):
    """
    Summary:
        The model the CLI said served a call.

    Parameters:
        envelope (dict): A decoded result envelope.

    Returns:
        str: A model id, or `UNKNOWN_MODEL` when the envelope does not name one.
    """
    named = envelope.get("model")
    if isinstance(named, str) and named.strip():
        return named.strip()
    usage = envelope.get("modelUsage") or envelope.get("model_usage")
    if isinstance(usage, dict) and usage:
        return next(iter(usage))
    return UNKNOWN_MODEL


def flatten_messages(messages):
    """Turn an OpenAI-style message list into a system and a user prompt.

    Summary:
        Split chat messages into the two strings the CLI takes.

    Parameters:
        messages (Sequence[Mapping]): `{"role", "content"}` entries.

    Returns:
        tuple[str, str]: The joined system content, and the joined remainder
            with each non-system turn labelled so a multi-turn prompt does not
            collapse into one undifferentiated block.
    """
    system, rest = [], []
    for message in messages or ():
        role = (message.get("role") or "user").strip().lower()
        content = message.get("content") or ""
        if role == "system":
            system.append(content)
        elif role == "assistant":
            rest.append(f"Assistant: {content}")
        else:
            rest.append(content)
    return "\n\n".join(system).strip(), "\n\n".join(rest).strip()


def research_schema():
    """The shape `parse_research` validates, restated for `--json-schema`.

    Summary:
        Build the JSON Schema handed to the CLI for a research call.

    Returns:
        dict: A schema whose keys match `clients/research_client.parse_research`.

    Note:
        The reply still goes through `parse_research` afterwards. The schema
        guarantees the shape; the parser clamps the lengths and list sizes,
        and those are different jobs.
    """
    text_fields = ("company_summary", "mission", "tailoring_advice")
    list_fields = (
        "products", "tech_stack", "recent_news", "responsibilities",
        "requirements", "nice_to_haves", "posting_keywords", "culture_notes",
    )
    properties = {name: {"type": "string"} for name in text_fields}
    properties.update({
        name: {"type": "array", "items": {"type": "string"}}
        for name in list_fields
    })
    return {
        "type": "object",
        "properties": properties,
        "required": list(text_fields) + list(list_fields),
    }


# Clients ------------------------------------------------------------------


class _CliClient:
    """What the two shapes share: configuration, attribution, and one call."""

    def __init__(self, model=None, runner=None, binary=None, limiter=None,
                 timeout=None):
        #: Reported to the pool after each call, which writes it to
        #: `provider_usage.model`. Starts as whatever was configured.
        self.model = model or model_name() or UNKNOWN_MODEL
        self.limiter = limiter
        self.runner = runner
        self.binary = binary
        self.timeout = timeout
        #: Total tokens the last call reported, for the pool to reconcile.
        self.last_total_tokens = 0
        #: The last call's cost estimate, in dollars.
        self.last_cost_usd = 0.0

    @property
    def last_model(self):
        """
        Summary:
            Name the model behind the most recent call.

        Returns:
            str: The model the CLI reported, or the configured one before any
                call has been made.
        """
        return self.model

    def _call(self, system_prompt, prompt, **kwargs):
        """
        Summary:
            Run one invocation and record what it cost.

        Parameters:
            system_prompt (str): Replaces the CLI's system prompt.
            prompt (str): The user prompt, sent on stdin.
            **kwargs: Forwarded to `run_cli`.

        Returns:
            dict: The decoded envelope.

        Raises:
            ProviderBudgetExhausted: From the limiter, or from a spend ceiling.
            ProviderRateLimited: On a refusal, crash, or timeout.
        """
        if self.limiter is not None:
            self.limiter.check()
        envelope = run_cli(
            system_prompt, prompt,
            runner=self.runner, binary=self.binary,
            timeout=self.timeout, **kwargs,
        )
        self.last_total_tokens = envelope_tokens(envelope)
        self.model = envelope_model(envelope)
        try:
            self.last_cost_usd = float(envelope.get("total_cost_usd") or 0.0)
        except (TypeError, ValueError):
            self.last_cost_usd = 0.0
        return envelope


class ClaudeCliClient(_CliClient):
    """The JSON shape, matching `GroqClient.complete_json` exactly.

    No tools are allowed here at all. The messages carry untrusted email, and a
    classification call has nothing to do that a tool would help with.
    """

    @classmethod
    def from_config(cls, limiter=None):
        """
        Summary:
            Build a classification client from the environment.

        Parameters:
            limiter: An optional spend limiter, checked before each call.

        Returns:
            ClaudeCliClient: A configured client.

        Raises:
            ProviderNotConfigured: When no CLI binary resolves.
        """
        return cls(model=model_name(), binary=binary_path(), limiter=limiter)

    def complete_json(self, messages, parser, fallback, max_tokens=200):
        """
        Summary:
            Send one completion through the CLI and validate the reply.

        Parameters:
            messages (list[dict]): The chat messages to send.
            parser (Callable[[str], Any]): Validates the model's reply text.
            fallback (Any): Returned when the model produced nothing usable.
            max_tokens (int): Accepted for interface parity. The CLI has no
                output-token flag; `--max-turns` and `--max-budget-usd` are
                what bound a run here.

        Returns:
            Any: Whatever `parser` returns, or `fallback`.

        Raises:
            ProviderBudgetExhausted: When a spend ceiling stopped the run.
            ProviderRateLimited: On a refusal, crash, or timeout.
        """
        system_prompt, prompt = flatten_messages(messages)
        envelope = self._call(system_prompt, prompt, max_turns=DEFAULT_MAX_TURNS)
        text = envelope.get("result")
        if not isinstance(text, str) or not text.strip():
            return fallback
        return parser(text)


class ClaudeCliResearchClient(_CliClient):
    """The research shape, grounded in the CLI's own web tools.

    The prompt, the prompt builder and the validator all come from
    `clients/research_client.py` rather than being restated, for the reason
    `clients/gemini_research.py` gives: there is one description of what
    research means, and three providers cannot drift away from it.
    """

    @classmethod
    def from_config(cls, limiter=None):
        """
        Summary:
            Build a research client from the environment.

        Parameters:
            limiter: An optional spend limiter, checked before each call.

        Returns:
            ClaudeCliResearchClient: A configured client.

        Raises:
            ProviderNotConfigured: When no CLI binary resolves.
        """
        return cls(model=model_name(), binary=binary_path(), limiter=limiter,
                   timeout=timeout_seconds(RESEARCH_TIMEOUT))

    def research(self, lead):
        """
        Summary:
            Research one company and role, grounded in live web search.

        Parameters:
            lead (Mapping): The lead to research. Needs `title`, `company`,
                `location` and `apply_url`.

        Returns:
            tuple: `(payload, input_tokens, output_tokens)`, where payload is
                the validated `parse_research` shape.

        Raises:
            ProviderBudgetExhausted: When a spend ceiling stopped the run.
            ProviderRateLimited: On a refusal, crash, or timeout.
        """
        from clients.research_client import (
            RESEARCH_SYSTEM_PROMPT,
            build_research_prompt,
            parse_research,
        )

        envelope = self._call(
            RESEARCH_SYSTEM_PROMPT,
            build_research_prompt(lead),
            tools=RESEARCH_TOOLS,
            schema=research_schema(),
            max_turns=RESEARCH_MAX_TURNS,
        )

        structured = envelope.get("structured_output")
        if isinstance(structured, dict):
            # Already schema-valid; parse_research is still what clamps the
            # field lengths and list sizes, so it runs either way.
            payload = parse_research(json.dumps(structured))
        else:
            payload = parse_research(envelope.get("result") or "")

        usage = envelope.get("usage")
        usage = usage if isinstance(usage, dict) else {}

        def _count(key):
            try:
                return int(usage.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        return payload, _count("input_tokens"), _count("output_tokens")
