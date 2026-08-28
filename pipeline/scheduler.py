"""When the pipeline runs.

An asyncio task on the same event loop the UI uses. That is the important
detail: the concurrency contract in `web/state.py` says database access stays
on the thread that opened the sqlite connection, and a scheduler running as a
coroutine on that loop honours it for free. A background *thread* calling the
same store would not.

The loop is deliberately dull - fixed interval, jittered start, errors logged
and swallowed. A poller that dies quietly at 3am is worse than one that runs a
useless cycle, so nothing in here is allowed to raise out of `_loop`.
"""

import asyncio
import contextlib
import logging
import random
import sqlite3
from datetime import datetime

from pipeline.orchestrator import CYCLE_DEADLINE_SHARE

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 600  # 10 minutes
MIN_INTERVAL_SECONDS = 60

#: Retention pass is cheap but pointless to run every cycle.
PRUNE_EVERY_CYCLES = 144  # about daily at a 10-minute interval


class PipelineScheduler:
    def __init__(self, cycle, interval=DEFAULT_INTERVAL_SECONDS,
                 prune_after_days=30):
        self.cycle = cycle
        self.interval = max(MIN_INTERVAL_SECONDS, int(interval))
        # The cycle bounds each stage by a count and none by time, so its
        # duration is whatever the providers made it that day. The scheduler is
        # the only thing that knows what "too long" means here - it is the one
        # holding the interval - so it is what sets the budget. Left alone if a
        # caller set one explicitly.
        if cycle.deadline_seconds is None:
            cycle.deadline_seconds = int(self.interval * CYCLE_DEADLINE_SHARE)
        self.prune_after_days = prune_after_days
        self.task = None
        self.cycles = 0
        self.last_run_at = None
        self.last_error = None
        self.listeners = []

    # --- lifecycle ---------------------------------------------------------

    def start(self):
        if self.task is not None and not self.task.done():
            return self.task
        self.task = asyncio.create_task(self._loop(), name="pipeline-scheduler")
        log.info("Pipeline scheduler started at a %ds interval", self.interval)
        return self.task

    async def stop(self):
        if self.task is None:
            return
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        self.task = None
        log.info("Pipeline scheduler stopped")

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    # --- notification ------------------------------------------------------

    def subscribe(self, callback):
        self.listeners.append(callback)
        return callback

    def unsubscribe(self, callback):
        if callback in self.listeners:
            self.listeners.remove(callback)

    def emit(self):
        for callback in list(self.listeners):
            try:
                callback(self)
            except Exception:
                # A page that has gone away must not break the scheduler.
                log.debug("Scheduler listener failed", exc_info=True)

    # --- the loop ----------------------------------------------------------

    async def _loop(self):
        # Jitter the first run so a restart loop does not hammer Gmail in
        # lockstep, and so the UI is responsive before any work starts.
        await asyncio.sleep(random.uniform(5, 20))
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Belt and braces: run_once already swallows, but the loop must
                # survive anything at all.
                self.last_error = str(exc)
                log.exception("Scheduler cycle raised")
            await asyncio.sleep(self.interval)

    async def run_once(self):
        """One cycle plus periodic maintenance. Safe to call by hand."""
        result = await self.cycle.run()
        self.cycles += 1
        self.last_run_at = datetime.now().isoformat(timespec="seconds")
        self.last_error = result.get("error")

        if self.cycles % PRUNE_EVERY_CYCLES == 0:
            await self._prune()

        self.emit()
        return result

    async def _prune(self):
        try:
            cleared = self.cycle.mail.prune_bodies(self.prune_after_days)
            # Budgeting only ever looks back 24 hours; the rest is kept so
            # "what did last month cost" stays answerable, and dropped after
            # that so the table cannot grow without bound unattended.
            usage = self.cycle.mail.prune_provider_usage(self.prune_after_days)
            # Seven stages every ten minutes outgrows the usage table, so it
            # goes on the same schedule rather than a slower one of its own.
            usage += self.cycle.mail.prune_stage_runs(self.prune_after_days)
            if usage:
                log.info("Pruned %d telemetry row(s) older than %d days",
                         usage, self.prune_after_days)
            if cleared:
                log.info("Pruned %d irrelevant message bod%s older than %d days",
                         cleared, "y" if cleared == 1 else "ies",
                         self.prune_after_days)
            if cleared or usage:
                await self._vacuum()
        except Exception:
            log.exception("Retention pass failed")

    async def _vacuum(self):
        """Reclaim the space the retention pass freed, off the loop thread.

        Summary:
            Run `VACUUM` on its own connection in a worker thread.

        Note:
            Without this the file only ever grows. But VACUUM rewrites the
            whole database and holds an exclusive lock while it does - on an
            11MB file that is hundreds of milliseconds, and it was running on
            the event loop that also serves every page. Roughly daily, the
            interface simply stopped.

            A worker thread needs its own connection: sqlite hands a connection
            to the thread that opened it, and the shared one belongs to the
            loop. That is not a breach of the concurrency contract in
            `web/state.py` - the contract is about *that* connection - but it
            is the reason this cannot simply be handed the store.

            Failures are logged and swallowed. An un-reclaimed page is a disk
            usage problem; a retention pass that took the poller down with it
            is an outage.
        """
        path = getattr(self.cycle.store, "db_path", None)
        if not path or path == ":memory:":
            return

        def run():
            connection = sqlite3.connect(path, timeout=30)
            try:
                connection.execute("VACUUM")
            finally:
                connection.close()

        try:
            await asyncio.to_thread(run)
        except Exception:
            log.warning("Could not reclaim space after the retention pass",
                        exc_info=True)

    def status(self):
        return {
            "running": self.running,
            "interval": self.interval,
            "cycles": self.cycles,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "state": self.cycle.state,
            "message": self.cycle.message,
            "deadline": self.cycle.deadline_seconds,
            # Per-stage failures, which `last_error` alone could not carry: it
            # only ever held what reached the top of `run`, so a stage that
            # failed on every single cycle showed nothing anywhere.
            "stage_errors": self._stage_errors(),
        }

    def _stage_errors(self):
        """
        Summary:
            The most recent failure of each stage, for the Settings card.

        Returns:
            dict[str, dict]: Stage name mapped to its last failure, or an empty
                dict when the store cannot be read.

        Note:
            Never raises. This is rendered on a page that redraws on a timer,
            and a status card that vanished because a query failed would hide
            more than it reported.
        """
        try:
            return self.cycle.mail.last_stage_errors()
        except Exception:
            log.debug("Could not read stage errors", exc_info=True)
            return {}
