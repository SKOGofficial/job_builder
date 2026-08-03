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

from utilities import credentials

log = logging.getLogger(__name__)

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


class ResearchNotConfigured(Exception):
    """Raised when no usable Anthropic API key is available."""


class SpendCeilingReached(Exception):
    """Raised when the daily research budget is exhausted."""


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

Reply with JSON only, in this exact shape:
{
  "company_summary": "<2-3 sentences on what the company does>",
  "products": ["<main product or service>"],
  "tech_stack": ["<technology named in the posting or on their engineering site>"],
  "recent_news": ["<a notable recent development, with rough date>"],
  "posting_keywords": ["<skill or requirement emphasised by the posting>"],
  "culture_notes": ["<something specific about how they work>"],
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


def parse_research(text):
    """Validate a research reply into a plain dict. Never raises."""
    if not text:
        return {}
    # Models occasionally wrap JSON in a fence despite instructions.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    try:
        data = json.loads(cleaned)
    except (TypeError, ValueError):
        log.debug("Research reply was not valid JSON")
        return {}
    if not isinstance(data, dict):
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
        "products": _list(data.get("products")),
        "tech_stack": _list(data.get("tech_stack")),
        "recent_news": _list(data.get("recent_news")),
        "posting_keywords": _list(data.get("posting_keywords")),
        "culture_notes": _list(data.get("culture_notes")),
        "tailoring_advice": _text(data.get("tailoring_advice")),
    }


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

    def _call(self, prompt):
        """One request. Returns `(text, input_tokens, output_tokens)`."""
        if self._caller is not None:
            return self._caller(prompt)

        if self._client is None:
            self._client = anthropic.Anthropic(api_key=self.key)

        response = self._client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=RESEARCH_SYSTEM_PROMPT,
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
