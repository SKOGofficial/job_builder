"""Grounded research on Gemini.

The first test in `RequestShapeTests` is the important one. Google Search
grounding and a JSON response type cannot be sent together - the API answers
HTTP 400 - so if that pairing ever regresses, every research call fails. It is
not the kind of mistake unit tests usually catch, because the request looks
perfectly reasonable.

Everything else follows from that constraint: the reply is plain text rather
than guaranteed JSON, so the parser has to dig it out of whatever arrives.
"""

import unittest

import clients.gemini_research as gr
from clients.providers.base import ProviderBudgetExhausted, ProviderRateLimited
from clients.research_client import parse_research


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


def reply(text, prompt_tokens=500, output_tokens=800):
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": output_tokens,
            "totalTokenCount": prompt_tokens + output_tokens,
        },
    }


LEAD = {
    "title": "Software Engineer",
    "company": "Acme",
    "location": "Remote",
    "apply_url": "https://acme.test/jobs/1",
}

PAYLOAD = (
    '{"company_summary": "Acme builds widgets.", "products": ["Widget"], '
    '"tech_stack": ["Python"], "recent_news": [], '
    '"posting_keywords": ["python", "sqlite"], "culture_notes": [], '
    '"tailoring_advice": "Emphasise data plumbing."}'
)


class RequestShapeTests(unittest.TestCase):
    def client(self, response):
        calls = []

        def poster(url, **payload):
            calls.append((url, payload))
            return response

        client = gr.GeminiResearchClient(key="k", model="gemini-3.6-flash",
                                         poster=poster)
        return client, calls

    def test_grounding_is_sent_and_json_mode_is_not(self):
        """The constraint this whole module is shaped around.

        `tools` plus `responseMimeType` is rejected with HTTP 400 by the Gemini
        API. Research needs the tool, so it cannot have the response type.
        """
        client, calls = self.client(FakeResponse(200, reply(PAYLOAD)))
        client.research(LEAD)

        body = calls[0][1]["json"]
        self.assertEqual(body["tools"], [{"google_search": {}}])
        self.assertNotIn("responseMimeType", body["generationConfig"])
        self.assertNotIn("responseSchema", body["generationConfig"])

    def test_the_shared_system_prompt_is_used(self):
        from clients.research_client import RESEARCH_SYSTEM_PROMPT

        client, calls = self.client(FakeResponse(200, reply(PAYLOAD)))
        client.research(LEAD)
        body = calls[0][1]["json"]
        self.assertEqual(
            body["systemInstruction"]["parts"][0]["text"], RESEARCH_SYSTEM_PROMPT
        )

    def test_the_posting_url_reaches_the_prompt(self):
        """What authorises the model to read the posting itself."""
        client, calls = self.client(FakeResponse(200, reply(PAYLOAD)))
        client.research(LEAD)
        sent = calls[0][1]["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("https://acme.test/jobs/1", sent)

    def test_the_key_travels_in_a_header_not_the_url(self):
        client, calls = self.client(FakeResponse(200, reply(PAYLOAD)))
        client.research(LEAD)
        url, payload = calls[0]
        self.assertEqual(payload["headers"]["x-goog-api-key"], "k")
        self.assertNotIn("key=", url)

    def test_the_search_tool_name_is_configurable(self):
        """Models before 2.0 spell it google_search_retrieval."""
        import os

        os.environ["GEMINI_SEARCH_TOOL"] = "google_search_retrieval"
        self.addCleanup(os.environ.pop, "GEMINI_SEARCH_TOOL", None)
        client, calls = self.client(FakeResponse(200, reply(PAYLOAD)))
        client.research(LEAD)
        self.assertEqual(calls[0][1]["json"]["tools"],
                         [{"google_search_retrieval": {}}])


class ResponseTests(unittest.TestCase):
    def client(self, caller):
        return gr.GeminiResearchClient(key="k", caller=caller)

    def test_a_clean_payload_parses(self):
        client = self.client(lambda prompt: (PAYLOAD, 500, 800))
        payload, inp, out = client.research(LEAD)
        self.assertEqual(payload["company_summary"], "Acme builds widgets.")
        self.assertEqual(payload["posting_keywords"], ["python", "sqlite"])
        self.assertEqual((inp, out), (500, 800))

    def test_a_fenced_payload_parses(self):
        client = self.client(
            lambda prompt: (f"```json\n{PAYLOAD}\n```", 500, 800)
        )
        payload, _i, _o = client.research(LEAD)
        self.assertEqual(payload["company_summary"], "Acme builds widgets.")

    def test_a_payload_wrapped_in_prose_parses(self):
        """The realistic grounded-reply shape, since JSON mode is unavailable."""
        client = self.client(lambda prompt: (
            f"Here is what I found about Acme:\n\n{PAYLOAD}\n\nHope that helps.",
            500, 800,
        ))
        payload, _i, _o = client.research(LEAD)
        self.assertEqual(payload["company_summary"], "Acme builds widgets.")

    def test_unparseable_output_is_an_empty_payload_not_an_error(self):
        client = self.client(lambda prompt: ("I could not find anything.", 500, 20))
        payload, _i, out = client.research(LEAD)
        self.assertEqual(payload, {})
        self.assertEqual(out, 20)

    def test_usage_is_reported_even_when_the_reply_is_useless(self):
        """It still cost tokens; the spend ledger must see them."""
        client = self.client(lambda prompt: ("", 500, 0))
        _payload, inp, out = client.research(LEAD)
        self.assertEqual((inp, out), (500, 0))


class TransportResponseTests(unittest.TestCase):
    def client(self, response):
        return gr.GeminiResearchClient(
            key="k", poster=lambda url, **kw: response
        )

    def test_a_blocked_prompt_returns_nothing_usable(self):
        client = self.client(FakeResponse(200, {
            "promptFeedback": {"blockReason": "SAFETY"},
            "usageMetadata": {"promptTokenCount": 40},
        }))
        payload, inp, _out = client.research(LEAD)
        self.assertEqual(payload, {})
        self.assertEqual(inp, 40)

    def test_no_candidates_returns_nothing_usable(self):
        client = self.client(FakeResponse(200, {"candidates": []}))
        self.assertEqual(client.research(LEAD)[0], {})

    def test_a_rate_limit_carries_its_scope(self):
        client = self.client(FakeResponse(429, {"error": {"details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaId": "GenerateRequestsPerDayPerProject"}]},
        ]}}))
        with self.assertRaises(ProviderRateLimited) as caught:
            client.research(LEAD)
        self.assertEqual(caught.exception.scope, "day")
        self.assertEqual(caught.exception.provider, "Gemini")

    def test_other_errors_raise(self):
        client = self.client(FakeResponse(500, text="boom"))
        with self.assertRaises(RuntimeError):
            client.research(LEAD)

    def test_total_tokens_are_recorded_for_the_pool(self):
        client = self.client(FakeResponse(200, reply(PAYLOAD, 500, 800)))
        client.research(LEAD)
        self.assertEqual(client.last_total_tokens, 1300)


class LimiterTests(unittest.TestCase):
    def test_a_spend_ceiling_is_checked_before_the_call(self):
        class Spent:
            def check(self):
                raise ProviderBudgetExhausted("budget spent")

        called = []
        client = gr.GeminiResearchClient(
            key="k", caller=lambda p: called.append(p) or (PAYLOAD, 1, 1),
            limiter=Spent(),
        )
        with self.assertRaises(ProviderBudgetExhausted):
            client.research(LEAD)
        self.assertEqual(called, [], "the ceiling must be checked before spending")


class ParserHardeningTests(unittest.TestCase):
    """`parse_research` after the prose-tolerance change. Still never raises."""

    def test_prose_before_and_after(self):
        parsed = parse_research(f"Sure!\n{PAYLOAD}\nLet me know.")
        self.assertEqual(parsed["company_summary"], "Acme builds widgets.")

    def test_a_bare_fence_with_no_language_tag(self):
        parsed = parse_research(f"```\n{PAYLOAD}\n```")
        self.assertEqual(parsed["tech_stack"], ["Python"])

    def test_a_json_array_is_not_a_payload(self):
        self.assertEqual(parse_research('["not", "an", "object"]'), {})

    def test_empty_and_none(self):
        self.assertEqual(parse_research(""), {})
        self.assertEqual(parse_research(None), {})

    def test_prose_with_no_json_at_all(self):
        self.assertEqual(parse_research("I could not find anything useful."), {})

    def test_an_unclosed_brace_does_not_raise(self):
        self.assertEqual(parse_research('{"company_summary": "half'), {})

    def test_fields_are_still_clamped(self):
        """The tolerance must not weaken the validation behind it."""
        parsed = parse_research(
            '{"company_summary": "' + "x" * 5000 + '", '
            '"products": ' + str(["p"] * 50).replace("'", '"') + "}"
        )
        self.assertEqual(len(parsed["company_summary"]), 2000)
        self.assertEqual(len(parsed["products"]), 12)


if __name__ == "__main__":
    unittest.main()
