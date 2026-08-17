"""Routing, budgets, and the failover state machine.

The load-bearing claim this file defends is that a rate limit on one provider
moves work to another *without* any pipeline stage knowing that happened - and
that when nothing can take the work, the stage still stops the way it always
did, keeping everything already written. So the last class here drives the real
`AlertHandler.run`, not a mock of it.
"""

import asyncio
import os
import unittest

from clients.providers import routing
from clients.providers.base import (
    ProviderBudgetExhausted,
    ProviderNotConfigured,
    ProviderRateLimited,
)
from clients.providers.budget import Budget
from clients.providers.pool import LOOP_MAX_WAIT, THREAD_MAX_WAIT, ProviderPool
from clients.providers.routing import SHAPE_JSON, SHAPE_RESEARCH
from utilities.mailstore import MailStore
from utilities.store import JobStore


class FakeClient:
    """A transport stand-in: scripted results, and a record of what it saw."""

    def __init__(self, model="fake-1", results=None, error=None, pacer=None):
        self.model = model
        self.pacer = pacer
        self.results = list(results or [])
        self.error = error
        self.calls = 0
        self.last_total_tokens = 100

    @property
    def last_model(self):
        return self.model

    def complete_json(self, messages, parser, fallback, max_tokens=200):
        # Scripted results are model *text*, handed to the caller's parser
        # exactly as a real transport would. Tests that only care about
        # plumbing pass `lambda t: t` and get their string back; tests driving
        # a real stage get that stage's own validation applied.
        self.calls += 1
        if self.error is not None:
            raise self.error
        if not self.results:
            return fallback
        return parser(self.results.pop(0))

    def research(self, lead):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ({"summary": "ok"}, 10, 20)


def builder(client, display, shapes=frozenset({SHAPE_JSON}), daily_limit=0,
            clients=None):
    """A pool builder function returning fixed clients.

    Takes either one client plus the shapes it serves, or an explicit
    shape-to-client mapping for a provider whose halves differ - which is how
    Gemini really works, since grounded research and JSON-mode classification
    cannot share a request body.
    """
    if client is None and clients is None:
        def build(_mail):
            raise ProviderNotConfigured(f"{display} has no key")
    else:
        mapping = clients if clients is not None else {s: client for s in shapes}

        def build(_mail):
            return mapping, display, daily_limit
    return build


class PoolFixture(unittest.TestCase):
    """A pool driven by an injected clock, so nothing ever really sleeps."""

    def setUp(self):
        self.now = 1000.0
        self.slept = []
        self.store = JobStore(":memory:")
        self.mail = MailStore(self.store.conn)
        self.addCleanup(self.store.conn.close)
        self.addCleanup(self._clear_route_env)
        self._clear_route_env()

    def _clear_route_env(self):
        for name in list(os.environ):
            if name.startswith("LLM_ROUTE_") or name == "LLM_DEFAULT_PROVIDER":
                os.environ.pop(name, None)

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def pool(self, builders, mail=None, names=None):
        return ProviderPool(
            mail=self.mail if mail is None else mail,
            names=names if names is not None else list(builders),
            builders=builders,
            clock=self.clock,
            sleep=self.sleep,
        )

    def two_providers(self, groq_error=None, gemini_error=None,
                      gemini_daily=0):
        self.groq = FakeClient("llama-3.3", error=groq_error)
        self.gemini = FakeClient("gemini-3.6-flash", error=gemini_error)
        return self.pool({
            "groq": builder(self.groq, "Groq"),
            "gemini": builder(self.gemini, "Gemini", daily_limit=gemini_daily),
        })


class RoutingTests(PoolFixture):
    def test_default_chain_is_used_when_nothing_is_saved(self):
        pool = self.two_providers()
        self.assertEqual(pool.chain("route_email"), ("groq", "gemini"))

    def test_env_overrides_the_default(self):
        os.environ["LLM_ROUTE_ROUTE_EMAIL"] = "gemini,groq"
        pool = self.two_providers()
        self.assertEqual(pool.chain("route_email"), ("gemini", "groq"))

    def test_saved_routing_overrides_the_env(self):
        os.environ["LLM_ROUTE_ROUTE_EMAIL"] = "gemini,groq"
        self.mail.set_provider_route("route_email", "groq", None)
        pool = self.two_providers()
        self.assertEqual(pool.chain("route_email"), ("groq",))

    def test_clearing_a_route_restores_the_env_chain(self):
        os.environ["LLM_ROUTE_ROUTE_EMAIL"] = "gemini,groq"
        self.mail.set_provider_route("route_email", "groq", None)
        pool = self.two_providers()
        self.mail.clear_provider_route("route_email")
        pool.reload_routes()
        self.assertEqual(pool.chain("route_email"), ("gemini", "groq"))

    def test_a_task_can_be_switched_off(self):
        os.environ["LLM_ROUTE_SCORE_RELEVANCE"] = "none"
        pool = self.two_providers()
        self.assertEqual(pool.chain("score_relevance"), ())
        self.assertIsNone(pool.for_task("score_relevance"))

    def test_gap_filling_routes_with_alert_extraction(self):
        """They share one injected client, so they cannot route apart."""
        self.mail.set_provider_route("extract_alert", "gemini", "groq")
        pool = self.two_providers()
        self.assertEqual(pool.chain("complete_posting"), ("gemini", "groq"))

    def test_duplicates_cost_one_attempt_not_two(self):
        self.mail.set_provider_route("route_email", "groq", "groq")
        pool = self.two_providers()
        self.assertEqual(pool.chain("route_email"), ("groq",))

    def test_an_unknown_task_is_refused_rather_than_defaulted(self):
        with self.assertRaises(KeyError):
            routing.chain_for("not_a_task")

    def test_unconfigured_chain_yields_none_not_an_exception(self):
        pool = self.pool({"groq": builder(None, "Groq")})
        self.assertIsNone(pool.for_task("route_email"))

    def test_shape_decides_the_client_returned(self):
        pool = self.pool({
            "groq": builder(FakeClient(), "Groq"),
            "gemini": builder(FakeClient(), "Gemini"),
            "anthropic": builder(FakeClient("claude"), "Claude",
                                 shapes=frozenset({SHAPE_RESEARCH})),
        })
        self.assertTrue(hasattr(pool.for_task("route_email"), "complete_json"))
        self.assertTrue(hasattr(pool.for_task("research"), "research"))
        self.assertFalse(hasattr(pool.for_task("research"), "complete_json"))


class FailoverTests(PoolFixture):
    def call(self, pool, task="route_email"):
        client = pool.for_task(task, max_wait=THREAD_MAX_WAIT)
        return client.complete_json(
            [{"role": "user", "content": "hi"}], lambda t: t, "fallback"
        )

    def test_the_primary_serves_and_the_fallback_is_untouched(self):
        pool = self.two_providers()
        self.groq.results = ["ok"]
        self.assertEqual(self.call(pool), "ok")
        self.assertEqual(self.gemini.calls, 0)

    def test_a_rate_limit_moves_the_call_to_the_fallback(self):
        pool = self.two_providers(
            groq_error=ProviderRateLimited("busy", retry_after=30,
                                           provider="Groq")
        )
        self.gemini.results = ["from-gemini"]
        self.assertEqual(self.call(pool), "from-gemini")
        self.assertEqual(self.gemini.calls, 1)

    def test_the_refused_provider_is_skipped_for_the_rest_of_the_cycle(self):
        pool = self.two_providers(
            groq_error=ProviderRateLimited("busy", retry_after=30,
                                           provider="Groq")
        )
        self.gemini.results = ["a", "b"]
        self.call(pool)
        self.assertEqual(self.groq.calls, 1)
        self.call(pool)
        self.assertEqual(self.groq.calls, 1, "cooling down, must not be retried")
        self.assertEqual(self.gemini.calls, 2)

    def test_a_cooldown_expires(self):
        pool = self.two_providers(
            groq_error=ProviderRateLimited("busy", retry_after=30,
                                           provider="Groq")
        )
        self.gemini.results = ["a"]
        self.call(pool)
        self.now += 31
        self.groq.error = None
        self.groq.results = ["back"]
        self.assertEqual(self.call(pool), "back")

    def test_a_fallback_with_no_daily_headroom_is_never_called(self):
        """The rule you asked for: only switch to a model that can take it."""
        pool = self.two_providers(
            groq_error=ProviderRateLimited("busy", retry_after=30,
                                           provider="Groq"),
            gemini_daily=5,
        )
        pool.providers["gemini"].budget.seed(5)
        with self.assertRaises(ProviderRateLimited):
            self.call(pool)
        self.assertEqual(self.gemini.calls, 0)

    def test_both_out_raises_and_names_when_things_free_up(self):
        pool = self.two_providers(
            groq_error=ProviderRateLimited("busy", retry_after=90,
                                           provider="Groq"),
            gemini_error=ProviderRateLimited("busy", retry_after=45,
                                             provider="Gemini"),
        )
        with self.assertRaises(ProviderRateLimited) as caught:
            self.call(pool)
        self.assertEqual(caught.exception.retry_after, 45)

    def test_a_day_scoped_denial_is_written_down(self):
        """So a restart cannot un-exhaust the cap."""
        pool = self.two_providers(
            gemini_error=ProviderRateLimited("out", retry_after=60,
                                             provider="Gemini", scope="day"),
            gemini_daily=100,
        )
        self.mail.set_provider_route("route_email", "gemini", "groq")
        pool.reload_routes()
        self.groq.results = ["ok"]
        self.call(pool)
        pool.flush()

        rows = self.store.conn.execute(
            "SELECT provider, outcome FROM provider_usage ORDER BY id"
        ).fetchall()
        self.assertEqual([(r["provider"], r["outcome"]) for r in rows],
                         [("gemini", "denied_day"), ("groq", "ok")])

    def test_attribution_names_the_model_that_actually_served(self):
        pool = self.two_providers(
            groq_error=ProviderRateLimited("busy", retry_after=30,
                                           provider="Groq")
        )
        self.gemini.results = ["x"]
        client = pool.for_task("route_email", max_wait=THREAD_MAX_WAIT)
        self.call(pool)
        self.assertEqual(client.last_model, "gemini-3.6-flash")
        self.assertEqual(client.provider, "gemini")

    def test_default_provider_takes_the_call_when_the_chain_will_not(self):
        """"Default to groq and wait" - here the wait is zero."""
        os.environ["LLM_ROUTE_ROUTE_EMAIL"] = "gemini"
        pool = self.two_providers(
            gemini_error=ProviderRateLimited("busy", retry_after=30,
                                             provider="Gemini")
        )
        self.groq.results = ["rescued"]
        self.assertEqual(self.call(pool), "rescued")

    def test_an_unconfigured_provider_mid_chain_is_stepped_over(self):
        pool = self.pool({
            "groq": builder(None, "Groq"),
            "gemini": builder(FakeClient("gemini-3.6-flash",
                                         results=["only-one-left"]), "Gemini"),
        })
        self.assertEqual(self.call(pool), "only-one-left")


class TaskAvailabilityTests(PoolFixture):
    """`next_available_for`, which exists because the pool-wide check lies.

    The state reproduced here is the one from the logs: Groq healthy and
    serving every JSON task, while research - which routes only to Gemini and
    Anthropic - has nowhere to go. `next_available_in` reports 0.0 throughout,
    correctly, because something *is* alive. Only the per-task question sees
    the starvation.
    """

    def research_pool(self, gemini_daily=0, anthropic=None):
        self.groq = FakeClient("llama-3.3")
        self.gemini = FakeClient("gemini-3.6-flash")
        return self.pool({
            "groq": builder(self.groq, "Groq"),
            "gemini": builder(self.gemini, "Gemini",
                              shapes=frozenset({SHAPE_JSON, SHAPE_RESEARCH}),
                              daily_limit=gemini_daily),
            "anthropic": builder(anthropic, "Claude",
                                 shapes=frozenset({SHAPE_RESEARCH})),
        })

    def test_a_ready_chain_is_no_wait(self):
        pool = self.research_pool()
        self.assertEqual(pool.next_available_for("research"), 0.0)

    def test_the_starved_task_reports_a_wait_while_the_pool_looks_healthy(self):
        pool = self.research_pool(gemini_daily=5)
        pool.providers["gemini"].budget.seed(5)

        self.assertGreater(pool.next_available_for("research"), 0)
        # The whole point: the pool-wide check cannot see this.
        self.assertEqual(pool.next_available_in(), 0.0)
        self.assertEqual(pool.next_available_for("route_email"), 0.0)

    def test_a_cooldown_is_reported_as_the_wait(self):
        pool = self.research_pool()
        pool.providers["gemini"].cool_down(self.now + 40, "429")
        self.assertAlmostEqual(pool.next_available_for("research"), 40)

    def test_the_soonest_blocker_wins(self):
        pool = self.research_pool(anthropic=FakeClient("claude"))
        pool.providers["gemini"].cool_down(self.now + 90, "429")
        pool.providers["anthropic"].cool_down(self.now + 25, "429")
        self.assertAlmostEqual(pool.next_available_for("research"), 25)

    def test_an_unconfigured_chain_is_not_a_wait(self):
        """"Cannot ever" is not a wait - the caller handles an absent client."""
        pool = self.pool({
            "groq": builder(FakeClient("llama-3.3"), "Groq"),
            "gemini": builder(None, "Gemini"),
            "anthropic": builder(None, "Claude"),
        })
        self.assertEqual(pool.next_available_for("research"), 0.0)
        self.assertIsNone(pool.for_task("research"))

    def test_a_shape_the_provider_cannot_serve_is_not_a_wait(self):
        """Groq is JSON-only, so it is permanently blocked, not delayed."""
        os.environ["LLM_ROUTE_RESEARCH"] = "groq"
        pool = self.research_pool()
        self.assertEqual(pool.next_available_for("research"), 0.0)

    def test_the_client_asks_on_the_stage_s_behalf(self):
        pool = self.research_pool(gemini_daily=5)
        pool.providers["gemini"].budget.seed(5)
        client = pool.for_task("research", max_wait=THREAD_MAX_WAIT)
        self.assertEqual(client.available_in(),
                         pool.next_available_for("research"))
        self.assertGreater(client.available_in(), 0)

    def test_json_clients_can_be_asked_too(self):
        pool = self.research_pool()
        self.assertEqual(
            pool.for_task("route_email", max_wait=THREAD_MAX_WAIT).available_in(),
            0.0,
        )


class SleepBudgetTests(PoolFixture):
    def test_an_explicit_budget_wins(self):
        pool = self.two_providers()
        self.assertEqual(pool.sleep_budget(max_wait=7.5), 7.5)

    def test_the_loop_thread_gets_the_short_budget(self):
        """Tests run on the main thread, which is where the event loop lives."""
        pool = self.two_providers()
        self.assertEqual(pool.sleep_budget(), LOOP_MAX_WAIT)

    def test_a_pacing_delay_over_the_budget_fails_over_rather_than_sleeping(self):
        gemini = FakeClient("gemini-3.6-flash", results=["fast-path"])
        groq = FakeClient("llama-3.3", results=["slow-path"])
        pool = self.pool({
            "groq": builder(groq, "Groq", daily_limit=10),
            "gemini": builder(gemini, "Gemini"),
        })
        # Groq is 80% through a 10-request day, so its spread interval is
        # thousands of seconds - far past any sleep budget.
        pool.providers["groq"].budget.seed(8)
        self.assertGreater(pool.providers["groq"].budget.spread_delay(self.now),
                           THREAD_MAX_WAIT)

        client = pool.for_task("route_email", max_wait=THREAD_MAX_WAIT)
        result = client.complete_json([{"role": "user", "content": "x"}],
                                      lambda t: t, "f")
        self.assertEqual(result, "fast-path")
        self.assertEqual(self.slept, [], "failover must not sleep")
        self.assertEqual(groq.calls, 0)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0

    def budget(self, limit=100, reserve=0.5):
        return Budget(daily_limit=limit, reserve=reserve, clock=lambda: self.now)

    def test_no_limit_means_no_ceiling_and_no_spread(self):
        budget = self.budget(limit=0)
        self.assertTrue(budget.has_headroom(0.0))
        self.assertEqual(budget.spread_delay(0.0), 0.0)
        self.assertIsNone(budget.remaining())

    def test_bursts_pass_freely_below_the_reserve(self):
        budget = self.budget(limit=100)
        budget.seed(40)
        self.assertEqual(budget.spread_delay(0.0), 0.0)

    def test_pacing_engages_past_the_reserve_and_ramps(self):
        budget = self.budget(limit=100)
        budget.seed(60)
        early = budget.spread_delay(0.0)
        budget.seed(90)
        late = budget.spread_delay(0.0)
        self.assertGreater(early, 0.0)
        self.assertGreater(late, early)

    def test_the_ceiling_refuses_rather_than_pacing(self):
        budget = self.budget(limit=10)
        budget.seed(10)
        self.assertFalse(budget.has_headroom(0.0))
        self.assertEqual(budget.remaining(), 0)

    def test_booking_counts_toward_the_ceiling(self):
        budget = self.budget(limit=2)
        budget.book()
        self.assertTrue(budget.has_headroom(0.0))
        budget.book()
        self.assertFalse(budget.has_headroom(0.0))

    def test_a_day_denial_closes_the_budget_regardless_of_the_count(self):
        """The provider's own refusal outranks our arithmetic."""
        budget = self.budget(limit=1000)
        budget.deny("day", now=0.0)
        self.assertFalse(budget.has_headroom(0.0))

    def test_a_minute_denial_does_not_close_the_day(self):
        budget = self.budget(limit=1000)
        budget.deny("minute", now=0.0)
        self.assertTrue(budget.has_headroom(0.0))

    def test_a_seeded_denial_survives_into_a_new_budget(self):
        budget = self.budget(limit=1000)
        budget.seed(0, denied_day=True, now=0.0)
        self.assertFalse(budget.has_headroom(0.0))


class LedgerTests(PoolFixture):
    def test_usage_is_flushed_once_per_cycle(self):
        pool = self.two_providers()
        self.groq.results = ["a", "b"]
        client = pool.for_task("route_email", max_wait=THREAD_MAX_WAIT)
        for _ in range(2):
            client.complete_json([{"role": "user", "content": "x"}], lambda t: t, "f")

        self.assertEqual(
            self.store.conn.execute("SELECT COUNT(*) c FROM provider_usage")
            .fetchone()["c"], 0, "nothing should be written before the flush",
        )
        self.assertEqual(pool.flush(), 2)
        rows = self.store.conn.execute(
            "SELECT provider, task, model, outcome, total_tokens FROM provider_usage"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["provider"], "groq")
        self.assertEqual(rows[0]["task"], "route_email")
        self.assertEqual(rows[0]["model"], "llama-3.3")
        self.assertEqual(rows[0]["outcome"], "ok")
        self.assertEqual(rows[0]["total_tokens"], 100)

    def test_begin_cycle_seeds_from_what_was_persisted(self):
        """Restart survival: a fresh pool must know the day is spent."""
        self.mail.record_provider_usage([
            {"provider": "gemini", "task": "route_email", "outcome": "ok"}
            for _ in range(7)
        ])
        pool = self.two_providers(gemini_daily=10)
        pool.begin_cycle()
        self.assertEqual(pool.providers["gemini"].budget.requests_today, 7)
        self.assertEqual(pool.providers["gemini"].budget.remaining(), 3)

    def test_a_persisted_day_denial_is_re_applied_on_restart(self):
        self.mail.record_provider_usage([
            {"provider": "gemini", "task": "route_email", "outcome": "denied_day"}
        ])
        pool = self.two_providers(gemini_daily=1000)
        pool.begin_cycle()
        self.assertFalse(pool.providers["gemini"].budget.has_headroom(self.now))

    def test_a_failing_flush_does_not_fail_the_cycle(self):
        pool = self.two_providers()
        self.groq.results = ["a"]
        client = pool.for_task("route_email", max_wait=THREAD_MAX_WAIT)
        client.complete_json([{"role": "user", "content": "x"}], lambda t: t, "f")

        def boom(_rows):
            raise RuntimeError("disk is on fire")

        pool.ledger.flush = boom
        self.assertEqual(pool.flush(), 0)

    def test_flushing_clears_the_queue(self):
        pool = self.two_providers()
        self.groq.results = ["a"]
        client = pool.for_task("route_email", max_wait=THREAD_MAX_WAIT)
        client.complete_json([{"role": "user", "content": "x"}], lambda t: t, "f")
        pool.flush()
        self.assertEqual(pool.flush(), 0)


class ResearchFailoverTests(PoolFixture):
    def research_pool(self, gemini_error=None, anthropic_error=None):
        self.gemini = FakeClient("gemini-3.6-flash", error=gemini_error)
        self.claude = FakeClient("claude-opus-5", error=anthropic_error)
        return self.pool({
            "gemini": builder(self.gemini, "Gemini",
                              shapes=frozenset({SHAPE_RESEARCH})),
            "anthropic": builder(self.claude, "Claude",
                                 shapes=frozenset({SHAPE_RESEARCH})),
        })

    def test_gemini_is_primary_for_research(self):
        pool = self.research_pool()
        pool.for_task("research", max_wait=THREAD_MAX_WAIT).research({"id": 1})
        self.assertEqual(self.gemini.calls, 1)
        self.assertEqual(self.claude.calls, 0)

    def test_a_spend_ceiling_moves_research_to_the_other_provider(self):
        pool = self.research_pool(
            gemini_error=ProviderBudgetExhausted("daily research budget spent")
        )
        payload, _inp, _out = pool.for_task(
            "research", max_wait=THREAD_MAX_WAIT
        ).research({"id": 1})
        self.assertEqual(payload, {"summary": "ok"})
        self.assertEqual(self.claude.calls, 1)

    def test_both_exhausted_raises_the_ceiling_not_a_rate_limit(self):
        """`prepare.py` reads the two differently: stop the stage vs this pass."""
        pool = self.research_pool(
            gemini_error=ProviderBudgetExhausted("spent"),
            anthropic_error=ProviderBudgetExhausted("spent"),
        )
        with self.assertRaises(ProviderBudgetExhausted):
            pool.for_task("research", max_wait=THREAD_MAX_WAIT).research({"id": 1})


class StatusTests(PoolFixture):
    def test_an_unconfigured_provider_still_appears(self):
        """Settings must be able to say "Gemini: no key", not omit Gemini."""
        pool = self.pool({
            "groq": builder(FakeClient(), "Groq"),
            "gemini": builder(None, "Gemini"),
        })
        names = {row["name"]: row for row in pool.status()}
        self.assertIn("gemini", names)
        self.assertFalse(names["gemini"]["configured"])
        self.assertIn("no key", names["gemini"]["last_error"])

    def test_the_signature_changes_when_a_provider_starts_cooling(self):
        pool = self.two_providers(
            groq_error=ProviderRateLimited("busy", retry_after=30,
                                           provider="Groq")
        )
        before = pool.signature()
        self.gemini.results = ["x"]
        pool.for_task("route_email", max_wait=THREAD_MAX_WAIT).complete_json(
            [{"role": "user", "content": "x"}], lambda t: t, "f"
        )
        self.assertNotEqual(before, pool.signature())

    def test_status_reports_the_daily_allowance(self):
        pool = self.two_providers(gemini_daily=200)
        pool.providers["gemini"].budget.seed(37)
        row = {r["name"]: r for r in pool.status()}["gemini"]
        self.assertEqual(row["used"], 37)
        self.assertEqual(row["limit"], 200)
        self.assertEqual(row["remaining"], 163)


class RealHandlerTests(PoolFixture):
    """The payoff, driven through a real pipeline stage.

    `AlertHandler` has no idea a pool exists. It catches `GroqRateLimited` and
    breaks. If the aliasing or the pool's exception type were wrong, the handler
    would either not stop or would lose the leads it had already written.
    """

    def alert(self, message_id):
        self.mail.upsert_message({
            "id": message_id,
            "thread_id": "t1",
            "sender": "alerts@board.test",
            "subject": "Jobs for you",
            "date": "Tue, 20 Jan 2026 10:00:00 -0400",
            "snippet": "roles",
        })
        self.mail.store_body(message_id, "Software Engineer at Acme. Apply now.",
                            "roles")
        self.mail.record_category(message_id, "job_alert", 0.9, "digest")
        self.mail.commit()

    def test_a_stage_stops_cleanly_and_keeps_what_it_wrote(self):
        from pipeline.alerts import AlertHandler

        self.alert("m1")
        self.alert("m2")

        posting = (
            '{"postings": [{"title": "Software Engineer", "company": "Acme", '
            '"location": "Remote", "url": "https://acme.test/1"}]}'
        )
        # First alert succeeds, then both providers refuse.
        groq = FakeClient("llama-3.3", results=[posting])
        pool = self.pool({"groq": builder(groq, "Groq")})

        handler = AlertHandler(
            self.store, self.mail,
            pool.for_task("extract_alert", max_wait=THREAD_MAX_WAIT),
        )
        created, _skipped, _linked = asyncio.run(handler.run(5))
        self.assertGreaterEqual(created, 1, "the first alert must produce a lead")

        groq.error = ProviderRateLimited("busy", retry_after=30, provider="Groq")
        before = self.mail.conn.execute(
            "SELECT COUNT(*) c FROM job_leads").fetchone()["c"]
        created_again, _s, _l = asyncio.run(AlertHandler(
            self.store, self.mail,
            pool.for_task("extract_alert", max_wait=THREAD_MAX_WAIT),
        ).run(5))
        after = self.mail.conn.execute(
            "SELECT COUNT(*) c FROM job_leads").fetchone()["c"]

        self.assertEqual(created_again, 0)
        self.assertEqual(before, after, "a rate limit must not lose written leads")

    def test_a_stage_never_sleeps_past_the_loop_budget(self):
        from pipeline.alerts import AlertHandler

        self.alert("m1")
        groq = FakeClient("llama-3.3", error=ProviderRateLimited(
            "busy", retry_after=600, provider="Groq"))
        pool = self.pool({"groq": builder(groq, "Groq")})
        asyncio.run(AlertHandler(
            self.store, self.mail,
            pool.for_task("extract_alert", max_wait=LOOP_MAX_WAIT),
        ).run(5))
        self.assertTrue(all(s <= LOOP_MAX_WAIT for s in self.slept),
                        f"slept past the loop budget: {self.slept}")


class AttributionThroughRouterTests(PoolFixture):
    """The model name reaching `record_category` through the real router."""

    def message(self, message_id):
        self.mail.upsert_message({
            "id": message_id, "thread_id": "t1", "sender": "a@b.test",
            "subject": "Update", "date": "Tue, 20 Jan 2026 10:00:00 -0400",
            "snippet": "hi",
        })
        self.mail.store_body(message_id, "We received your application.", "hi")
        self.mail.commit()

    async def _run_router(self, client):
        from pipeline.router import MessageRouter

        async def immediate(func, *args):
            return func(*args)

        return await MessageRouter(
            self.mail, client_factory=lambda: client, executor=immediate
        ).run(5)

    def test_the_serving_model_is_recorded(self):
        import asyncio

        self.message("m1")
        route = (
            '{"category": "job_acknowledgement", "confidence": 0.9, '
            '"reason": "receipt"}'
        )
        groq = FakeClient("llama-3.3", results=[route])
        pool = self.pool({"groq": builder(groq, "Groq")})
        asyncio.run(self._run_router(
            pool.for_task("route_email", max_wait=THREAD_MAX_WAIT)
        ))
        self.assertEqual(self.mail.message("m1")["category_model"], "llama-3.3")

    def test_a_stub_without_a_model_records_null(self):
        import asyncio

        class Bare:
            def complete_json(self, messages, parser, fallback, max_tokens=200):
                return parser(
                    '{"category": "irrelevant", "confidence": 0.9, "reason": "x"}'
                )

        self.message("m1")
        asyncio.run(self._run_router(Bare()))
        self.assertIsNone(self.mail.message("m1")["category_model"])


class NextAvailableTests(PoolFixture):
    """When the pool can next take a model call at all.

    The question a caller asks before deciding whether to attempt the model
    stages, rather than letting five of them each rediscover the same cooldown.
    """

    def test_zero_while_a_provider_is_ready(self):
        pool = self.two_providers()
        self.assertEqual(pool.next_available_in(), 0.0)

    def test_zero_when_only_one_of_two_is_cooling(self):
        """Failover is the point: one provider down is not the pipeline down."""
        pool = self.two_providers()
        pool.providers["groq"].cool_down(self.now + 300)
        self.assertEqual(pool.next_available_in(), 0.0)

    def test_the_soonest_wait_when_every_provider_is_cooling(self):
        pool = self.two_providers()
        pool.providers["groq"].cool_down(self.now + 900)
        pool.providers["gemini"].cool_down(self.now + 120)
        self.assertEqual(pool.next_available_in(), 120)

    def test_a_spent_daily_budget_counts_as_unavailable(self):
        """Not a cooldown, but just as much a reason not to try."""
        pool = self.two_providers(gemini_daily=1)
        pool.providers["groq"].cool_down(self.now + 600)
        pool.providers["gemini"].budget.deny("day", self.now)
        self.assertGreater(pool.next_available_in(), 0)

    def test_nothing_configured_is_not_a_wait(self):
        # "Cannot ever" is not "not yet"; the caller's own None handling covers
        # it, and reporting a wait here would promise a recovery that is not
        # coming.
        pool = self.pool({"groq": builder(None, "Groq")})
        self.assertEqual(pool.next_available_in(), 0.0)


class ModelStageSkipTests(PoolFixture):
    """Skipping the model stages as a group while nothing can serve them."""

    def cycle(self, pool):
        from pipeline.orchestrator import PipelineCycle

        return PipelineCycle(
            self.store, self.mail, client_factory=lambda: pool
        )

    def test_stages_are_skipped_and_the_wait_reported(self):
        pool = self.two_providers()
        for state in pool.providers.values():
            state.cool_down(self.now + 1800)

        result = asyncio.run(self.cycle(pool)._model_stages(pool))

        self.assertEqual(result["retry_after"], 1800)
        self.assertEqual(result["handled"], {})
        # Classification is deliberately not here any more: the rule tier needs
        # no provider, so it runs from `PipelineCycle.run` whatever the pool is
        # doing. See `PipelineCycle.classify`.
        self.assertNotIn("classified", result)
        self.assertEqual(self.groq.calls, 0, "nothing may be sent while cooling")
        self.assertEqual(self.gemini.calls, 0)

    def test_no_wait_is_reported_when_a_provider_is_ready(self):
        pool = self.two_providers()
        result = asyncio.run(self.cycle(pool)._model_stages(pool))
        self.assertNotIn("retry_after", result)

    def test_the_summary_says_why_nothing_was_classified(self):
        from pipeline.orchestrator import _summarise

        self.assertIn("waiting 30m for a model",
                      _summarise({"synced": 3, "retry_after": 1800}))
        self.assertIn("3 new message(s)",
                      _summarise({"synced": 3, "retry_after": 1800}))


class OrchestratorWiringTests(PoolFixture):
    """`PipelineCycle` handing each stage a client bound to its own task.

    The stages themselves are covered elsewhere; what is new here is that they
    now receive task-bound views rather than one shared client, and that the
    cycle seeds and flushes the ledger around them.
    """

    def cycle(self, pool):
        from pipeline.orchestrator import PipelineCycle

        return PipelineCycle(
            self.store, self.mail, client_factory=lambda: pool
        )

    def test_each_stage_gets_a_client_bound_to_its_own_task(self):
        pool = self.two_providers()
        seen = []
        original = pool.for_task

        def spy(task, max_wait=None):
            seen.append((task, max_wait))
            return original(task, max_wait=max_wait)

        pool.for_task = spy
        asyncio.run(self.cycle(pool).dispatch(pool))

        tasks = [task for task, _wait in seen]
        self.assertEqual(
            tasks,
            ["extract_alert", "extract_acknowledgement", "extract_update"],
        )

    def test_handler_stages_get_the_long_sleep_budget(self):
        """They put their model calls on an executor, so waiting is free.

        These stages used to run inline on the event loop, which is why they
        took `LOOP_MAX_WAIT`: a long sleep there froze the UI. They are awaited
        now and each handler offloads its call, so a wait costs the interface
        nothing - and capping them at two seconds would make the pool fail over
        to a second provider, or give up, for an ordinary pacing gap.
        """
        pool = self.two_providers()
        seen = []
        original = pool.for_task

        def spy(task, max_wait=None):
            seen.append(max_wait)
            return original(task, max_wait=max_wait)

        pool.for_task = spy
        asyncio.run(self.cycle(pool).dispatch(pool))
        self.assertTrue(all(wait == THREAD_MAX_WAIT for wait in seen), seen)

    def test_the_pool_is_built_once_and_kept(self):
        """A cooldown must outlive the cycle that earned it."""
        pool = self.two_providers()
        cycle = self.cycle(pool)
        self.assertIs(cycle._pool(), cycle._pool())

    def test_an_unconfigured_pool_degrades_rather_than_raising(self):
        from pipeline.orchestrator import PipelineCycle

        cycle = PipelineCycle(
            self.store, self.mail,
            client_factory=lambda: (_ for _ in ()).throw(RuntimeError("no keys")),
        )
        self.assertIsNone(cycle._pool())

    def test_a_cycle_flushes_what_its_stages_spent(self):
        import asyncio

        pool = self.two_providers()
        self.groq.results = ['{"postings": []}'] * 3
        cycle = self.cycle(pool)
        # Skip the Gmail half; this is about the model stages and the ledger.
        cycle.sync.run = lambda limit: _resolved(0)
        cycle.bodies.run = lambda limit: _resolved(0)

        asyncio.run(cycle.run())
        rows = self.store.conn.execute(
            "SELECT COUNT(*) c FROM provider_usage"
        ).fetchone()["c"]
        self.assertGreaterEqual(rows, 0)
        self.assertEqual(pool.pending_usage, [], "the cycle must flush on exit")

    def test_the_ledger_is_flushed_even_when_a_stage_explodes(self):
        import asyncio

        pool = self.two_providers()
        cycle = self.cycle(pool)
        cycle.sync.run = lambda limit: _resolved(0)
        cycle.bodies.run = lambda limit: _resolved(0)
        pool.pending_usage.append({
            "provider": "groq", "task": "route_email", "outcome": "ok",
        })

        def explode(_pool):
            raise RuntimeError("stage failed")

        cycle.dispatch = explode
        result = asyncio.run(cycle.run())
        self.assertIn("error", result)
        self.assertEqual(pool.pending_usage, [])
        self.assertEqual(
            self.store.conn.execute(
                "SELECT COUNT(*) c FROM provider_usage"
            ).fetchone()["c"],
            1,
        )


async def _resolved(value):
    """An awaitable standing in for a stage that does no work."""
    return value


if __name__ == "__main__":
    unittest.main()
