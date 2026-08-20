"""What every model provider shares.

Groq was the only provider for long enough that the pacing, the token
arithmetic, and the exception vocabulary all grew inside `llm_client.py`. None
of it is Groq-specific: a rolling token window and a minimum gap between calls
describe any free tier, and the exceptions describe any provider refusing work.
This module is that shared half, lifted out unchanged so a second provider
inherits it rather than reimplementing it against the same ceiling.

The exception names are the load-bearing part. Six pipeline modules catch
`GroqRateLimited` by name and stop their batch cleanly on it. `llm_client`
aliases that name onto `ProviderRateLimited` here rather than subclassing it,
which is what makes those six sites stop for *any* provider's rate limit
without a single edit. Subclassing would have done the opposite: a Gemini
error would sail straight past `except GroqRateLimited`.
"""

import json
import time

#: Free tier ceiling for Groq's default model. Providers with a different
#: ceiling pass their own; this stays the default so existing callers that
#: construct a bare `Pacer` are unaffected.
TOKENS_PER_MINUTE = 12000

#: Fallback projection for a caller that paces without describing its request.
#: `complete_json` measures the real request instead - see `estimate_tokens`.
#: Left as the `Pacer.wait` default so a bare pacer still books something.
ESTIMATED_TOKENS_PER_CALL = 900

#: Bytes per token for English prose, the usual rough ratio. Applied to the
#: JSON-serialised request, whose quoting and escaping inflate the count a
#: little - which errs toward over-booking, the safe direction.
CHARS_PER_TOKEN = 4

DEFAULT_REQUESTS_PER_MINUTE = 12


class ProviderNotConfigured(Exception):
    """Raised when a provider has no usable API key, or its SDK is absent."""


class ProviderRateLimited(Exception):
    """Raised on HTTP 429 so a batch can stop cleanly instead of retrying.

    Carries enough for a caller to decide between waiting and moving on. The
    `scope` distinction matters more than it looks: a per-minute limit clears
    on its own within the cycle, while a per-day one does not, and only the
    latter is worth writing down so a restart cannot un-exhaust it.

    Parameters:
        message (str): Human-readable reason, surfaced in the UI.
        retry_after (int): Seconds the provider asked us to wait. 0 when the
            provider gave no hint.
        provider (str): Display name of the provider that refused. Empty when
            a test double raises, which is why every consumer treats it as
            optional and falls back to a literal.
        scope (str): "minute" or "day". Anything else is treated as "minute",
            the recoverable reading.
        limits (dict): The rate-limit headers the response reported, as
            collected by `rate_limit_snapshot`. Empty when the provider sent
            none, or when a test double raises.

    Summary:
        Signal that a provider refused a request because of a rate limit.
    """

    def __init__(self, message, retry_after=0, provider="", scope="minute",
                 limits=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.provider = provider
        self.scope = scope if scope == "day" else "minute"
        self.limits = limits or {}


class ProviderRequestTooLarge(Exception):
    """Raised when a provider refuses a request as too big to serve.

    Distinct from `ProviderRateLimited`, and the distinction is the whole
    point: a rate limit clears on its own, so the right response is to wait and
    send the same request again. This never clears. Retrying the identical
    payload against the same provider fails identically for ever, so the only
    useful responses are to send it somewhere with more room or to give up on
    it - which is why the pool treats this as grounds to fail over rather than
    to pause.

    Groq answers HTTP 413 for two different situations that both fit this:
    a payload larger than the model will accept at all, and a single request
    whose prompt plus `max_tokens` exceeds the account's per-minute token
    allowance. The second is limit-shaped but is not a rate limit - waiting
    does not help, because the request is bigger than the whole minute's budget.
    """


class ProviderBudgetExhausted(Exception):
    """Raised when a configured spend or request ceiling is gone for the window.

    Deliberately not a `ProviderRateLimited`. `pipeline/prepare.py` treats this
    as "stop the whole stage" while a rate limit means only "stop this pass",
    and collapsing the two would spend the day's remaining leads discovering
    the ceiling one at a time.
    """


def estimate_tokens(messages, max_tokens):
    """Project what one request will actually cost the rolling token window.

    A flat per-call estimate is what caused the free-tier 429s this replaces.
    Classification sends 2,000 body characters and asks for 200 back - around
    1,100 tokens, close to the old flat 900. Alert extraction sends 6,000
    characters and asks for 1,500 back, which is nearer 3,400. Booking that at
    900 let four calls drain a 12,000-token minute while the pacer believed it
    had room for thirteen.

    Measuring the request removes the guess and, more usefully, removes the
    need for every new call site to remember to pass a number.

    Summary:
        Estimate the token cost of a chat request from its serialised messages
        and its output ceiling.

    Parameters:
        messages (list[dict]): The chat messages about to be sent.
        max_tokens (int): The requested output ceiling.

    Returns:
        int: Projected total tokens, input plus a worst-case output.

    Note:
        Deliberately errs high. JSON quoting inflates the character count, and
        `max_tokens` is a ceiling replies rarely reach, so the projection is
        conservative - which is the safe direction for a rate limit.
    """
    serialized = json.dumps(messages, ensure_ascii=False)
    return len(serialized) // CHARS_PER_TOKEN + max_tokens


def retry_after_seconds(response):
    """Read retry-after, falling back to a minute when it is absent.

    Summary:
        Extract the retry-after hint from an HTTP response.

    Parameters:
        response: Anything exposing a `headers` mapping.

    Returns:
        int: Seconds to wait. 60 when the header is missing or unparseable,
            because no hint is not the same as no wait.
    """
    raw = (getattr(response, "headers", None) or {}).get("retry-after")
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return 60


#: What a provider reports alongside a 429. Requests are typically limited per
#: day and tokens per minute, so the pair says which ceiling was actually
#: reached - the thing `retry-after` on its own leaves ambiguous. Named in the
#: `x-ratelimit-*` convention Groq and Gemini both follow.
RATE_LIMIT_HEADERS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
)


def rate_limit_snapshot(response):
    """
    Summary:
        Collect whichever rate-limit headers a response carries.

    Parameters:
        response: Anything exposing a `headers` mapping.

    Returns:
        dict: Header name to value, for the headers that were present. Empty
            when the response carries none.

    Note:
        Header lookup is case-insensitive on a real `requests` response; a
        plain dict from a test is read as-is, so both work.
    """
    headers = getattr(response, "headers", None) or {}
    snapshot = {}
    for name in RATE_LIMIT_HEADERS:
        value = headers.get(name)
        if value is not None:
            snapshot[name] = value
    return snapshot


def describe_rate_limit(snapshot):
    """
    Summary:
        Render a rate-limit snapshot as one readable line for the log.

    Parameters:
        snapshot (dict): As returned by `rate_limit_snapshot`.

    Returns:
        str: A short summary naming what is left and when it resets, or a note
            that the response said nothing.

    Note:
        Reports requests and tokens separately and on purpose. Seeing which of
        the two is at zero is the whole point: a spent per-minute token budget
        clears in seconds, while a spent daily request budget does not clear
        until tomorrow, and pacing cannot help with the second.
    """
    if not snapshot:
        return "no rate-limit headers reported"
    parts = []
    for kind in ("requests", "tokens"):
        remaining = snapshot.get(f"x-ratelimit-remaining-{kind}")
        if remaining is None:
            continue
        limit = snapshot.get(f"x-ratelimit-limit-{kind}", "?")
        reset = snapshot.get(f"x-ratelimit-reset-{kind}", "?")
        parts.append(f"{remaining}/{limit} {kind} left, resets in {reset}")
    return "; ".join(parts) or "no rate-limit headers reported"


class Pacer:
    """Spaces requests so neither the request nor the token ceiling is reached.

    Both limits are enforced: a minimum gap between calls, and a rolling
    sixty-second window of tokens actually spent, reported by each response.
    sleep and clock are injectable so tests can drive this without waiting.

    Scope note: a `Pacer` describes one provider's per-minute allowance and
    nothing more. Per-day ceilings live in `budget.Budget`, because those must
    survive a restart and a rolling in-memory window cannot.
    """

    def __init__(
        self,
        per_minute=DEFAULT_REQUESTS_PER_MINUTE,
        tokens_per_minute=TOKENS_PER_MINUTE,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self.min_interval = 60.0 / max(1, per_minute)
        self.tokens_per_minute = tokens_per_minute
        self._sleep = sleep
        self._clock = clock
        self._last_call = None
        self._spent = []

    def wait(self, projected_tokens=ESTIMATED_TOKENS_PER_CALL):
        """Block until a request of this size may be sent.

        Summary:
            Sleep for however long both pacing rules together demand.

        Parameters:
            projected_tokens (int): What the pending request is expected to
                cost. See `estimate_tokens`.
        """
        now = self._clock()
        if self._last_call is not None:
            gap = self.min_interval - (now - self._last_call)
            if gap > 0:
                self._sleep(gap)
                now = self._clock()
        delay = self.token_delay(now, projected_tokens)
        if delay > 0:
            self._sleep(delay)
            now = self._clock()
        self._last_call = now

    def interval_delay(self, now):
        """How long the minimum-gap rule alone would make a caller wait.

        The non-sleeping half of `wait`, exposed so a caller choosing between
        providers can ask "what would this cost me?" without committing to it.
        `wait` is what actually sleeps, inside the transport; this only
        reports.

        Summary:
            Report the remaining minimum gap before the next call may go out.

        Parameters:
            now (float): Current monotonic time, from the injected clock.

        Returns:
            float: Seconds to wait. 0.0 when no call has been made yet, or
                when the gap has already elapsed.
        """
        if self._last_call is None:
            return 0.0
        return max(0.0, self.min_interval - (now - self._last_call))

    def token_delay(self, now, projected_tokens):
        """
        Summary:
            How long to wait before a request of this size fits inside the
            rolling sixty-second token window.

        Parameters:
            now (float): Current monotonic time, from the injected clock.
            projected_tokens (int): What the pending request is expected to
                cost. See `estimate_tokens`.

        Returns:
            float: Seconds to sleep. 0.0 when the request fits now.

        Note:
            Returns 0.0 when nothing has been spent yet, even if the request
            alone exceeds the whole per-minute budget. Waiting cannot make room
            that no earlier call is occupying, so blocking would stall forever;
            better to send it and let the API answer. This mattered less when
            every call was booked at a flat 900 tokens - now that projections
            are measured, they can in principle exceed the ceiling.
        """
        self._spent = [(at, n) for at, n in self._spent if now - at < 60.0]
        used = sum(n for _at, n in self._spent)
        if used + projected_tokens <= self.tokens_per_minute:
            return 0.0
        if not self._spent:
            return 0.0
        oldest = min(at for at, _n in self._spent)
        return max(0.0, 60.0 - (now - oldest))

    def record(self, tokens):
        """Book what a completed request actually spent.

        Summary:
            Add a request's real token cost to the rolling window.

        Parameters:
            tokens (int): Total tokens the provider reported. Falsy values are
                ignored, so a provider that omits usage simply does not narrow
                the window.
        """
        if tokens:
            self._spent.append((self._clock(), tokens))
