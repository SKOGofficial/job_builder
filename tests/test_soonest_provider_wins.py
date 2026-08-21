"""A provider that must wait loses to one that can serve now.

The pool picked providers in chain order and treated "ready" as a yes/no. A
primary whose pacer said "wait 40 seconds" still counted as ready, so `_send`
slept out those 40 seconds while the fallback sat idle with a full day's
allowance - and the primary then often returned 429 anyway, so the call failed
over having already burned the wait.

Measured on a real mailbox before the fix: Groq had served 295 requests and was
rate limiting on nearly every call, Gemini showed 1 of 1200 used, and
classification advanced one or two messages per ten-minute cycle against a
backlog of 200. Settings had been promising the opposite the whole time - "a
provider that runs out hands the work to the next one rather than stopping".

The tie case matters as much as the fix: when nothing is paced, every delay is
0.0 and chain order must still decide, or the configured primary stops meaning
anything.
"""

import unittest

from clients.providers.routing import SHAPE_JSON
from tests.test_provider_pool import FakeClient, PoolFixture, builder


class Paced:
    """A pacer stand-in reporting a fixed delay."""

    def __init__(self, delay):
        self._delay = delay

    def interval_delay(self, _now):
        return self._delay

    def token_delay(self, _now, _projected):
        return 0.0

    def record(self, _tokens):
        pass


class TestSoonestWins(PoolFixture):
    def pool_with(self, groq_delay, gemini_delay):
        # The pacer hangs off the client - `ProviderState.pacer` reads through
        # to it, so one key's allowance is shared by all its endpoints.
        self.groq = FakeClient("llama-3.3", results=["groq"],
                               pacer=Paced(groq_delay))
        self.gemini = FakeClient("gemini-3.6-flash", results=["gemini"],
                                 pacer=Paced(gemini_delay))
        return self.pool({
            "groq": builder(self.groq, "Groq"),
            "gemini": builder(self.gemini, "Gemini"),
        })

    def order(self, pool):
        ready, _blocked = pool.candidates(
            "route_email", SHAPE_JSON, projected_tokens=1000,
            budget_s=45.0, now=self.now)
        return [state.name for state in ready]

    def test_a_paced_primary_loses_to_an_idle_fallback(self):
        """The bug, stated directly."""
        pool = self.pool_with(groq_delay=40.0, gemini_delay=0.0)
        self.assertEqual(self.order(pool), ["gemini", "groq"])

    def test_the_idle_fallback_actually_serves_the_call(self):
        pool = self.pool_with(groq_delay=40.0, gemini_delay=0.0)
        client = pool.for_task("route_email", max_wait=45.0)

        result = client.complete_json(
            [{"role": "user", "content": "hi"}], lambda t: t, "fallback")

        self.assertEqual(result, "gemini")
        self.assertEqual(self.groq.calls, 0, "the paced provider is not called")
        self.assertEqual(self.slept, [], "and nothing waits on its behalf")

    def test_chain_order_still_decides_when_nothing_is_paced(self):
        """The ordinary case must be untouched: the primary still wins."""
        pool = self.pool_with(groq_delay=0.0, gemini_delay=0.0)
        self.assertEqual(self.order(pool), ["groq", "gemini"])

        client = pool.for_task("route_email", max_wait=45.0)
        self.assertEqual(
            client.complete_json([{"role": "user", "content": "hi"}],
                                 lambda t: t, "fallback"),
            "groq",
        )
        self.assertEqual(self.gemini.calls, 0)

    def test_the_less_paced_of_two_paced_providers_wins(self):
        pool = self.pool_with(groq_delay=30.0, gemini_delay=5.0)
        self.assertEqual(self.order(pool), ["gemini", "groq"])

    def test_a_primary_paced_only_slightly_still_wins_on_tie(self):
        pool = self.pool_with(groq_delay=5.0, gemini_delay=5.0)
        self.assertEqual(self.order(pool), ["groq", "gemini"])

    def test_pacing_past_the_budget_still_blocks_entirely(self):
        """Sorting must not resurrect a provider the budget rules out."""
        pool = self.pool_with(groq_delay=90.0, gemini_delay=0.0)
        ready, blocked = pool.candidates(
            "route_email", SHAPE_JSON, projected_tokens=1000,
            budget_s=45.0, now=self.now)
        self.assertEqual([state.name for state in ready], ["gemini"])
        self.assertIn("groq", [name for name, _reason, _seconds in blocked])


if __name__ == "__main__":
    unittest.main()
