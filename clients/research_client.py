"""Company and role research via Claude, with server-side web search.

The second model in the system, and the split of work between the two is the
whole point:

- **Groq** (`llm_client.py`) does the high-volume, low-stakes work - one label
  from a fixed set, thousands of times, on the free tier.
- **Claude Opus 5** does the low-volume, high-value work - research a company
  and shape a resume, tens of times, where quality is what you are paying for.

Web search runs server-side (`web_search_20260209`), so there is no separate
search API key to manage on a box that has to stay up unattended. Web fetch is
declared alongside it to read the posting itself; it only retrieves URLs
already present in the conversation, so passing the apply URL in the prompt is
what authorises it.

Cost control is not optional here. At roughly $5/$25 per million tokens, an
unbounded loop over a daily digest costs more per month than the server. Two
guards: the relevance gate upstream (`pipeline/relevance.py`) decides *whether*
to call at all, and `SpendLimiter` below caps how much can be spent in a day
regardless. A parser bug that turns one email into 400 leads should cost a few
dollars and produce a loud log line, not a month's budget.
"""

import json
import logging
import os
from datetime import datetime, timedelta

from clients.providers.base import ProviderBudgetExhausted, ProviderNotConfigured
from utilities import credentials

log = logging.getLogger(__name__)

#: Wall-clock ceiling for one research request, in seconds.
#:
#: Generous next to the 30s the JSON providers get: this call runs a server-side
#: web search and several fetches before it answers, and the CLI's own research
#: mode allows 240s for the same work.
REQUEST_TIMEOUT = 240.0

try:
    import anthropic

    ANTHROPIC_AVAILABLE = True
    ANTHROPIC_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - exercised only without the dep
    anthropic = None
    ANTHROPIC_AVAILABLE = False
    ANTHROPIC_IMPORT_ERROR = str(exc)

KEYRING_SERVICE = "job_builder_anthropic"
KEYRING_USERNAME = "api_key"

PLACEHOLDER_KEY = "your-anthropic-api-key-here"

DEFAULT_MODEL = "claude-opus-5"

#: Server-side tools. The dated variants carry dynamic filtering, which keeps
#: search results from flooding the context window.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
WEB_FETCH_TOOL = {"type": "web_fetch_20260209", "name": "web_fetch"}

#: Daily ceiling in output tokens. Output is the expensive half at $25/M, so
#: capping it caps the bill. Roughly $2.50/day at the default.
DEFAULT_DAILY_OUTPUT_TOKENS = 100_000

MISSING_PACKAGES_HINT = (
    "Research needs the Anthropic SDK. Run: pip install -r requirements.txt"
)


# Aliases onto the provider-neutral exceptions - see the note in
# `clients/llm_client.py`. `ResearchNotConfigured` becoming the same class as
# `GroqNotConfigured` is intended: every site that catches either one means
# "this model cannot run, degrade rather than stop".
#
# `SpendCeilingReached` stays a *separate* class from `ProviderRateLimited`.
# `pipeline/prepare.py` reads it as "stop the whole stage" while a rate limit
# means "stop this pass", and collapsing them would spend the day's remaining
# leads rediscovering the ceiling one at a time.
ResearchNotConfigured = ProviderNotConfigured
SpendCeilingReached = ProviderBudgetExhausted


def _load_env():
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
    )
    load_dotenv(dotenv_path=env_path)


def stored_api_key():
    return credentials.read_secret(KEYRING_SERVICE, KEYRING_USERNAME)


def save_api_key(value):
    credentials.write_secret(KEYRING_SERVICE, KEYRING_USERNAME, value.strip())


def forget_api_key():
    return credentials.delete_secret(KEYRING_SERVICE, KEYRING_USERNAME)


def api_key():
    """Credential store first, then .env - same order as the Groq key."""
    stored = stored_api_key()
    if stored and stored.strip():
        return stored.strip()
    _load_env()
    value = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not value or value == PLACEHOLDER_KEY:
        raise ResearchNotConfigured(
            "No Anthropic API key found. Add ANTHROPIC_API_KEY to .env, or "
            "store it in the credential store from Settings."
        )
    return value


def model_name():
    _load_env()
    return (os.environ.get("ANTHROPIC_MODEL") or "").strip() or DEFAULT_MODEL


def daily_output_ceiling():
    _load_env()
    raw = os.environ.get("RESEARCH_DAILY_OUTPUT_TOKENS")
    try:
        value = int(raw) if raw else DEFAULT_DAILY_OUTPUT_TOKENS
    except ValueError:
        return DEFAULT_DAILY_OUTPUT_TOKENS
    return value if value > 0 else DEFAULT_DAILY_OUTPUT_TOKENS


def is_configured():
    if not ANTHROPIC_AVAILABLE:
        return False
    try:
        api_key()
    except ResearchNotConfigured:
        return False
    return True


class SpendLimiter:
    """Refuses calls once the day's output-token budget is gone.

    Reads actual spend back out of `job_research`, so it survives a restart -
    an in-memory counter would reset every time the service bounced, which is
    exactly when a runaway loop is most likely.
    """

    def __init__(self, mail, ceiling=None):
        self.mail = mail
        self.ceiling = ceiling if ceiling is not None else daily_output_ceiling()

    def spent_today(self):
        since = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        return self.mail.research_spend_since(since)

    def remaining(self):
        return max(0, self.ceiling - self.spent_today()["output_tokens"])

    def check(self):
        if self.remaining() <= 0:
            raise SpendCeilingReached(
                f"Daily research budget of {self.ceiling} output tokens is spent. "
                f"Raise RESEARCH_DAILY_OUTPUT_TOKENS or wait for the window to roll."
            )


RESEARCH_SYSTEM_PROMPT = """You research a company and a specific job opening so \
an applicant can tailor their application.

Search the web for current information. Read the job posting itself when a URL \
is provided. Prefer the company's own site and the posting over aggregators.

Report only what you actually found. An empty field is correct and useful; an \
invented one is worse than useless, because the applicant may repeat it in an \
interview.

Quote the posting's own wording for responsibilities and requirements. An \
application that answers a requirement in the words the posting used is easier \
for a reader to match against; a paraphrase makes them do the work.

Reply with JSON only, in this exact shape:
{
  "company_summary": "<2-3 sentences on what the company does>",
  "mission": "<the company's stated mission or the problem it exists to solve>",
  "products": ["<main product or service>"],
  "tech_stack": ["<technology named in the posting or on their engineering site>"],
  "recent_news": ["<a notable recent development, with rough date>"],
  "responsibilities": ["<what the holder of this role will actually do, the posting's wording>"],
  "requirements": ["<a stated requirement, the posting's wording>"],
  "nice_to_haves": ["<a preferred or 'nice to have' item, the posting's wording>"],
  "posting_keywords": ["<skill or requirement emphasised by the posting>"],
  "culture_notes": ["<something specific about how they work, including community or employee development programmes>"],
  "tailoring_advice": "<2-3 sentences on what an application should emphasise>"
}"""


def build_research_prompt(lead):
    parts = [
        f"Role: {lead['title']}",
        f"Company: {lead['company']}",
    ]
    if lead["location"]:
        parts.append(f"Location: {lead['location']}")
    if lead["apply_url"]:
        parts.append(f"Job posting: {lead['apply_url']}")
    return (
        "\n".join(parts)
        + "\n\nResearch this company and role, then reply with the JSON described."
    )


def _json_object(text):
    """Find the JSON object in a reply that may be wrapped in prose.

    Tolerance here is load-bearing rather than defensive. A grounded search
    request cannot also ask for a JSON response type - the Gemini API rejects
    the combination - so the research reply is only asked for JSON in the
    prompt, and a model that obliges with a sentence of preamble is behaving
    normally rather than badly.

    Summary:
        Extract a JSON object from model output, tolerating fences and prose.

    Parameters:
        text (str): The model's raw reply.

    Returns:
        dict | None: The decoded object, or None when nothing parses. Never
            raises.
    """
    cleaned = (text or "").strip()
    # Models occasionally wrap JSON in a fence despite instructions.
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        if len(parts) > 1:
            cleaned = parts[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()

    for candidate in (cleaned, _outermost_braces(cleaned)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _outermost_braces(text):
    """
    Summary:
        Return the span from the first `{` to the last `}`, if both exist.

    Parameters:
        text (str): The text to scan.

    Returns:
        str: The spanned substring, or an empty string when there is no pair.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return ""
    return text[start:end + 1]


def parse_research(text):
    """Validate a research reply into a plain dict. Never raises."""
    if not text:
        return {}
    data = _json_object(text)
    if data is None:
        log.debug("Research reply was not valid JSON")
        return {}

    def _text(value):
        return value.strip()[:2000] if isinstance(value, str) else ""

    def _list(value):
        if not isinstance(value, list):
            return []
        return [item.strip()[:300] for item in value
                if isinstance(item, str) and item.strip()][:12]

    return {
        "company_summary": _text(data.get("company_summary")),
        "mission": _text(data.get("mission")),
        "products": _list(data.get("products")),
        "tech_stack": _list(data.get("tech_stack")),
        "recent_news": _list(data.get("recent_news")),
        "responsibilities": _list(data.get("responsibilities")),
        "requirements": _list(data.get("requirements")),
        "nice_to_haves": _list(data.get("nice_to_haves")),
        "posting_keywords": _list(data.get("posting_keywords")),
        "culture_notes": _list(data.get("culture_notes")),
        "tailoring_advice": _text(data.get("tailoring_advice")),
    }


OPENINGS_SYSTEM_PROMPT = """You check one company for jobs it is currently \
advertising, on behalf of someone who knows an employee there and wants to ask \
for a referral.

Search the web. Read the company's own careers page first; it is the only \
authoritative source for what is open today. Aggregators lag by weeks and list \
roles that have been filled.

Report only openings you actually found a link to. Every opening must carry the \
URL of its own posting - not the careers index, not a search page. An opening \
you cannot link to is one the reader cannot apply to, so leave it out.

An empty list is a correct and useful answer. Inventing a role wastes a real \
favour from a real person, which is worse than reporting nothing.

Reply with JSON only, in this exact shape:
{
  "openings": [
    {
      "title": "<the role title, as the posting words it>",
      "location": "<location or remote arrangement, empty if unstated>",
      "url": "<direct link to this posting>",
      "posted": "<YYYY-MM-DD the posting states, empty if it states none>"
    }
  ]
}"""


def build_openings_prompt(contact):
    """
    Summary:
        Assemble the user half of a careers-page check.

    Parameters:
        contact (Mapping): The contact whose employer to check. Needs
            `company`; `careers_url` and `role` are used when present.

    Returns:
        str: The prompt body.

    Note:
        The careers URL is passed through when the user stored one, which is
        what authorises the fetch tool to read it - the tool only retrieves
        URLs already present in the conversation.
    """
    parts = ["Company: %s" % (contact["company"],)]
    careers_url = contact.get("careers_url") if hasattr(contact, "get") \
        else contact["careers_url"]
    if careers_url:
        parts.append("Careers page: %s" % (careers_url,))
    return (
        "\n".join(parts)
        + "\n\nFind the roles this company is advertising now, then reply "
          "with the JSON described."
    )


def _posted_epoch(value):
    """A stated posting date as epoch seconds.

    Summary:
        Convert a model-reported date string to a timestamp.

    Parameters:
        value: The reported date, expected as `YYYY-MM-DD`.

    Returns:
        int | None: Epoch seconds, or None when nothing usable was given.
            None is normal: most careers pages state no date at all.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return int(datetime.strptime(value.strip()[:10], "%Y-%m-%d").timestamp())
    except ValueError:
        return None


def parse_openings(text):
    """Validate a careers-check reply into a list of openings. Never raises.

    Summary:
        Turn a model reply into the openings it reported.

    Parameters:
        text (str): The model's raw reply, possibly fenced or wrapped in prose.

    Returns:
        list[dict]: `title`, `location`, `url`, and `posted_ts` per opening.
            Capped at 25.

    Note:
        **An entry without a title or a URL is dropped.** That is the whole
        guard on this path: these postings are reported by a model reading the
        web rather than extracted from mail the user actually received, and a
        role with no link is one nothing can verify and nobody can apply to.
    """
    if not text:
        return []
    data = _json_object(text)
    if data is None:
        log.debug("Openings reply was not valid JSON")
        return []

    entries = data.get("openings")
    if not isinstance(entries, list):
        return []

    openings = []
    for entry in entries[:25]:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title")
        url = entry.get("url")
        if not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(url, str) or not url.strip().startswith("http"):
            continue
        location = entry.get("location")
        openings.append({
            "title": title.strip()[:200],
            "location": location.strip()[:120]
                if isinstance(location, str) and location.strip() else None,
            "url": url.strip()[:1000],
            "posted_ts": _posted_epoch(entry.get("posted")),
        })
    return openings


class ResearchClient:
    """Claude with web search, wrapped for one job at a time.

    `caller` is injectable so tests never reach the network - the same pattern
    `GroqClient` uses for its `poster`.
    """

    def __init__(self, key=None, model=None, caller=None, limiter=None):
        self.key = key
        self.model = model or DEFAULT_MODEL
        self.limiter = limiter
        self._caller = caller
        self._client = None

    @classmethod
    def from_config(cls, limiter=None):
        if not ANTHROPIC_AVAILABLE:
            raise ResearchNotConfigured(MISSING_PACKAGES_HINT)
        return cls(key=api_key(), model=model_name(), limiter=limiter)

    def _call(self, prompt, system=None):
        """One request. Returns `(text, input_tokens, output_tokens)`.

        Summary:
            Send one web-search request and return its text and usage.

        Parameters:
            prompt (str): The user-side prompt.
            system (str | None): The system prompt. None uses
                `RESEARCH_SYSTEM_PROMPT`, so the research path is unchanged.

        Returns:
            tuple[str, int, int]: Reply text, input tokens, output tokens.

        Note:
            `system` is a parameter rather than a second client because the two
            jobs differ only in what is asked for: both want the same model,
            the same tools, and the same tolerance for a prose-wrapped reply.
            The injected `caller` still takes the prompt alone, so every
            existing test double keeps working.
        """
        if self._caller is not None:
            return self._caller(prompt)

        if self._client is None:
            # Explicit timeout. Every other network path in the app has one -
            # 30s for Groq and Gemini, 90/240s for the CLI subprocess - and
            # this was relying on whatever the SDK happened to default to. A
            # research call runs in an executor thread and holds it for its
            # whole duration, so "however long the SDK feels like" is a hole in
            # the cycle's time budget.
            self._client = anthropic.Anthropic(api_key=self.key,
                                               timeout=REQUEST_TIMEOUT)

        response = self._client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system or RESEARCH_SYSTEM_PROMPT,
            tools=[WEB_SEARCH_TOOL, WEB_FETCH_TOOL],
            messages=[{"role": "user", "content": prompt}],
        )

        # Safety classifiers can decline; check before reading content, or an
        # unconditional content[0] raises on an otherwise successful response.
        if response.stop_reason == "refusal":
            log.warning("Research request was declined by safety classifiers")
            return "", response.usage.input_tokens, response.usage.output_tokens

        text = "".join(block.text for block in response.content
                       if getattr(block, "type", None) == "text")
        return text, response.usage.input_tokens, response.usage.output_tokens

    def research(self, lead):
        """Research one lead. Returns `(payload, input_tokens, output_tokens)`."""
        if self.limiter is not None:
            self.limiter.check()
        text, input_tokens, output_tokens = self._call(build_research_prompt(lead))
        return parse_research(text), input_tokens, output_tokens

    def find_openings(self, contact):
        """Check one company for what it is advertising now.

        Summary:
            Search a contact's employer for current openings.

        Parameters:
            contact (Mapping): Needs `company`; `careers_url` is used when set.

        Returns:
            tuple[list, int, int]: The openings, input tokens, output tokens.
                The list is empty when nothing usable came back, which is a
                normal answer rather than a failure.

        Raises:
            ProviderBudgetExhausted: When a limiter is attached and its ceiling
                is spent.
        """
        if self.limiter is not None:
            self.limiter.check()
        text, input_tokens, output_tokens = self._call(
            build_openings_prompt(contact), system=OPENINGS_SYSTEM_PROMPT
        )
        return parse_openings(text), input_tokens, output_tokens
