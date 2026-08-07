"""The Gemini transport.

Two clusters matter most. The request shape, because Gemini splits the message
list differently from Groq and a system prompt landing in `contents` would
silently become user-supplied text - which is the exact boundary the injection
defence depends on. And the 429 body, because whether a limit is per-minute or
per-day decides between waiting and failing over, and only the error body says
which.
"""

import os
import unittest

import clients.providers.gemini as gemini
from clients.providers.base import ProviderNotConfigured, ProviderRateLimited
from utilities import credentials


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


def reply(text, total_tokens=850):
    """A minimal successful generateContent body."""
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"totalTokenCount": total_tokens},
    }


def quota_error(retry_delay=None, quota_id=None):
    """A 429 body shaped like Google's, with optional RetryInfo/QuotaFailure."""
    details = []
    if quota_id is not None:
        details.append({
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{"quotaId": quota_id, "quotaMetric": "generate_requests"}],
        })
    if retry_delay is not None:
        details.append({
            "@type": "type.googleapis.com/google.rpc.RetryInfo",
            "retryDelay": retry_delay,
        })
    return {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": details}}


class EnvIsolationMixin:
    """Keeps tests off the developer's real .env and keyring.

    Mirrors the mixin in test_llm_classification.py. `gemini.load_dotenv` is
    nulled the same way, which is why the module keeps a module-level handle on
    it rather than importing inside the function.
    """

    GEMINI_VARS = (
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "GEMINI_REQUESTS_PER_MINUTE",
        "GEMINI_TOKENS_PER_MINUTE",
        "GEMINI_REQUESTS_PER_DAY",
    )

    def isolate_env(self):
        self.saved_env = {name: os.environ.get(name) for name in self.GEMINI_VARS}
        for name in self.GEMINI_VARS:
            os.environ.pop(name, None)
        self.saved_loader = gemini.load_dotenv
        gemini.load_dotenv = None
        self.saved_keyring = credentials.keyring
        credentials.keyring = None
        self.addCleanup(self.restore_env)

    def restore_env(self):
        for name, value in self.saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        gemini.load_dotenv = self.saved_loader
        credentials.keyring = self.saved_keyring


class ConfigTests(EnvIsolationMixin, unittest.TestCase):
    def setUp(self):
        self.isolate_env()

    def test_missing_key_is_not_configured(self):
        with self.assertRaises(ProviderNotConfigured):
            gemini.api_key()
        self.assertFalse(gemini.is_configured())

    def test_placeholder_counts_as_unset(self):
        """Copying .env.example without editing must not send a junk key."""
        os.environ["GEMINI_API_KEY"] = gemini.PLACEHOLDER_KEY
        with self.assertRaises(ProviderNotConfigured):
            gemini.api_key()

    def test_env_key_is_used(self):
        os.environ["GEMINI_API_KEY"] = "env-key"
        self.assertEqual(gemini.api_key(), "env-key")

    def test_defaults(self):
        self.assertEqual(gemini.model_name(), gemini.DEFAULT_MODEL)
        self.assertEqual(gemini.requests_per_minute(),
                         gemini.DEFAULT_REQUESTS_PER_MINUTE)
        self.assertEqual(gemini.tokens_per_minute(), gemini.DEFAULT_TOKENS_PER_MINUTE)
        self.assertEqual(gemini.requests_per_day(), gemini.DEFAULT_REQUESTS_PER_DAY)

    def test_overrides(self):
        os.environ["GEMINI_MODEL"] = "gemini-3.5-flash"
        os.environ["GEMINI_REQUESTS_PER_MINUTE"] = "4"
        os.environ["GEMINI_TOKENS_PER_MINUTE"] = "1000"
        os.environ["GEMINI_REQUESTS_PER_DAY"] = "50"
        self.assertEqual(gemini.model_name(), "gemini-3.5-flash")
        self.assertEqual(gemini.requests_per_minute(), 4)
        self.assertEqual(gemini.tokens_per_minute(), 1000)
        self.assertEqual(gemini.requests_per_day(), 50)

    def test_rubbish_falls_back_to_defaults(self):
        os.environ["GEMINI_REQUESTS_PER_MINUTE"] = "not-a-number"
        os.environ["GEMINI_REQUESTS_PER_DAY"] = "-5"
        self.assertEqual(gemini.requests_per_minute(),
                         gemini.DEFAULT_REQUESTS_PER_MINUTE)
        self.assertEqual(gemini.requests_per_day(), gemini.DEFAULT_REQUESTS_PER_DAY)

    def test_zero_days_disables_the_daily_check(self):
        """0 is meaningful here, not a bad value - .env.example documents it."""
        os.environ["GEMINI_REQUESTS_PER_DAY"] = "0"
        self.assertEqual(gemini.requests_per_day(), 0)


class ToContentsTests(unittest.TestCase):
    def test_system_prompt_is_separated(self):
        contents, system = gemini.to_contents([
            {"role": "system", "content": "You classify."},
            {"role": "user", "content": "An email."},
        ])
        self.assertEqual(system, {"parts": [{"text": "You classify."}]})
        self.assertEqual(contents, [{"role": "user", "parts": [{"text": "An email."}]}])

    def test_assistant_becomes_model(self):
        contents, _system = gemini.to_contents([
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ])
        self.assertEqual([c["role"] for c in contents], ["user", "model", "user"])

    def test_no_system_message_yields_none(self):
        """Gemini rejects an empty systemInstruction, so it must be omitted."""
        _contents, system = gemini.to_contents([{"role": "user", "content": "a"}])
        self.assertIsNone(system)

    def test_multiple_system_messages_are_joined_not_dropped(self):
        _contents, system = gemini.to_contents([
            {"role": "system", "content": "one"},
            {"role": "system", "content": "two"},
            {"role": "user", "content": "a"},
        ])
        self.assertEqual(system["parts"][0]["text"], "one\n\ntwo")


class RetryAfterTests(unittest.TestCase):
    def test_header_wins(self):
        response = FakeResponse(429, quota_error(retry_delay="27s"),
                                headers={"retry-after": "5"})
        self.assertEqual(gemini.gemini_retry_after(response), (5, "minute"))

    def test_retry_info_is_read_when_there_is_no_header(self):
        response = FakeResponse(429, quota_error(retry_delay="27s"))
        self.assertEqual(gemini.gemini_retry_after(response), (27, "minute"))

    def test_fractional_delay_parses(self):
        response = FakeResponse(429, quota_error(retry_delay="1.5s"))
        self.assertEqual(gemini.gemini_retry_after(response)[0], 1)

    def test_default_when_the_body_says_nothing(self):
        self.assertEqual(gemini.gemini_retry_after(FakeResponse(429)), (60, "minute"))

    def test_a_per_day_quota_is_scoped_day(self):
        """The signal that should send work elsewhere rather than sleep."""
        response = FakeResponse(429, quota_error(
            retry_delay="30s",
            quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        ))
        self.assertEqual(gemini.gemini_retry_after(response), (30, "day"))

    def test_a_long_delay_is_a_lockout_however_labelled(self):
        response = FakeResponse(429, quota_error(retry_delay="7200s"))
        self.assertEqual(gemini.gemini_retry_after(response)[1], "day")

    def test_an_unparseable_body_still_answers(self):
        class Broken:
            status_code = 429
            headers = {}

            def json(self):
                raise ValueError("not json")

        self.assertEqual(gemini.gemini_retry_after(Broken()), (60, "minute"))


class ClientTests(unittest.TestCase):
    def client(self, response, **kwargs):
        calls = []

        def poster(url, **payload):
            calls.append((url, payload))
            return response

        client = gemini.GeminiClient(
            key="k", pacer=gemini.Pacer(sleep=lambda _s: None), **kwargs
        )
        client.poster = poster
        return client, calls

    def messages(self):
        return [
            {"role": "system", "content": "You classify."},
            {"role": "user", "content": "We are moving forward."},
        ]

    def test_request_shape(self):
        client, calls = self.client(FakeResponse(200, reply('{"ok": true}')))
        client.complete_json(self.messages(), lambda t: t, "fallback")

        url, payload = calls[0]
        self.assertEqual(
            url,
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{gemini.DEFAULT_MODEL}:generateContent",
        )
        self.assertNotIn("key=", url, "a credential must never be in the URL")
        self.assertEqual(payload["headers"]["x-goog-api-key"], "k")

        body = payload["json"]
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "You classify.")
        self.assertEqual(len(body["contents"]), 1, "system must not reach contents")
        self.assertEqual(
            body["generationConfig"]["responseMimeType"], "application/json"
        )
        self.assertEqual(body["generationConfig"]["temperature"], 0)
        self.assertEqual(body["generationConfig"]["maxOutputTokens"], 200)
        self.assertNotIn("responseSchema", body["generationConfig"])

    def test_max_tokens_reaches_the_request(self):
        client, calls = self.client(FakeResponse(200, reply("{}")))
        client.complete_json(self.messages(), lambda t: t, "f", max_tokens=1500)
        self.assertEqual(calls[0][1]["json"]["generationConfig"]["maxOutputTokens"], 1500)

    def test_text_reaches_the_parser(self):
        client, _calls = self.client(FakeResponse(200, reply('{"label": "Rejected"}')))
        seen = []
        client.complete_json(self.messages(), lambda t: seen.append(t) or t, "f")
        self.assertEqual(seen, ['{"label": "Rejected"}'])

    def test_multipart_text_is_joined(self):
        payload = {
            "candidates": [{"content": {"parts": [{"text": '{"a":'}, {"text": " 1}"}]}}],
            "usageMetadata": {"totalTokenCount": 10},
        }
        client, _calls = self.client(FakeResponse(200, payload))
        self.assertEqual(client.complete_json(self.messages(), lambda t: t, "f"),
                         '{"a": 1}')

    def test_usage_reaches_the_pacer(self):
        client, _calls = self.client(FakeResponse(200, reply("{}", total_tokens=1234)))
        client.complete_json(self.messages(), lambda t: t, "f")
        self.assertEqual(client.last_total_tokens, 1234)

    def test_usage_falls_back_to_the_two_halves(self):
        payload = {
            "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20},
        }
        client, _calls = self.client(FakeResponse(200, payload))
        client.complete_json(self.messages(), lambda t: t, "f")
        self.assertEqual(client.last_total_tokens, 120)

    # The three no-content shapes ------------------------------------------

    def test_blocked_prompt_returns_the_fallback(self):
        payload = {"promptFeedback": {"blockReason": "SAFETY"},
                   "usageMetadata": {"promptTokenCount": 40}}
        client, _calls = self.client(FakeResponse(200, payload))
        self.assertEqual(client.complete_json(self.messages(), lambda t: t, "f"), "f")

    def test_no_candidates_returns_the_fallback(self):
        client, _calls = self.client(FakeResponse(200, {"candidates": []}))
        self.assertEqual(client.complete_json(self.messages(), lambda t: t, "f"), "f")

    def test_candidate_with_no_parts_returns_the_fallback(self):
        """A SAFETY or MAX_TOKENS stop can leave the candidate empty."""
        payload = {"candidates": [{"finishReason": "SAFETY", "content": {}}],
                   "usageMetadata": {"totalTokenCount": 30}}
        client, _calls = self.client(FakeResponse(200, payload))
        self.assertEqual(client.complete_json(self.messages(), lambda t: t, "f"), "f")

    def test_a_blocked_prompt_still_books_its_tokens(self):
        """It cost input tokens; not recording them overstates the headroom."""
        payload = {"promptFeedback": {"blockReason": "SAFETY"},
                   "usageMetadata": {"promptTokenCount": 40}}
        client, _calls = self.client(FakeResponse(200, payload))
        client.complete_json(self.messages(), lambda t: t, "f")
        self.assertEqual(client.last_total_tokens, 40)

    # Errors ----------------------------------------------------------------

    def test_rate_limit_carries_provider_and_scope(self):
        response = FakeResponse(429, quota_error(
            retry_delay="45s",
            quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        ))
        client, _calls = self.client(response)
        with self.assertRaises(ProviderRateLimited) as caught:
            client.complete_json(self.messages(), lambda t: t, "f")
        self.assertEqual(caught.exception.provider, "Gemini")
        self.assertEqual(caught.exception.retry_after, 45)
        self.assertEqual(caught.exception.scope, "day")

    def test_a_gemini_429_is_caught_by_the_existing_groq_handlers(self):
        """The end-to-end payoff of the Phase 1 aliasing."""
        import clients.llm_client as llm

        client, _calls = self.client(FakeResponse(429, quota_error()))
        with self.assertRaises(llm.GroqRateLimited):
            client.complete_json(self.messages(), lambda t: t, "f")

    def test_other_http_errors_raise_runtime_error(self):
        client, _calls = self.client(FakeResponse(500, text="upstream boom"))
        with self.assertRaises(RuntimeError) as caught:
            client.complete_json(self.messages(), lambda t: t, "f")
        self.assertIn("500", str(caught.exception))

    def test_the_key_never_reaches_the_url_or_an_error(self):
        """A credential in the URL ends up in tracebacks and logs."""
        secret = "AIzaSyTOTALLY-SECRET-VALUE"
        calls = []

        def poster(url, **payload):
            calls.append((url, payload))
            return FakeResponse(500, text="upstream boom")

        client = gemini.GeminiClient(
            key=secret, pacer=gemini.Pacer(sleep=lambda _s: None)
        )
        client.poster = poster
        with self.assertRaises(RuntimeError) as caught:
            client.complete_json(self.messages(), lambda t: t, "f")

        self.assertNotIn(secret, calls[0][0])
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, client.endpoint())


class SharedPromptTests(unittest.TestCase):
    """`classify` must use the same prompt and label set as Groq."""

    def test_classify_uses_the_shared_prompt_and_parser(self):
        import clients.llm_client as llm

        calls = []

        def poster(url, **payload):
            calls.append(payload)
            return FakeResponse(200, reply(
                '{"label": "Rejected", "confidence": 0.9, "reason": "no"}'
            ))

        client = gemini.GeminiClient(key="k", pacer=gemini.Pacer(sleep=lambda _s: None))
        client.poster = poster
        result = client.classify({
            "sender": "a@b.com", "subject": "Update",
            "body": "We will not be moving forward.",
            "company": "Acme", "position_title": "Engineer",
        })

        self.assertEqual(result["label"], "Rejected")
        sent = calls[0]["json"]["systemInstruction"]["parts"][0]["text"]
        self.assertEqual(sent, llm.SYSTEM_PROMPT)

    def test_an_unrecognised_label_is_still_discarded(self):
        """The injection defence must not weaken on a second provider."""
        client = gemini.GeminiClient(key="k", pacer=gemini.Pacer(sleep=lambda _s: None))
        client.poster = lambda url, **kw: FakeResponse(
            200, reply('{"label": "Hired", "confidence": 1.0, "reason": "x"}')
        )
        result = client.classify({"sender": "a", "subject": "b", "body": "c"})
        self.assertEqual(result["label"], "Unclear")


if __name__ == "__main__":
    unittest.main()
