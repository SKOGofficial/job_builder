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
from datetime import datetime

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
            self._prune()

        self.emit()
        return result

    def _prune(self):
        try:
            cleared = self.cycle.mail.prune_bodies(self.prune_after_days)
            # Budgeting only ever looks back 24 hours; the rest is kept so
            # "what did last month cost" stays answerable, and dropped after
            # that so the table cannot grow without bound unattended.
            usage = self.cycle.mail.prune_provider_usage(self.prune_after_days)
            if usage:
                log.info("Pruned %d provider usage row(s) older than %d days",
                         usage, self.prune_after_days)
            if cleared:
                log.info("Pruned %d irrelevant message bod%s older than %d days",
                         cleared, "y" if cleared == 1 else "ies",
                         self.prune_after_days)
            if cleared or usage:
                # Reclaim the space; without this the file only ever grows.
                self.cycle.mail.conn.execute("VACUUM")
        except Exception:
            log.exception("Retention pass failed")

    def status(self):
        return {
            "running": self.running,
            "interval": self.interval,
            "cycles": self.cycles,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "state": self.cycle.state,
            "message": self.cycle.message,
        }
