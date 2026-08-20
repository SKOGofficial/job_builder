"""Gemini as a second classification provider.

Deliberately the same shape as `clients/llm_client.py`: `complete_json`, a
`Pacer`, an injectable `poster`, and the same credential-store-first key
resolution. Every pipeline stage takes an injected client and calls exactly one
method on it, so matching that surface is what lets Gemini serve any stage
without a single call site changing.

Plain REST through `requests`, no provider SDK - the same choice Groq gets, and
for the same reason: one HTTP call with a JSON body does not need a dependency,
and the injectable `poster` keeps tests off the network.

Where it differs from Groq, and why the differences are handled here rather
than leaked upward:

- Gemini splits the message list. System prompts go in `systemInstruction`, and
  the assistant role is called `model`. `to_contents` translates.
- It has three ways to return no usable text where Groq has one - a blocked
  prompt, no candidates, or a candidate with no parts after a safety or
  length stop. All three degrade to the caller's fallback.
- Its 429 can mean "wait a moment" or "come back tomorrow", and the difference
  is only visible in the error body. `gemini_retry_after` reads it, because a
  daily lockout should send work to another provider rather than be retried.

Rate limits move and are per-project - Google publishes them in AI Studio
rather than the docs - so all three ceilings are configuration with
conservative defaults, not constants.
"""

import json
import os
import re

from clients.providers.base import (
    Pacer,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderRequestTooLarge,
    estimate_tokens,
    retry_after_seconds,
)
from utilities import credentials

try:
    import requests
    from dotenv import load_dotenv

    GEMINI_AVAILABLE = True
    GEMINI_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - exercised only without the deps
    requests = None
    load_dotenv = None
    GEMINI_AVAILABLE = False
    GEMINI_IMPORT_ERROR = str(exc)

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
REQUEST_TIMEOUT = 30

KEYRING_SERVICE = "job_builder_gemini"
KEYRING_USERNAME = "api_key"

#: Shipped in .env.example. Treated as "not configured" so copying the example
#: file without editing it cannot send a junk key to Google.
PLACEHOLDER_KEY = "your-gemini-api-key-here"

DISPLAY_NAME = "Gemini"

#: Pinned rather than the `gemini-flash-latest` alias. That alias is hot-swapped
#: on every release, which would change how a classifier behaves with no commit
#: and no way to correlate the change with the labels it produced.
DEFAULT_MODEL = "gemini-3.6-flash"

#: Free-tier shaped, and all three overridable. Google publishes per-project
#: limits in AI Studio rather than the docs, so these are conservative starting
#: points: under the observed 10 requests/minute, and well under the daily
#: allowance so an unattended run cannot spend it before lunch.
DEFAULT_REQUESTS_PER_MINUTE = 8
DEFAULT_TOKENS_PER_MINUTE = 250_000
DEFAULT_REQUESTS_PER_DAY = 1200

#: A retry hint this long is not a pause, it is a lockout until the quota
#: resets. Treated as a daily denial so work moves to another provider instead
#: of sleeping through the rest of the cycle.
DAY_SCOPE_SECONDS = 3600

MISSING_PACKAGES_HINT = (
    "Gemini support needs extra packages. Run: pip install -r requirements.txt"
)


# Configuration ------------------------------------------------------------


def _load_env():
    """Load `.env` if python-dotenv is installed.

    Summary:
        Populate the environment from the project's .env file.

    Note:
        Guarded on the module-level `load_dotenv` name rather than importing
        locally, because the tests null that name out to keep a developer's
        real .env from leaking into a run.
    """
    if load_dotenv:
        env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            ".env",
        )
        load_dotenv(dotenv_path=env_path)


def stored_api_key():
    """
    Summary:
        Return the key held in the OS credential store, if any.

    Returns:
        str | None: The stored key, or None on a machine with no credential
            store - which is what makes the .env fallback below the real path
            there.
    """
    return credentials.read_secret(KEYRING_SERVICE, KEYRING_USERNAME)


def save_api_key(value):
    """
    Summary:
        Move a key into the OS credential store.

    Parameters:
        value (str): The key to store.

    Raises:
        CredentialStoreUnavailable: If no credential store can accept it.
    """
    credentials.write_secret(KEYRING_SERVICE, KEYRING_USERNAME, value.strip())


def forget_api_key():
    """
    Summary:
        Delete the stored key.

    Returns:
        bool: True when a key was removed, False when there was none.
    """
    return credentials.delete_secret(KEYRING_SERVICE, KEYRING_USERNAME)


def api_key():
    """Resolve the key: credential store first, then .env.

    Summary:
        Return a usable Gemini API key.

    Returns:
        str: The resolved key.

    Raises:
        ProviderNotConfigured: When no key is set, or when the value is still
            the placeholder shipped in .env.example.
    """
    stored = stored_api_key()
    if stored and stored.strip():
        return stored.strip()
    _load_env()
    value = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not value or value == PLACEHOLDER_KEY:
        raise ProviderNotConfigured(
            "No Gemini API key found. Add GEMINI_API_KEY to .env, or store it "
            "in your credential manager from Settings."
        )
    return value


def model_name():
    """
    Summary:
        The configured Gemini model, or the default.

    Returns:
        str: A model id suitable for the generateContent endpoint.
    """
    _load_env()
    return (os.environ.get("GEMINI_MODEL") or "").strip() or DEFAULT_MODEL


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


def requests_per_minute():
    """
    Summary:
        The configured per-minute request ceiling.

    Returns:
        int: Requests per minute.
    """
    _load_env()
    return _positive_number(
        os.environ.get("GEMINI_REQUESTS_PER_MINUTE"), DEFAULT_REQUESTS_PER_MINUTE, int
    )


def tokens_per_minute():
    """
    Summary:
        The configured per-minute token ceiling.

    Returns:
        int: Tokens per minute.
    """
    _load_env()
    return _positive_number(
        os.environ.get("GEMINI_TOKENS_PER_MINUTE"), DEFAULT_TOKENS_PER_MINUTE, int
    )


def requests_per_day():
    """The per-day request ceiling, which Groq has no equivalent of.

    Summary:
        The configured daily request ceiling.

    Returns:
        int: Requests per day. 0 disables the daily check entirely, which is
            why this does not go through `_positive_number` - 0 is a
            meaningful value here rather than a bad one.
    """
    _load_env()
    raw = os.environ.get("GEMINI_REQUESTS_PER_DAY")
    if raw is None or not str(raw).strip():
        return DEFAULT_REQUESTS_PER_DAY
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_REQUESTS_PER_DAY
    return value if value >= 0 else DEFAULT_REQUESTS_PER_DAY


def is_configured():
    """
    Summary:
        Whether Gemini can serve a request right now.

    Returns:
        bool: True when the packages are present and a key resolves.
    """
    if not GEMINI_AVAILABLE:
        return False
    try:
        api_key()
    except ProviderNotConfigured:
        return False
    return True


# Request and response translation ----------------------------------------


def to_contents(messages):
    """Translate OpenAI-style chat messages into Gemini's request shape.

    Gemini splits what OpenAI keeps in one list: system prompts belong in a
    separate `systemInstruction` field, and the assistant role is spelled
    `model`. Every current call site sends exactly `[system, user]`, but this
    is written generally so a future multi-turn stage does not need a second
    translator.

    Summary:
        Split chat messages into Gemini's `contents` and `systemInstruction`.

    Parameters:
        messages (list[dict]): Chat messages with `role` and `content` keys.

    Returns:
        tuple[list[dict], dict | None]: The `contents` array, and the
            `systemInstruction` object - None when no system message was given,
            since Gemini rejects an empty one.

    Note:
        Multiple system messages are joined rather than dropped. Nothing sends
        two today; silently discarding one later would be a subtle prompt bug.
    """
    system = "\n\n".join(
        m.get("content", "") for m in messages if m.get("role") == "system"
    )
    contents = [
        {
            "role": "model" if m.get("role") == "assistant" else "user",
            "parts": [{"text": m.get("content", "")}],
        }
        for m in messages
        if m.get("role") != "system"
    ]
    return contents, ({"parts": [{"text": system}]} if system else None)


def _error_details(response):
    """
    Summary:
        Pull the `details` array out of a Gemini error body.

    Parameters:
        response: The HTTP response.

    Returns:
        list: The error details, or an empty list when the body is missing,
            not JSON, or not shaped as expected. Never raises - this runs
            while already handling an error, and failing here would replace a
            useful rate-limit message with a parse traceback.
    """
    try:
        payload = response.json()
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    error = payload.get("error")
    if not isinstance(error, dict):
        return []
    details = error.get("details")
    return details if isinstance(details, list) else []


def _retry_delay_seconds(details):
    """
    Summary:
        Read `RetryInfo.retryDelay` out of Gemini error details.

    Parameters:
        details (list): The error `details` array.

    Returns:
        int | None: Seconds to wait, or None when no RetryInfo is present.

    Note:
        The value is a duration string like "27s" or "1.5s", not a number.
    """
    for entry in details:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("retryDelay")
        if raw is None:
            continue
        match = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*s?\s*$", str(raw))
        if match:
            return int(float(match.group(1)))
    return None


def _names_a_daily_quota(details):
    """
    Summary:
        Whether a QuotaFailure in the error names a per-day quota.

    Parameters:
        details (list): The error `details` array.

    Returns:
        bool: True when any violation's quota id or metric mentions a daily
            limit, for example "GenerateRequestsPerDayPerProjectPerModel".
    """
    for entry in details:
        if not isinstance(entry, dict):
            continue
        for violation in entry.get("violations") or []:
            if not isinstance(violation, dict):
                continue
            text = f"{violation.get('quotaId', '')} {violation.get('quotaMetric', '')}"
            if "perday" in text.replace("_", "").replace("-", "").lower():
                return True
    return False


def gemini_retry_after(response):
    """Seconds Gemini asked us to wait, and whether the limit is a daily one.

    Three sources in order: the standard `Retry-After` header, the `RetryInfo`
    entry in the error body's `details` array, then a one-minute default.

    The scope matters more than the number. A per-minute limit clears inside
    the cycle, so waiting is reasonable; a per-day one does not, and sleeping
    on it would stall the pipeline until midnight Pacific while another
    provider sits idle. Two independent signals mark it: a `QuotaFailure`
    naming a per-day quota, or a delay of an hour or more, which is a lockout
    however it is labelled.

    Summary:
        Extract the retry hint and limit scope from a Gemini 429.

    Parameters:
        response: The HTTP response carrying the 429.

    Returns:
        tuple[int, str]: Seconds to wait, and the scope - "day" or "minute".
    """
    details = _error_details(response)
    header = (getattr(response, "headers", None) or {}).get("retry-after")
    if header is not None:
        seconds = retry_after_seconds(response)
    else:
        parsed = _retry_delay_seconds(details)
        seconds = parsed if parsed is not None else 60
    daily = _names_a_daily_quota(details) or seconds >= DAY_SCOPE_SECONDS
    return seconds, ("day" if daily else "minute")


# Client -------------------------------------------------------------------


class GeminiClient:
    """One Gemini model, paced, speaking the same surface as `GroqClient`."""

    def __init__(self, key, model=DEFAULT_MODEL,
                 per_minute=DEFAULT_REQUESTS_PER_MINUTE,
                 tokens_per_minute=DEFAULT_TOKENS_PER_MINUTE,
                 pacer=None, poster=None):
        self.key = key
        self.model = model
        self.pacer = pacer or Pacer(per_minute, tokens_per_minute=tokens_per_minute)
        # Injectable so tests never reach the network.
        self.poster = poster or (requests.post if requests else None)
        #: Total tokens the last response reported, for the pool to reconcile
        #: its optimistic booking against what was really spent.
        self.last_total_tokens = 0

    @property
    def last_model(self):
        """
        Summary:
            Name the model behind the most recent completion.

        Returns:
            str: The configured model name.
        """
        return self.model

    @classmethod
    def from_config(cls):
        """
        Summary:
            Build a client from the environment and credential store.

        Returns:
            GeminiClient: A configured client.

        Raises:
            ProviderNotConfigured: When the packages are missing or no key
                resolves.
        """
        if not GEMINI_AVAILABLE:
            raise ProviderNotConfigured(MISSING_PACKAGES_HINT)
        return cls(
            key=api_key(),
            model=model_name(),
            per_minute=requests_per_minute(),
            tokens_per_minute=tokens_per_minute(),
        )

    def endpoint(self):
        """
        Summary:
            The generateContent URL for this client's model.

        Returns:
            str: The full endpoint URL, with no credential in it.
        """
        return f"{API_BASE}/{self.model}:generateContent"

    def complete_json(self, messages, parser, fallback, max_tokens=200):
        """One paced, JSON-mode completion, validated by the caller's parser.

        The same contract as `GroqClient.complete_json`, deliberately, down to
        the argument order: the pipeline stages hold one of these without
        knowing which.

        Summary:
            Send one paced, JSON-mode completion and hand the reply to a
            caller-supplied validator.

        Parameters:
            messages (list[dict]): The chat messages to send.
            parser (Callable[[str], Any]): Validates the model's reply text.
            fallback (Any): Returned when the model produces nothing usable.
            max_tokens (int): Output ceiling for the request.

        Returns:
            Any: Whatever `parser` returns, or `fallback`.

        Raises:
            ProviderRateLimited: On HTTP 429, carrying the retry hint and
                whether the limit was a per-minute or per-day one.
            RuntimeError: On any other HTTP error at or above 400.

        Note:
            No `responseSchema` is sent. Every parser in this project already
            validates and clamps untrusted model output, and the fixed label
            sets are what stop a crafted email steering a status write. A
            schema would duplicate that validation somewhere it can drift.
        """
        contents, system = to_contents(messages)
        body = {
            "contents": contents,
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        }
        if system is not None:
            body["systemInstruction"] = system

        self.pacer.wait(estimate_tokens(messages, max_tokens))
        response = self.poster(
            self.endpoint(),
            headers={
                # A header rather than the documented ?key= parameter: requests
                # echoes URLs into exception text and logged tracebacks, and a
                # credential must not travel there.
                "x-goog-api-key": self.key,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 429:
            seconds, scope = gemini_retry_after(response)
            raise ProviderRateLimited(
                "Gemini rate limit reached.",
                retry_after=seconds,
                provider=DISPLAY_NAME,
                scope=scope,
            )
        if response.status_code in (413, 400) and "too large" in (
            getattr(response, "text", "") or ""
        ).lower():
            # Gemini reports an oversized request as 400 rather than 413, so
            # the body has to be read. Same meaning either way: too big to
            # serve, and no amount of waiting changes that.
            raise ProviderRequestTooLarge(
                f"Gemini refused the request as too large: "
                f"{getattr(response, 'text', '')[:200]}"
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Gemini returned HTTP {response.status_code}: "
                f"{getattr(response, 'text', '')[:200]}"
            )

        payload = response.json()
        return self._read(payload, parser, fallback)

    def _read(self, payload, parser, fallback):
        """Turn a successful response body into a parsed result.

        Gemini has three ways to come back with no usable text where Groq has
        one, and all three are ordinary rather than exceptional: a prompt the
        safety filters refused, a response with no candidates, and a candidate
        whose parts are empty because it stopped on SAFETY or MAX_TOKENS. Each
        degrades to the caller's fallback, which for every stage here means
        "unclear" - the same answer as an unparseable reply.

        Summary:
            Extract and parse the model's text, or return the fallback.

        Parameters:
            payload (dict): The decoded response body.
            parser (Callable[[str], Any]): Validates the model's reply text.
            fallback (Any): Returned when there is no usable text.

        Returns:
            Any: Whatever `parser` returns, or `fallback`.

        Note:
            Usage is booked before any early return. A blocked prompt still
            costs input tokens, and not recording them would let the pacer
            believe it had room it does not have.
        """
        usage = payload.get("usageMetadata") or {}
        self.last_total_tokens = usage.get("totalTokenCount") or (
            (usage.get("promptTokenCount") or 0)
            + (usage.get("candidatesTokenCount") or 0)
        )
        self.pacer.record(self.last_total_tokens)

        if (payload.get("promptFeedback") or {}).get("blockReason"):
            return fallback
        candidates = payload.get("candidates") or []
        if not candidates:
            return fallback
        parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
        text = "".join(
            part.get("text", "") for part in parts if isinstance(part, dict)
        )
        if not text.strip():
            return fallback
        return parser(text)

    def classify(self, match):
        """Classify one matched reply.

        Summary:
            Label a matched email reply, for the legacy per-job classifier.

        Parameters:
            match (dict): The match payload, with `sender`, `subject`, `body`,
                `company` and `position_title`.

        Returns:
            dict: Keys `label`, `confidence`, `reason`.

        Raises:
            ProviderRateLimited: On HTTP 429.

        Note:
            Prompt and parser are imported from `clients.llm_client` rather
            than restated, so both providers classify against exactly the same
            instructions and the same fixed label set. Two copies of a prompt
            drift, and the drift would show up as a provider that "labels
            differently" for no visible reason.
        """
        from clients.llm_client import build_messages, parse_classification, unclear

        return self.complete_json(
            build_messages(match),
            parse_classification,
            unclear("Model returned no choices."),
        )
