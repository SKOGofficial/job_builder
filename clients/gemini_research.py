"""Company and role research on Gemini, grounded in Google Search.

The counterpart to `research_client.py`, and deliberately indistinguishable
from it to callers: `research(lead)` returns `(payload, input_tokens,
output_tokens)` and `.model` names what produced it, which is exactly what
`pipeline/generate.py` reads. The prompt, the prompt builder and the validator
are imported from that module rather than restated, so there is one copy of
each and the two providers cannot drift into researching different things.

The one real constraint, and the reason this is a separate module rather than a
flag on the classification client:

**Google Search grounding and JSON response mode are mutually exclusive.**
Sending `tools: [{"google_search": {}}]` together with
`generationConfig.responseMimeType = "application/json"` is rejected with HTTP
400 - "Function calling with a response mime type: 'application/json' is
unsupported". The same applies to `responseSchema`. Research needs the tool, so
it cannot have the response type, and the reply arrives as ordinary text that
may or may not be fenced.

That costs nothing, because `parse_research` was already written to never raise
and to dig JSON out of whatever it is handed. The alternative - one grounded
call followed by a second call to reformat - would double the request count
against the daily cap on the provider we made primary for research, and would
reintroduce hallucination in a pass whose only job is reformatting.
"""

import logging

from clients.providers.base import ProviderRateLimited
from clients.providers.gemini import (
    API_BASE,
    DISPLAY_NAME,
    REQUEST_TIMEOUT,
    GEMINI_AVAILABLE,
    MISSING_PACKAGES_HINT,
    ProviderNotConfigured,
    api_key,
    gemini_retry_after,
    model_name,
    requests as _requests,
    to_contents,
)
from clients.research_client import (
    OPENINGS_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
    build_openings_prompt,
    build_research_prompt,
    parse_openings,
    parse_research,
)

log = logging.getLogger(__name__)

#: The grounding tool. Named in configuration rather than sniffed from the
#: model id, because models before 2.0 spell it `google_search_retrieval` and a
#: silently wrong key produces an ungrounded answer that looks fine.
DEFAULT_SEARCH_TOOL = "google_search"

#: Output ceiling, matching `ResearchClient`'s.
MAX_OUTPUT_TOKENS = 4096


def search_tool():
    """
    Summary:
        The grounding tool declaration to send.

    Returns:
        dict: A Gemini tool object, honouring `GEMINI_SEARCH_TOOL`.
    """
    import os

    name = (os.environ.get("GEMINI_SEARCH_TOOL") or "").strip() \
        or DEFAULT_SEARCH_TOOL
    return {name: {}}


class GeminiResearchClient:
    """Gemini with Google Search grounding, wrapped for one job at a time.

    `caller` is injectable so tests never reach the network - the same pattern
    `ResearchClient` uses, and for the same reason.
    """

    def __init__(self, key=None, model=None, caller=None, limiter=None,
                 poster=None):
        self.key = key
        self.model = model or "gemini-3.6-flash"
        self.limiter = limiter
        self._caller = caller
        self.poster = poster or (_requests.post if _requests else None)
        #: Total tokens the last response reported, for the pool to reconcile.
        self.last_total_tokens = 0

    @property
    def last_model(self):
        """
        Summary:
            Name the model behind the most recent research call.

        Returns:
            str: The configured model name.
        """
        return self.model

    @classmethod
    def from_config(cls, limiter=None):
        """
        Summary:
            Build a research client from the environment and credential store.

        Parameters:
            limiter: An optional spend limiter, checked before each call.

        Returns:
            GeminiResearchClient: A configured client.

        Raises:
            ProviderNotConfigured: When the packages are missing or no key
                resolves.
        """
        if not GEMINI_AVAILABLE:
            raise ProviderNotConfigured(MISSING_PACKAGES_HINT)
        return cls(key=api_key(), model=model_name(), limiter=limiter)

    def endpoint(self):
        """
        Summary:
            The generateContent URL for this client's model.

        Returns:
            str: The full endpoint URL, with no credential in it.
        """
        return f"{API_BASE}/{self.model}:generateContent"

    def build_body(self, prompt, system_prompt=None):
        """The grounded request body.

        Summary:
            Assemble the generateContent body for one grounded call.

        Parameters:
            prompt (str): The user-side prompt from `build_research_prompt` or
                `build_openings_prompt`.
            system_prompt (str | None): The system instruction. None uses
                `RESEARCH_SYSTEM_PROMPT`, leaving the research path unchanged.

        Returns:
            dict: The request body.

        Note:
            Carries `tools` and deliberately omits `responseMimeType`. The two
            cannot be sent together - see the module docstring. There is a test
            asserting exactly this pairing, because the failure mode if it
            regresses is an HTTP 400 on every research call. The same applies
            to a careers check, which is why it shares this body rather than
            building its own.
        """
        contents, system = to_contents([
            {"role": "system", "content": system_prompt or RESEARCH_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ])
        body = {
            "contents": contents,
            "tools": [search_tool()],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
            },
        }
        if system is not None:
            body["systemInstruction"] = system
        return body

    def _call(self, prompt, system_prompt=None):
        """One request. Returns `(text, input_tokens, output_tokens)`.

        Summary:
            Send one grounded request and return its text and usage.

        Parameters:
            prompt (str): The user-side prompt.
            system_prompt (str | None): The system instruction. None uses the
                research prompt. The injected `caller` still takes the prompt
                alone, so existing test doubles keep working.

        Returns:
            tuple[str, int, int]: Reply text, input tokens, output tokens.
                Empty text when the model returned nothing usable, which the
                caller's parser turns into an empty payload rather than an
                error.

        Raises:
            ProviderRateLimited: On HTTP 429.
            RuntimeError: On any other HTTP error at or above 400.
        """
        if self._caller is not None:
            return self._caller(prompt)

        response = self.poster(
            self.endpoint(),
            headers={
                "x-goog-api-key": self.key,
                "Content-Type": "application/json",
            },
            json=self.build_body(prompt, system_prompt),
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
        if response.status_code >= 400:
            raise RuntimeError(
                f"Gemini returned HTTP {response.status_code}: "
                f"{getattr(response, 'text', '')[:200]}"
            )

        payload = response.json()
        usage = payload.get("usageMetadata") or {}
        input_tokens = usage.get("promptTokenCount") or 0
        output_tokens = usage.get("candidatesTokenCount") or 0
        self.last_total_tokens = usage.get("totalTokenCount") or (
            input_tokens + output_tokens
        )

        if (payload.get("promptFeedback") or {}).get("blockReason"):
            log.warning("Research request was declined by Gemini safety filters")
            return "", input_tokens, output_tokens
        candidates = payload.get("candidates") or []
        if not candidates:
            return "", input_tokens, output_tokens
        parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
        text = "".join(
            part.get("text", "") for part in parts if isinstance(part, dict)
        )
        return text, input_tokens, output_tokens

    def research(self, lead):
        """Research one lead.

        Summary:
            Research a company and role, returning the payload and its cost.

        Parameters:
            lead (dict | sqlite3.Row): Needs `title`, `company`, `location`
                and `apply_url`.

        Returns:
            tuple[dict, int, int]: The parsed payload, input tokens, output
                tokens. The payload is `{}` when nothing usable came back.

        Raises:
            ProviderBudgetExhausted: When a limiter is attached and its ceiling
                is spent.
            ProviderRateLimited: On HTTP 429.
        """
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

        Raises:
            ProviderBudgetExhausted: When a limiter is attached and its ceiling
                is spent.
            ProviderRateLimited: On HTTP 429.

        Note:
            The prompt and the parser are imported from `research_client` for
            the same reason `research` imports its own: one copy of each, so
            the two providers cannot drift into checking different things.
        """
        if self.limiter is not None:
            self.limiter.check()
        text, input_tokens, output_tokens = self._call(
            build_openings_prompt(contact), OPENINGS_SYSTEM_PROMPT
        )
        return parse_openings(text), input_tokens, output_tokens
