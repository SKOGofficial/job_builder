"""A cycle's duration is a budget, not whatever the providers made it.

Every stage was bounded by a count and none by time, which is not the same
thing. `classify` at sixty calls against a five-second pacer is three hundred
seconds; `prepare` is five leads against a subprocess allowed four minutes
each. `dispatch` runs after both. So the stages at the end were not competing
for the cycle - they were getting whatever the ones at the front left over,
which on a bad day was nothing.

The filter stage had no count limit either, and it is the one stage that does
its whole pass synchronously on the thread serving the UI.
"""

import asyncio
import sqlite3
import unittest

from pipeline.orchestrator import CYCLE_DEADLINE_SHARE, PipelineCycle
from pipeline.scheduler import PipelineScheduler
from pipeline.rough_filter import DROP_PERSONAL
from utilities.mailstore import MailStore, VERDICT_PASSED
from utilities.store import JobStore


async def _resolved(value):
    return value


class FakeClock:
    """A monotonic clock that only moves when told to.

    Timing tests that race a real clock are the ones that pass on a laptop and
    fail in CI. The first version of the deadline test set a one-millisecond
    budget and assumed seven stubbed stages would outlast it; against an
    in-memory database they do not reliably take that long, so the assertion
    depended on how busy the runner was.
    """

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_cycle(**kwargs):
    store = JobStore(":memory:")
    mail = MailStore(store.conn)

    def no_pool():
        raise RuntimeError("no provider configured")

    cycle = PipelineCycle(store, mail, client_factory=no_pool, **kwargs)
    cycle.sync.run = lambda limit: _resolved(0)
    cycle.bodies.run = lambda limit: _resolved(0)
    return store, mail, cycle


class TheFilterStageIsBoundedTests(unittest.TestCase):
    def setUp(self):
        self.store, self.mail, self.cycle = make_cycle(limits={"filter": 3})

    def tearDown(self):
        self.store.close()

    def add(self, count):
        for index in range(count):
            self.mail.upsert_message({
                "id": f"m{index}", "sender": "someone@example.com",
                "subject": "Hello", "date": ""})
        self.mail.commit()

    def unfiltered(self):
        return self.store.conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE filter_verdict IS NULL"
        ).fetchone()["c"]

    def test_one_pass_does_no_more_than_its_limit(self):
        self.add(10)
        result = self.cycle.apply_filter()
        self.assertEqual(result["passed"] + result["dropped"], 3)
        self.assertEqual(self.unfiltered(), 7)

    def test_the_rest_is_picked_up_next_cycle(self):
        # The verdict column is the resume point, so nothing is lost by
        # stopping - which is what makes a bound safe here at all.
        self.add(10)
        for _ in range(4):
            self.cycle.apply_filter()
        self.assertEqual(self.unfiltered(), 0)

    def test_it_works_oldest_first(self):
        self.add(5)
        self.mail.conn.execute(
            "UPDATE messages SET received_ts = CAST(substr(gmail_message_id, 2) "
            "AS INTEGER)")
        self.mail.commit()
        self.cycle.apply_filter()

        done = [row["gmail_message_id"] for row in self.store.conn.execute(
            "SELECT gmail_message_id FROM messages "
            "WHERE filter_verdict IS NOT NULL ORDER BY received_ts")]
        self.assertEqual(done, ["m0", "m1", "m2"])


class TheCycleStopsStartingStagesWhenOutOfTimeTests(unittest.TestCase):
    def setUp(self):
        self.store, self.mail, self.cycle = make_cycle()

    def tearDown(self):
        self.store.close()

    def stages(self):
        return {row["stage"]: row for row in self.store.conn.execute(
            "SELECT * FROM stage_runs")}

    def test_no_deadline_means_no_ceiling(self):
        self.assertIsNone(self.cycle.deadline_seconds)
        self.assertFalse(self.cycle.out_of_time())

    def test_an_expired_deadline_skips_the_remaining_stages(self):
        clock = FakeClock()
        store, _mail, cycle = make_cycle(deadline_seconds=30, clock=clock)
        self.addCleanup(store.close)
        # Burn the whole budget during the first stage, so the deadline is
        # unambiguously spent by the time the second one is considered.
        cycle.sync.run = lambda limit: (clock.advance(60), _resolved(0))[1]

        asyncio.run(cycle.run())

        rows = {row["stage"]: row for row in store.conn.execute(
            "SELECT * FROM stage_runs")}
        # Recorded, not silently absent. A stage that never got a turn looks
        # exactly like a stage with nothing to do, and only the row says which.
        out_of_time = [name for name, row in rows.items()
                       if row["outcome"] == "skipped"
                       and "ran out of time" in (row["detail"] or "")]
        self.assertIn("filter", out_of_time)
        self.assertIn("classify", out_of_time)

    def test_the_first_stage_always_runs(self):
        # A budget smaller than a cycle takes is a misconfiguration, not an
        # instruction to do nothing. Without this guarantee the pipeline would
        # skip every stage on every cycle and quietly stop working while still
        # reporting that it had run.
        clock = FakeClock()
        store, _mail, cycle = make_cycle(deadline_seconds=30, clock=clock)
        self.addCleanup(store.close)
        clock.advance(600)  # already long past the deadline before we begin

        asyncio.run(cycle.run())

        first = store.conn.execute(
            "SELECT * FROM stage_runs ORDER BY id LIMIT 1").fetchone()
        self.assertEqual(first["stage"], "sync")
        self.assertEqual(first["outcome"], "ok")

    def test_a_generous_deadline_lets_everything_run(self):
        self.cycle.deadline_seconds = 300
        asyncio.run(self.cycle.run())
        stages = self.stages()
        self.assertNotIn("ran out of time", str(
            [row["detail"] for row in stages.values()]))

    def test_the_scheduler_sets_a_budget_from_its_interval(self):
        store, _mail, cycle = make_cycle()
        self.addCleanup(store.close)
        scheduler = PipelineScheduler(cycle, interval=600)
        self.assertEqual(cycle.deadline_seconds,
                         int(600 * CYCLE_DEADLINE_SHARE))

    def test_an_explicit_budget_is_left_alone(self):
        store, _mail, cycle = make_cycle(deadline_seconds=42)
        self.addCleanup(store.close)
        PipelineScheduler(cycle, interval=600)
        self.assertEqual(cycle.deadline_seconds, 42)


class VacuumRunsOffTheLoopTests(unittest.IsolatedAsyncioTestCase):
    """It rewrites the whole file under an exclusive lock, roughly daily."""

    async def test_a_file_backed_database_is_vacuumed_in_a_thread(self):
        import tempfile
        import threading
        import os

        handle, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        store = JobStore(path)
        mail = MailStore(store.conn)

        def no_pool():
            raise RuntimeError("no provider")

        cycle = PipelineCycle(store, mail, client_factory=no_pool)
        scheduler = PipelineScheduler(cycle, interval=60)

        loop_thread = threading.get_ident()
        seen = {}
        real_connect = sqlite3.connect

        def spy(*args, **kwargs):
            seen["thread"] = threading.get_ident()
            return real_connect(*args, **kwargs)

        sqlite3.connect = spy
        try:
            await scheduler._vacuum()
        finally:
            sqlite3.connect = real_connect
            store.close()
            try:
                os.unlink(path)
            except PermissionError:
                pass

        self.assertIn("thread", seen)
        self.assertNotEqual(seen["thread"], loop_thread,
                            "VACUUM must not run on the event loop thread")

    async def test_an_in_memory_database_is_skipped(self):
        # `:memory:` has nothing to reclaim, and opening a second connection to
        # it would open a different, empty database.
        store, _mail, cycle = make_cycle()
        self.addCleanup(store.close)
        scheduler = PipelineScheduler(cycle, interval=60)
        await scheduler._vacuum()  # must not raise


class SharedPoolStateIsGuardedTests(unittest.TestCase):
    """`record` and `cool_down` are called from executor threads."""

    def test_concurrent_records_all_survive(self):
        import threading

        from clients.providers.pool import ProviderPool

        pool = ProviderPool(names=[])
        barrier = threading.Barrier(8)

        def record():
            barrier.wait()
            for _ in range(50):
                pool.record("groq", "route_email", "ok")

        threads = [threading.Thread(target=record) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(pool.pending_usage), 8 * 50)

    def test_a_cooldown_is_never_lost_to_a_concurrent_one(self):
        import threading

        from clients.providers.pool import ProviderState

        state = ProviderState("groq")
        barrier = threading.Barrier(8)

        def cool(value):
            barrier.wait()
            for _ in range(50):
                state.cool_down(value)

        threads = [threading.Thread(target=cool, args=(index,))
                   for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # `cool_down` extends but never shortens, so the highest must win.
        self.assertEqual(state.cooldown_until, 7)


if __name__ == "__main__":
    unittest.main()
