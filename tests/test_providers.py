"""The provider-neutral core: exception identity, and pacing queries.

The exception tests here look pedantic and are not. Six pipeline modules stop
their batch on `except GroqRateLimited`, and whether those catches also stop
for a second provider's rate limit comes down to `is` versus `issubclass`. Get
it wrong and a Gemini 429 escapes into a bare `except Exception`, logged as an
unexplained failure while the batch dies anyway. These pin the direction.
"""

import unittest

import clients.llm_client as llm
import clients.research_client as research
from clients.providers import base


class ExceptionAliasTests(unittest.TestCase):
    """The alias-not-subclass decision, pinned from both directions."""

    def test_groq_names_are_the_neutral_classes(self):
        self.assertIs(llm.GroqRateLimited, base.ProviderRateLimited)
        self.assertIs(llm.GroqNotConfigured, base.ProviderNotConfigured)

    def test_research_names_are_the_neutral_classes(self):
        self.assertIs(research.ResearchNotConfigured, base.ProviderNotConfigured)
        self.assertIs(research.SpendCeilingReached, base.ProviderBudgetExhausted)

    def test_existing_catches_stop_for_another_provider(self):
        """The whole point: a Gemini 429 must reach `except GroqRateLimited`."""
        try:
            raise base.ProviderRateLimited(
                "Gemini rate limit reached.",
                retry_after=27,
                provider="Gemini",
                scope="day",
            )
        except llm.GroqRateLimited as exc:
            self.assertEqual(exc.provider, "Gemini")
            self.assertEqual(exc.retry_after, 27)
            self.assertEqual(exc.scope, "day")
        else:  # pragma: no cover - the assert above either runs or the test fails
            self.fail("ProviderRateLimited was not caught by GroqRateLimited")

    def test_spend_ceiling_is_not_a_rate_limit(self):
        """`prepare.py` stops the whole stage on one and only this pass on the
        other, so collapsing them would rediscover the ceiling per lead."""
        self.assertNotIsInstance(
            base.ProviderBudgetExhausted("spent"), base.ProviderRateLimited
        )
        with self.assertRaises(base.ProviderBudgetExhausted):
            try:
                raise base.ProviderBudgetExhausted("spent")
            except base.ProviderRateLimited:  # pragma: no cover - must not catch
                self.fail("SpendCeilingReached was caught as a rate limit")

    def test_rate_limit_defaults_are_the_recoverable_reading(self):
        exc = base.ProviderRateLimited("x")
        self.assertEqual(exc.retry_after, 0)
        self.assertEqual(exc.provider, "")
        self.assertEqual(exc.scope, "minute")

    def test_unknown_scope_is_treated_as_recoverable(self):
        """An unrecognised scope must not read as a day-long lockout."""
        self.assertEqual(base.ProviderRateLimited("x", scope="week").scope, "minute")


class IntervalDelayTests(unittest.TestCase):
    """`interval_delay` must report exactly what `wait` would sleep."""

    def setUp(self):
        self.now = 0.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def pacer(self, per_minute=12):
        return base.Pacer(
            per_minute=per_minute,
            tokens_per_minute=10 ** 9,  # take the token rule out of the picture
            sleep=self.sleep,
            clock=self.clock,
        )

    def test_no_delay_before_the_first_call(self):
        self.assertEqual(self.pacer().interval_delay(self.now), 0.0)

    def test_matches_what_wait_sleeps(self):
        pacer = self.pacer(per_minute=12)  # one call every 5s
        pacer.wait(10)
        self.now += 2.0
        predicted = pacer.interval_delay(self.now)
        pacer.wait(10)
        self.assertAlmostEqual(predicted, 3.0)
        self.assertAlmostEqual(self.slept[-1], predicted)

    def test_zero_once_the_gap_has_elapsed(self):
        pacer = self.pacer(per_minute=12)
        pacer.wait(10)
        self.now += 6.0
        self.assertEqual(pacer.interval_delay(self.now), 0.0)

    def test_reporting_does_not_consume_the_gap(self):
        """Asking twice must answer the same; only `wait` advances anything."""
        pacer = self.pacer(per_minute=12)
        pacer.wait(10)
        self.now += 1.0
        first = pacer.interval_delay(self.now)
        self.assertEqual(first, pacer.interval_delay(self.now))
        self.assertAlmostEqual(first, 4.0)


class ReExportTests(unittest.TestCase):
    """`llm_client` must keep exposing what tests and callers already import."""

    def test_pacing_helpers_are_the_same_objects(self):
        self.assertIs(llm.Pacer, base.Pacer)
        self.assertIs(llm.estimate_tokens, base.estimate_tokens)
        self.assertIs(llm.retry_after_seconds, base.retry_after_seconds)

    def test_token_constants_survive_the_move(self):
        self.assertEqual(llm.TOKENS_PER_MINUTE, 12000)
        self.assertEqual(llm.CHARS_PER_TOKEN, 4)
        self.assertEqual(llm.ESTIMATED_TOKENS_PER_CALL, 900)


if __name__ == "__main__":
    unittest.main()
