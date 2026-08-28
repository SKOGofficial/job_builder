"""Every stage leaves a record of what it did and how long it took.

The pipeline bounds each stage by a count and none of them by time, so a
cycle's duration is whatever the providers happened to make it that day. There
was no `perf_counter` anywhere outside the tests and no duration column in
`provider_usage`, which left "why was that cycle slow" unanswerable from
inside the app.

The second thing this covers is the difference between a stage that did
nothing because there was nothing to do and a stage that did nothing because
no provider was free. Those produce the same empty result and only the record
tells them apart.
"""

import asyncio
import unittest

from pipeline.orchestrator import PipelineCycle, _count_of
from utilities.mailstore import MailStore
from utilities.store import JobStore


async def _resolved(value):
    """An awaitable standing in for a stage that does no work."""
    return value


def make_cycle():
    """
    Summary:
        Build a cycle wired to an in-memory database and no providers.

    Returns:
        tuple[JobStore, MailStore, PipelineCycle]: The stores and the cycle.

    Note:
        `client_factory` raises, which is how a cycle with nothing configured
        behaves. The Gmail halves are stubbed because this is about the timing
        wrapper, not about the network.
    """
    store = JobStore(":memory:")
    mail = MailStore(store.conn)

    def no_pool():
        raise RuntimeError("no provider configured")

    cycle = PipelineCycle(store, mail, client_factory=no_pool)
    cycle.sync.run = lambda limit: _resolved(0)
    cycle.bodies.run = lambda limit: _resolved(0)
    return store, mail, cycle


class StageTimingTests(unittest.TestCase):
    def setUp(self):
        self.store, self.mail, self.cycle = make_cycle()

    def tearDown(self):
        self.store.close()

    def rows(self):
        return self.store.conn.execute(
            "SELECT * FROM stage_runs ORDER BY id"
        ).fetchall()

    def test_a_cycle_records_a_row_for_every_stage(self):
        asyncio.run(self.cycle.run())
        stages = {row["stage"] for row in self.rows()}
        self.assertEqual(
            stages,
            {"sync", "filter", "bodies", "retire", "retire_alerts", "expire",
             "classify", "dispatch", "prepare"},
        )

    def test_the_stages_of_one_cycle_share_a_cycle_id(self):
        asyncio.run(self.cycle.run())
        ids = {row["cycle_id"] for row in self.rows()}
        self.assertEqual(len(ids), 1)

        asyncio.run(self.cycle.run())
        self.assertEqual(len({row["cycle_id"] for row in self.rows()}), 2)

    def test_a_skipped_stage_says_so_rather_than_looking_idle(self):
        asyncio.run(self.cycle.run())
        skipped = {row["stage"]: row for row in self.rows()
                   if row["outcome"] == "skipped"}
        self.assertEqual(set(skipped), {"dispatch", "prepare"})
        self.assertIn("provider", skipped["dispatch"]["detail"].lower())

    def test_timings_are_written_even_when_a_stage_explodes(self):
        def explode():
            raise RuntimeError("the filter fell over")

        self.cycle.apply_filter = explode
        asyncio.run(self.cycle.run())

        failed = [row for row in self.rows() if row["outcome"] == "error"]
        self.assertEqual([row["stage"] for row in failed], ["filter"])
        self.assertIn("fell over", failed[0]["detail"])

    def test_a_failed_stage_does_not_stop_the_ones_after_it(self):
        # The bug this exists for: a handler raising anything but a rate limit
        # propagated into the cycle's blanket except and took `prepare` down
        # with it, so one bad alert email cost the second half of the cycle.
        def explode():
            raise RuntimeError("nope")

        self.cycle.apply_filter = explode
        asyncio.run(self.cycle.run())

        stages = [row["stage"] for row in self.rows()]
        self.assertIn("classify", stages)
        self.assertIn("expire", stages)

    def test_a_failed_stage_is_still_reported_to_the_scheduler(self):
        # Not aborting is not the same as not reporting.
        def explode():
            raise RuntimeError("nope")

        self.cycle.apply_filter = explode
        result = asyncio.run(self.cycle.run())
        self.assertIn("error", result)
        self.assertIn("filter", result["error"])

    def test_a_clean_cycle_reports_no_error(self):
        result = asyncio.run(self.cycle.run())
        self.assertNotIn("error", result)


class TimingSummaryTests(unittest.TestCase):
    def setUp(self):
        self.store = JobStore(":memory:")
        self.mail = MailStore(self.store.conn)

    def tearDown(self):
        self.store.close()

    def write(self, stage, durations, outcome="ok"):
        self.mail.record_stage_runs([
            {"cycle_id": f"c{i}", "stage": stage,
             "started_at": f"2026-08-2{i % 9}T10:00:00",
             "duration_ms": ms, "processed": 1, "outcome": outcome}
            for i, ms in enumerate(durations)
        ])

    def test_percentiles_come_back_as_measured_values(self):
        self.write("classify", [10, 20, 30, 40, 1000])
        summary = {row["stage"]: row
                   for row in self.mail.stage_timings("2026-01-01T00:00:00")}
        self.assertEqual(summary["classify"]["runs"], 5)
        self.assertEqual(summary["classify"]["max_ms"], 1000)
        # Nearest-rank, so every number on the page is one that was actually
        # observed rather than an interpolation between two that were not.
        self.assertIn(summary["classify"]["median_ms"], (20, 30))
        self.assertIn(summary["classify"]["p95_ms"], (40, 1000))

    def test_the_slowest_stage_is_listed_first(self):
        self.write("classify", [5000, 5000])
        self.write("expire", [1, 1])
        stages = [row["stage"]
                  for row in self.mail.stage_timings("2026-01-01T00:00:00")]
        self.assertEqual(stages[0], "classify")

    def test_a_skipped_stage_is_not_counted_as_a_failure(self):
        self.write("prepare", [0, 0], outcome="skipped")
        summary = self.mail.stage_timings("2026-01-01T00:00:00")[0]
        self.assertEqual(summary["failures"], 0)

    def test_the_last_failure_of_each_stage_is_recoverable(self):
        self.mail.record_stage_runs([
            {"cycle_id": "a", "stage": "dispatch",
             "started_at": "2026-08-01T10:00:00", "duration_ms": 1,
             "outcome": "error", "detail": "older"},
            {"cycle_id": "b", "stage": "dispatch",
             "started_at": "2026-08-02T10:00:00", "duration_ms": 1,
             "outcome": "error", "detail": "newer"},
        ])
        self.assertEqual(
            self.mail.last_stage_errors()["dispatch"]["detail"], "newer")

    def test_recent_runs_are_selected_by_cycle_not_by_row(self):
        # A broken pipeline writes fewer rows per cycle than a working one, so
        # a flat LIMIT would show more history the worse things got.
        self.mail.record_stage_runs([
            {"cycle_id": "old", "stage": s,
             "started_at": "2026-08-01T10:00:00", "duration_ms": 1,
             "outcome": "ok"}
            for s in ("sync", "filter", "classify", "dispatch")
        ])
        self.mail.record_stage_runs([
            {"cycle_id": "new", "stage": "sync",
             "started_at": "2026-08-02T10:00:00", "duration_ms": 1,
             "outcome": "ok"},
        ])
        rows = self.mail.recent_stage_runs(cycles=1)
        self.assertEqual({row["cycle_id"] for row in rows}, {"new"})

    def test_retention_drops_only_what_is_past_the_window(self):
        from datetime import datetime, timedelta

        recent = datetime.now().isoformat(timespec="seconds")
        old = (datetime.now() - timedelta(days=60)).isoformat(
            timespec="seconds")
        self.mail.record_stage_runs([
            {"cycle_id": "a", "stage": "sync", "started_at": old,
             "duration_ms": 1, "outcome": "ok"},
            {"cycle_id": "b", "stage": "sync", "started_at": recent,
             "duration_ms": 1, "outcome": "ok"},
        ])
        self.assertEqual(self.mail.prune_stage_runs(30), 1)
        self.assertEqual(len(self.mail.recent_stage_runs()), 1)


class ProviderDurationTests(unittest.TestCase):
    def setUp(self):
        self.store = JobStore(":memory:")
        self.mail = MailStore(self.store.conn)

    def tearDown(self):
        self.store.close()

    def test_a_duration_is_stored_when_one_was_measured(self):
        self.mail.record_provider_usage([
            {"provider": "groq", "task": "route_email", "outcome": "ok",
             "duration_ms": 412},
        ])
        self.assertEqual(
            self.store.conn.execute(
                "SELECT duration_ms FROM provider_usage"
            ).fetchone()["duration_ms"],
            412,
        )

    def test_a_call_that_never_happened_records_null_not_zero(self):
        # Zero would put a call that was never made in the same bucket as an
        # instant reply and drag every average down with non-events.
        self.mail.record_provider_usage([
            {"provider": "groq", "task": "route_email", "outcome": "error"},
        ])
        self.assertIsNone(
            self.store.conn.execute(
                "SELECT duration_ms FROM provider_usage"
            ).fetchone()["duration_ms"]
        )


class CountOfTests(unittest.TestCase):
    """The stages predate the measurement and return four shapes between them."""

    def test_each_stage_shape_reduces_to_a_number(self):
        self.assertEqual(_count_of(7), 7)
        self.assertEqual(_count_of({"passed": 2, "dropped": 3}), 5)
        self.assertEqual(_count_of((1, 2, 3)), 6)
        self.assertEqual(_count_of(None), 0)
        self.assertEqual(_count_of({"note": "a string"}), 0)

    def test_a_boolean_is_not_counted_as_one(self):
        # `prepare_now` returns True. Counting that as "1 processed" would be
        # arithmetic on a flag.
        self.assertEqual(_count_of(True), 0)


if __name__ == "__main__":
    unittest.main()
