"""The Claude Code CLI provider.

Two clusters matter most, and neither is about the happy path.

The **command line**, because this provider's safety properties are entirely
carried by argv and cwd. The prompt is untrusted email text, so it must never
be interpolated into an argument; the CLI is an agent with shell and file
access, so the tool grant must be closed rather than merely discouraged; and
without `--bare` the working directory decides whether this repository's own
CLAUDE.md gets loaded into an email-classification call. None of that is
visible from the outside once it works, so it is asserted here.

And the **failure mapping**, because it decides whether the pool fails over. A
timeout that raises RuntimeError escapes `ProviderPool.call` uncaught and takes
the stage down; a timeout that raises ProviderRateLimited moves the work to
Gemini. The two look identical until the day the CLI hangs.
"""

import json
import os
import subprocess
import unittest

import clients.providers.claude_cli as claude_cli
from clients.providers.base import (
    ProviderBudgetExhausted,
    ProviderNotConfigured,
    ProviderRateLimited,
)


class FakeCompleted:
    """What `subprocess.run` returns, reduced to what this module reads."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def envelope(result="{}", *, structured=None, tokens=(120, 340), cost=0.02,
             is_error=False, subtype="success", model="claude-sonnet-4-6"):
    """A result envelope shaped like `--output-format json` produces."""
    body = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": result,
        "session_id": "sess-1",
        "total_cost_usd": cost,
        "duration_ms": 8100,
        "num_turns": 1,
        "model": model,
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
    }
    if structured is not None:
        body["structured_output"] = structured
    return json.dumps(body)


class Recorder:
    """A fake runner that records the call and replays a scripted result."""

    def __init__(self, stdout="", stderr="", returncode=0, raises=None):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.raises = raises
        self.argv = None
        self.kwargs = None
        self.calls = 0

    def __call__(self, argv, **kwargs):
        self.calls += 1
        self.argv = list(argv)
        self.kwargs = kwargs
        if self.raises is not None:
            raise self.raises
        return FakeCompleted(self.stdout, self.stderr, self.returncode)

    def flag(self, name):
        """The value following `name`, or None when the flag is absent."""
        if name not in self.argv:
            return None
        index = self.argv.index(name)
        return self.argv[index + 1] if index + 1 < len(self.argv) else ""


class EnvIsolationMixin:
    """Keeps tests off the developer's real .env.

    Mirrors `tests/test_gemini_client.py`. `claude_cli.load_dotenv` is nulled
    the same way, which is why the module keeps a module-level handle on it.
    """

    CLI_VARS = (
        "CLAUDE_CLI_PATH", "CLAUDE_CLI_MODEL", "CLAUDE_CLI_WORKDIR",
        "CLAUDE_CLI_TIMEOUT", "CLAUDE_CLI_MAX_BUDGET_USD",
        "CLAUDE_CLI_REQUESTS_PER_DAY", "CLAUDE_CLI_BARE",
    )

    def isolate_env(self):
        self.saved_env = {name: os.environ.get(name) for name in self.CLI_VARS}
        for name in self.CLI_VARS:
            os.environ.pop(name, None)
        self.saved_loader = claude_cli.load_dotenv
        claude_cli.load_dotenv = None
        self.addCleanup(self.restore_env)

    def restore_env(self):
        for name, value in self.saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        claude_cli.load_dotenv = self.saved_loader


class ConfigTests(EnvIsolationMixin, unittest.TestCase):
    def setUp(self):
        self.isolate_env()

    def test_a_missing_binary_is_not_configured(self):
        os.environ["CLAUDE_CLI_PATH"] = os.path.join("no", "such", "claude")
        with self.assertRaises(ProviderNotConfigured):
            claude_cli.binary_path()
        self.assertFalse(claude_cli.is_configured())

    def test_the_daily_ceiling_defaults_to_none(self):
        """0 is meaningful here, not a bad value: no ceiling."""
        self.assertEqual(claude_cli.requests_per_day(), 0)
        os.environ["CLAUDE_CLI_REQUESTS_PER_DAY"] = "40"
        self.assertEqual(claude_cli.requests_per_day(), 40)
        os.environ["CLAUDE_CLI_REQUESTS_PER_DAY"] = "nonsense"
        self.assertEqual(claude_cli.requests_per_day(), 0)

    def test_the_workdir_is_never_this_repository(self):
        """The whole isolation story rests on this."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertNotEqual(os.path.abspath(claude_cli.workdir()), here)

    def test_retry_after_reads_the_largest_duration_mentioned(self):
        self.assertEqual(claude_cli.parse_retry_after("try again in 4 hours"), 14400)
        self.assertEqual(claude_cli.parse_retry_after("wait 30 seconds"), 30)
        self.assertEqual(
            claude_cli.parse_retry_after("nothing useful", default=99), 99)


class CommandLineTests(EnvIsolationMixin, unittest.TestCase):
    """What is on the command line, and - more importantly - what is not."""

    def setUp(self):
        self.isolate_env()

    def run_client(self, client, **kwargs):
        recorder = Recorder(stdout=envelope('{"label": "alert"}'))
        client.runner = recorder
        client.binary = "claude"
        return recorder

    def test_the_prompt_is_on_stdin_and_never_in_argv(self):
        """It is untrusted email. It does not go near a command line."""
        secret = "COMPANY-SPECIFIC-BODY-TEXT"
        client = claude_cli.ClaudeCliClient(binary="claude")
        recorder = self.run_client(client)
        client.complete_json(
            [{"role": "system", "content": "SYS"},
             {"role": "user", "content": secret}],
            lambda text: text, "fallback",
        )
        self.assertNotIn(secret, " ".join(recorder.argv))
        self.assertIn(secret, recorder.kwargs["input"])

    def permissions(self, recorder):
        """The allow/deny rules actually sent, which are the real gate."""
        return json.loads(recorder.flag("--settings"))["permissions"]

    def test_classification_is_given_no_tools_at_all(self):
        """An empty allow-list under dontAsk denies everything, including
        tools this CLI version has not shipped yet."""
        client = claude_cli.ClaudeCliClient(binary="claude")
        recorder = self.run_client(client)
        client.complete_json([{"role": "user", "content": "hi"}],
                             lambda text: text, "fallback")
        rules = self.permissions(recorder)
        self.assertEqual(rules["allow"], [])
        self.assertIsNone(recorder.flag("--allowed-tools"))
        for tool in ("Bash", "Edit", "Write", "SendMessage"):
            self.assertIn(tool, rules["deny"])

    def test_research_is_given_exactly_the_two_web_tools(self):
        client = claude_cli.ClaudeCliResearchClient(binary="claude")
        recorder = Recorder(stdout=envelope(
            structured={"company_summary": "ok"}))
        client.runner = recorder
        client.research(_lead())
        rules = self.permissions(recorder)
        self.assertEqual(rules["allow"], ["WebSearch", "WebFetch"])
        self.assertIn("Bash", rules["deny"])
        self.assertNotIn("WebSearch", rules["deny"])
        self.assertEqual(recorder.flag("--allowed-tools"), "WebSearch,WebFetch")

    def test_no_mcp_server_is_offered(self):
        """A real run had the model reach for a personal Indeed connector,
        including its `get_resume` tool. Local servers are excluded here; the
        account-level ones the allow-list refuses."""
        client = claude_cli.ClaudeCliClient(binary="claude")
        recorder = self.run_client(client)
        client.complete_json([{"role": "user", "content": "hi"}],
                             lambda text: text, "fallback")
        self.assertEqual(json.loads(recorder.flag("--mcp-config")),
                         {"mcpServers": {}})
        self.assertIn("--strict-mcp-config", recorder.argv)

    def test_the_task_instructions_reach_stdin_not_only_the_flag(self):
        """`--system-prompt` alone does not bind, which cost a day to find.

        Passed only as a flag, the classification prompt never reached the
        model: asked for {"label","confidence","reason"} it invented keys from
        the user turn. The system text goes on stdin as well, and that is the
        half that carries - so it is the half asserted here.
        """
        client = claude_cli.ClaudeCliClient(binary="claude")
        recorder = self.run_client(client)
        client.complete_json([{"role": "system", "content": "SYSTEM-RULES"},
                              {"role": "user", "content": "USER-TURN"}],
                             lambda text: text, "fallback")
        self.assertIn("SYSTEM-RULES", recorder.kwargs["input"])
        self.assertIn("USER-TURN", recorder.kwargs["input"])
        self.assertEqual(recorder.flag("--append-system-prompt"), "SYSTEM-RULES")

    def test_every_prompt_ends_with_the_json_only_instruction(self):
        """The CLI has no JSON mode; a trailing user instruction is the only
        thing measured to stop it answering in prose."""
        client = claude_cli.ClaudeCliClient(binary="claude")
        recorder = self.run_client(client)
        client.complete_json([{"role": "user", "content": "hi"}],
                             lambda text: text, "fallback")
        self.assertTrue(recorder.kwargs["input"].endswith(
            claude_cli.JSON_ONLY_TAIL))

    def test_a_prose_reply_still_yields_its_json(self):
        """Belt to the tail's braces: the agent narrates when it feels like it."""
        recorder = Recorder(stdout=envelope(
            'Here is the classification:\n```json\n{"label": "Rejected"}\n```\n'
            'Let me know if you want more.'))
        client = claude_cli.ClaudeCliClient(binary="claude", runner=recorder)
        result = client.complete_json([{"role": "user", "content": "hi"}],
                                      json.loads, "fallback")
        self.assertEqual(result, {"label": "Rejected"})

    def test_the_subprocess_does_not_run_in_this_repository(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        client = claude_cli.ClaudeCliClient(binary="claude")
        recorder = self.run_client(client)
        client.complete_json([{"role": "user", "content": "hi"}],
                             lambda text: text, "fallback")
        self.assertNotEqual(os.path.abspath(recorder.kwargs["cwd"]), here)

    def test_permissions_and_mcp_are_closed(self):
        client = claude_cli.ClaudeCliClient(binary="claude")
        recorder = self.run_client(client)
        client.complete_json([{"role": "user", "content": "hi"}],
                             lambda text: text, "fallback")
        self.assertEqual(recorder.flag("--permission-mode"), "dontAsk")
        self.assertIn("--strict-mcp-config", recorder.argv)
        self.assertIn("--no-session-persistence", recorder.argv)

    def test_bare_mode_is_off_unless_asked_for(self):
        client = claude_cli.ClaudeCliClient(binary="claude")
        recorder = self.run_client(client)
        client.complete_json([{"role": "user", "content": "hi"}],
                             lambda text: text, "fallback")
        self.assertNotIn("--bare", recorder.argv)

        os.environ["CLAUDE_CLI_BARE"] = "1"
        client.complete_json([{"role": "user", "content": "hi"}],
                             lambda text: text, "fallback")
        self.assertIn("--bare", recorder.argv)


class CompletionTests(EnvIsolationMixin, unittest.TestCase):
    def setUp(self):
        self.isolate_env()

    def test_a_reply_reaches_the_parser_and_tokens_are_recorded(self):
        recorder = Recorder(stdout=envelope('{"label": "alert"}', tokens=(120, 340)))
        client = claude_cli.ClaudeCliClient(binary="claude", runner=recorder)
        result = client.complete_json([{"role": "user", "content": "hi"}],
                                      json.loads, "fallback")
        self.assertEqual(result, {"label": "alert"})
        self.assertEqual(client.last_total_tokens, 460)
        self.assertEqual(client.model, "claude-sonnet-4-6")
        self.assertEqual(client.last_model, "claude-sonnet-4-6")

    def test_an_empty_reply_degrades_to_the_fallback(self):
        recorder = Recorder(stdout=envelope(""))
        client = claude_cli.ClaudeCliClient(binary="claude", runner=recorder)
        self.assertEqual(
            client.complete_json([{"role": "user", "content": "hi"}],
                                 json.loads, "fallback"),
            "fallback",
        )


class ResearchTests(EnvIsolationMixin, unittest.TestCase):
    def setUp(self):
        self.isolate_env()

    def test_structured_output_is_preferred_over_the_text_result(self):
        """The schema is the guarantee; the text field is the fallback."""
        recorder = Recorder(stdout=envelope(
            "prose the model added anyway",
            structured={"company_summary": "From the schema",
                        "posting_keywords": ["python"]},
        ))
        client = claude_cli.ClaudeCliResearchClient(binary="claude",
                                                    runner=recorder)
        payload, input_tokens, output_tokens = client.research(_lead())
        self.assertEqual(payload["company_summary"], "From the schema")
        self.assertEqual(payload["posting_keywords"], ["python"])
        self.assertEqual((input_tokens, output_tokens), (120, 340))

    def test_the_schema_is_sent_and_matches_what_the_parser_validates(self):
        from clients.research_client import parse_research

        recorder = Recorder(stdout=envelope(
            structured={"company_summary": "non-empty, so it does not fail over"}))
        client = claude_cli.ClaudeCliResearchClient(binary="claude",
                                                    runner=recorder)
        client.research(_lead())
        schema = json.loads(recorder.flag("--json-schema"))
        self.assertEqual(sorted(schema["properties"]),
                         sorted(parse_research("{}")))

    def test_an_all_empty_payload_fails_over_instead_of_being_cached(self):
        """A blocked run produces a well-formed husk, which is not a success.

        Observed for real: the CLI's web tools were refused in headless mode
        and RESEARCH_SYSTEM_PROMPT correctly told the model to leave fields
        blank rather than invent them, so a schema-valid object came back with
        nothing in it. generate.py caches whatever it is given against the
        lead's identity key, so returning that would persist emptiness and
        never retry.
        """
        recorder = Recorder(stdout=envelope(structured={
            "company_summary": "", "posting_keywords": [], "mission": "",
        }))
        client = claude_cli.ClaudeCliResearchClient(binary="claude",
                                                    runner=recorder)
        with self.assertRaises(ProviderRateLimited):
            client.research(_lead())

    def test_the_refused_tools_are_named_in_the_failure(self):
        body = json.loads(envelope(structured={"company_summary": ""}))
        body["permission_denials"] = [{"tool_name": "WebSearch"},
                                      {"tool_name": "WebFetch"}]
        client = claude_cli.ClaudeCliResearchClient(
            binary="claude", runner=Recorder(stdout=json.dumps(body)))
        with self.assertRaises(ProviderRateLimited) as caught:
            client.research(_lead())
        self.assertIn("WebFetch", str(caught.exception))
        self.assertIn("WebSearch", str(caught.exception))

    def test_a_partial_payload_is_still_a_success(self):
        """One real field beats failing over; research is best-effort."""
        recorder = Recorder(stdout=envelope(
            structured={"company_summary": "A payments company.",
                        "posting_keywords": []}))
        client = claude_cli.ClaudeCliResearchClient(binary="claude",
                                                    runner=recorder)
        payload, _in, _out = client.research(_lead())
        self.assertEqual(payload["company_summary"], "A payments company.")

    def test_a_text_only_reply_still_parses(self):
        recorder = Recorder(stdout=envelope(
            '```json\n{"company_summary": "From text"}\n```'))
        client = claude_cli.ClaudeCliResearchClient(binary="claude",
                                                    runner=recorder)
        payload, _in, _out = client.research(_lead())
        self.assertEqual(payload["company_summary"], "From text")


class ThreadSafetyTests(EnvIsolationMixin, unittest.TestCase):
    """Clients run on executor threads. Whatever they touch must survive that.

    The pipeline calls every model client through `asyncio.to_thread`, so a
    client that reads sqlite through a connection made on the main thread
    raises `ProgrammingError` on every call - and the generic handler in
    `pipeline/prepare.py` turns that into a traceback per lead rather than
    anything naming the cause.
    """

    def setUp(self):
        self.isolate_env()

    def test_a_call_from_a_worker_thread_touches_no_database(self):
        import threading

        from utilities.mailstore import MailStore
        from utilities.store import JobStore

        store = JobStore(":memory:")
        mail = MailStore(store.conn)
        self.addCleanup(store.conn.close)

        recorder = Recorder(stdout=envelope('{"label": "alert"}'))
        client = claude_cli.ClaudeCliClient(binary="claude", runner=recorder)
        # Whatever the builder chose to attach, exercise it off the main
        # thread the way the pipeline does.
        outcome = {}

        def worker():
            try:
                outcome["value"] = client.complete_json(
                    [{"role": "user", "content": "hi"}], json.loads, "fallback")
            except Exception as exc:  # noqa: BLE001 - recorded, then asserted
                outcome["error"] = exc

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        self.assertNotIn("error", outcome,
                         f"call failed off the main thread: {outcome.get('error')}")
        self.assertEqual(outcome["value"], {"label": "alert"})
        self.assertIsNotNone(mail)


class FailureMappingTests(EnvIsolationMixin, unittest.TestCase):
    """Which exception comes out decides whether the pool fails over."""

    def setUp(self):
        self.isolate_env()

    def client(self, **kwargs):
        return claude_cli.ClaudeCliClient(binary="claude",
                                          runner=Recorder(**kwargs))

    def call(self, client):
        return client.complete_json([{"role": "user", "content": "hi"}],
                                    json.loads, "fallback")

    def test_a_timeout_is_a_rate_limit_and_not_a_runtime_error(self):
        """A RuntimeError escapes pool.call uncaught and kills the stage."""
        client = self.client(
            raises=subprocess.TimeoutExpired(cmd="claude", timeout=90))
        with self.assertRaises(ProviderRateLimited) as caught:
            self.call(client)
        self.assertGreater(caught.exception.retry_after, 0)

    def test_a_usage_limit_carries_the_wait_it_named(self):
        client = self.client(stdout=envelope(
            "Usage limit reached. Try again in 3 hours.",
            is_error=True, subtype="error_during_execution"))
        with self.assertRaises(ProviderRateLimited) as caught:
            self.call(client)
        self.assertEqual(caught.exception.retry_after, 10800)
        # Minute scope on purpose: a five-hour window is not a day, and "day"
        # would close the whole daily budget on the way past.
        self.assertEqual(caught.exception.scope, "minute")

    def test_a_spend_ceiling_is_its_own_exception(self):
        """prepare.py distinguishes it from a rate limit, so the pool must."""
        client = self.client(stdout=envelope(
            "Exceeded max budget of $0.50 for this run.",
            is_error=True, subtype="error_max_budget"))
        with self.assertRaises(ProviderBudgetExhausted):
            self.call(client)

    def test_an_auth_failure_does_not_permanently_unconfigure_the_provider(self):
        """ProviderNotConfigured deletes the client until the process restarts."""
        client = self.client(stdout=envelope(
            "Not logged in. Run `claude` to authenticate.",
            is_error=True, subtype="error"))
        with self.assertRaises(ProviderRateLimited):
            self.call(client)

    def test_unparseable_output_fails_over_rather_than_crashing(self):
        client = self.client(stdout="not json at all", returncode=0)
        with self.assertRaises(ProviderRateLimited):
            self.call(client)

    def test_a_non_zero_exit_with_no_output_fails_over(self):
        client = self.client(stdout="", stderr="boom", returncode=2)
        with self.assertRaises(ProviderRateLimited):
            self.call(client)

    def test_a_vanished_binary_is_a_configuration_problem(self):
        client = self.client(raises=OSError("No such file"))
        with self.assertRaises(ProviderNotConfigured):
            self.call(client)


def _lead():
    return {
        "title": "Backend Engineer",
        "company": "Stripe",
        "location": "Remote",
        "apply_url": "https://example.com/job/1",
        "identity_key": "KEY",
    }


if __name__ == "__main__":
    unittest.main()
