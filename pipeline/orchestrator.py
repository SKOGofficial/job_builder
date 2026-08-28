"""Running the stages in order.

    sync -> filter -> fetch bodies -> classify -> route to handlers

Each stage is bounded per cycle. A backfill of several thousand messages must
not monopolise the event loop or blow through the free-tier token ceiling in
one go, so a cycle does a slice of work and the scheduler comes back for the
rest. Everything is resumable because progress lives in the database - the
filter verdict, the stored body, the recorded category - rather than in memory.

One cycle is safe to run concurrently with the UI: database access stays on the
calling thread and only network calls go to the executor.
"""

import logging
import time
import uuid
from datetime import datetime

from clients.llm_client import GroqNotConfigured, GroqRateLimited
from clients.providers.pool import THREAD_MAX_WAIT
from pipeline.acknowledgements import AcknowledgementHandler
from pipeline.alerts import AlertHandler
from pipeline.resolver import JobResolver
from pipeline.rough_filter import build_filter
from pipeline.router import MessageRouter
from pipeline.sync import BodyFetcher, MailboxSync
from pipeline.updates import UpdateHandler
from utilities.durations import spell_duration
from utilities.mailstore import VERDICT_PASSED

log = logging.getLogger(__name__)

IDLE, RUNNING, ERROR = "idle", "running", "error"


class PipelineCycle:
    """One pass over the pipeline, with per-stage limits."""

    def __init__(self, store, mail, client_factory=None, executor=None,
                 threshold=0.85, limits=None, research_factory=None,
                 relevance_threshold=None):
        self.store = store
        self.mail = mail
        self.client_factory = client_factory
        self._research_factory = research_factory
        self.executor = executor
        self.threshold = threshold
        self.relevance_threshold = relevance_threshold
        self.limits = {
            "sync": 500,
            "bodies": 60,
            "classify": 60,
            # Alert extraction is the most expensive model call in the
            # pipeline - roughly 3,400 tokens against a 12,000/minute ceiling,
            # so about three per minute. At 50 a cycle spent a quarter of an
            # hour on alerts alone. 15 keeps each handler's share of a cycle
            # bounded to a few minutes.
            "handle": 15,
            # Deliberately small: each prepared lead is a real spend and a slow
            # call, so a burst of new leads is spread across cycles rather than
            # fired at once.
            "prepare": 5,
        }
        self.limits.update(limits or {})

        self.sync = MailboxSync(mail, executor=executor)
        self.bodies = BodyFetcher(mail, executor=executor)
        self.resolver = JobResolver(store, mail)
        #: Built once on first use and kept, so cooldowns and daily counters
        #: survive across cycles. See `_pool`.
        self._pool_instance = None

        self.state = IDLE
        self.message = ""
        self.last_result = {}

        #: Stage timings for the cycle in flight, flushed once at the end.
        #: Buffered rather than written per stage for the same reason the
        #: provider ledger is: a measurement that writes to the database in the
        #: middle of the stage it is measuring has changed what it measured.
        self.pending_timings = []
        self.cycle_id = ""

    @property
    def busy(self):
        return self.state == RUNNING

    # --- measurement -------------------------------------------------------

    async def _timed(self, stage, run, default=None):
        """Run one stage, recording how long it took and how it ended.

        Summary:
            Await a stage, buffering a `stage_runs` row for it either way.

        Parameters:
            stage (str): The stage name, as it appears on the diagnostics page.
            run (Callable): Zero-argument callable returning the stage's
                awaitable or value.
            default: What to return when the stage raises. None by default.

        Returns:
            Any: Whatever the stage returned, or `default` when it raised.

        Raises:
            asyncio.CancelledError: Propagated - a cancelled cycle is a
                shutdown, not a stage failure.

        Note:
            Swallows the stage's exception and records it rather than letting
            it end the cycle. Before this, an exception from any handler
            propagated into `run`'s blanket `except` and took `prepare` down
            with it - one bad alert email cost the whole second half of a
            cycle. The failure is now the stage's own, and it is written down
            instead of only logged.
        """
        started = time.perf_counter()
        started_at = datetime.now().isoformat(timespec="seconds")
        outcome, detail = "ok", None
        value = default
        try:
            value = run()
            if hasattr(value, "__await__"):
                value = await value
        except Exception as exc:
            outcome, detail = "error", str(exc)[:500]
            value = default
            log.exception("Pipeline stage %r failed", stage)
        finally:
            self.pending_timings.append({
                "cycle_id": self.cycle_id,
                "stage": stage,
                "started_at": started_at,
                "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
                "processed": _count_of(value),
                "outcome": outcome,
                "detail": detail,
            })
        return value

    def note_skipped(self, stage, detail):
        """
        Summary:
            Record that a stage was deliberately not run this cycle.

        Parameters:
            stage (str): The stage that was skipped.
            detail (str): Why, in one line.

        Note:
            A skipped stage has to leave a row. "Nothing happened because no
            provider was free" and "nothing happened because there was nothing
            to do" produce the same empty result, and only the record tells
            them apart afterwards.
        """
        self.pending_timings.append({
            "cycle_id": self.cycle_id,
            "stage": stage,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "duration_ms": 0,
            "processed": 0,
            "outcome": "skipped",
            "detail": detail[:500],
        })

    def failed_stages(self):
        """
        Summary:
            List the stages that failed in the cycle currently being timed.

        Returns:
            list[tuple[str, str]]: `(stage, detail)` for each failed stage, in
                the order they ran. Empty when every stage was fine.
        """
        return [(row["stage"], row["detail"] or "failed")
                for row in self.pending_timings if row["outcome"] == "error"]

    def flush_timings(self):
        """
        Summary:
            Write this cycle's buffered stage timings and clear the buffer.

        Returns:
            int: How many rows were written.

        Note:
            Never raises. Losing a measurement must not fail the cycle that
            produced it - the pipeline's job is the mail, not the metrics.
        """
        rows, self.pending_timings = self.pending_timings, []
        try:
            return self.mail.record_stage_runs(rows)
        except Exception:
            log.debug("Could not record stage timings", exc_info=True)
            return 0

    def _pool(self):
        """The provider pool, or None when no model is configured.

        Returning None rather than raising is deliberate: sync and filtering
        are useful on their own, and an unconfigured model should degrade the
        pipeline rather than stop it.

        Summary:
            Resolve the provider pool for this cycle.

        Returns:
            ProviderPool | None: The pool, or None when nothing is configured.

        Note:
            The pool is built once and kept, not rebuilt per cycle. Cooldowns
            and daily counters have to outlive the cycle that earned them - a
            pool rebuilt each pass would retry an exhausted provider every ten
            minutes until midnight. `client_factory` is honoured for tests that
            inject a pool or a bare client.
        """
        if self._pool_instance is not None:
            return self._pool_instance
        if self.client_factory is not None:
            try:
                self._pool_instance = self.client_factory()
            except Exception as exc:
                log.info("Model stages skipped: %s", exc)
                return None
            return self._pool_instance

        from clients.providers.pool import ProviderPool

        try:
            pool = ProviderPool(mail=self.mail)
        except Exception as exc:
            log.info("Model stages skipped: %s", exc)
            return None
        if not pool.configured_names():
            log.info("Model stages skipped: no provider is configured.")
            return None
        self._pool_instance = pool
        return pool

    def apply_filter(self):
        """Give every unfiltered message a verdict.

        Runs on headers alone. Rebuilt each cycle so a company applied to five
        minutes ago, or a domain just added to the denylist, is reflected
        immediately.
        """
        rough = build_filter(self.store, self.mail)
        rows = self.mail.conn.execute(
            "SELECT * FROM messages WHERE filter_verdict IS NULL"
        ).fetchall()
        passed = dropped = 0
        for row in rows:
            verdict = rough.verdict({
                "sender": row["sender"],
                "subject": row["subject"],
                "snippet": row["snippet"],
                "labels": _labels(row),
                "list_unsubscribe": _column(row, "list_unsubscribe"),
            })
            self.mail.set_filter_verdict(row["gmail_message_id"], verdict)
            if verdict == VERDICT_PASSED:
                passed += 1
            else:
                dropped += 1
        self.mail.commit()
        if rows:
            log.info("Rough filter passed %d and dropped %d of %d",
                     passed, dropped, len(rows))
        return {"passed": passed, "dropped": dropped}

    async def run(self):
        """One full cycle. Never raises; failures are recorded and reported."""
        if self.busy:
            return self.last_result
        self.state = RUNNING
        self.cycle_id = uuid.uuid4().hex[:12]
        self.pending_timings = []
        result = {}
        try:
            result["synced"] = await self._timed(
                "sync", lambda: self.sync.run(self.limits["sync"]), 0)
            result["filter"] = await self._timed(
                "filter", self.apply_filter, {})
            result["bodies"] = await self._timed(
                "bodies", lambda: self.bodies.run(self.limits["bodies"]), 0)
            # Straight after the fetch, because that is when a body turns out
            # to be empty. A message whose body came back blank is excluded
            # from the model queue and was previously marked as nothing at all,
            # so it stayed "unclassified" for ever with no way to tell it from
            # mail that had simply not been reached.
            result["retired"] = await self._timed(
                "retire", self.mail.retire_unclassifiable, 0)
            result["expired"] = await self._timed(
                "expire", self.mail.purge_stale_leads, 0)

            pool = self._pool()
            if pool is not None:
                pool.begin_cycle()
                try:
                    # Classification first and unconditionally: the rule tier
                    # needs no provider, so a cooling-off pool must not stop
                    # a mailbox of job-board digests being sorted.
                    result["classified"] = await self._timed(
                        "classify", lambda: self.classify(pool), {})
                    result.update(await self._model_stages(pool))
                finally:
                    pool.flush()
            else:
                result["classified"] = await self._timed(
                    "classify", lambda: self.classify(None), {})
                self.note_skipped("dispatch", "No provider is configured.")
                self.note_skipped("prepare", "No provider is configured.")
                result["handled"] = {}
                result["prepared"] = {}

            self.state = IDLE
            self.message = _summarise(result)

            # A stage that failed no longer ends the cycle - the stages after
            # it are worth running, and one bad alert email used to cost the
            # whole second half of a pass. But it must still be *reported*: a
            # stage failing on every single cycle showing nothing anywhere is
            # the other half of the same bug.
            failures = self.failed_stages()
            if failures:
                self.state = ERROR
                result["error"] = "; ".join(
                    f"{stage}: {detail}" for stage, detail in failures
                )
                self.message = (
                    f"{self.message} "
                    f"({', '.join(stage for stage, _ in failures)} failed)"
                )
        except Exception as exc:
            self.state = ERROR
            self.message = f"Pipeline cycle failed: {exc}"
            log.exception("Pipeline cycle failed")
            result["error"] = str(exc)
        finally:
            # Written even when the cycle failed. A cycle that died halfway is
            # precisely the one whose stage timings are worth reading.
            self.flush_timings()
        self.last_result = result
        return result

    async def classify(self, pool):
        """Label the unclassified backlog, with or without a provider.

        Summary:
            Run level-0 rules over pending mail and hand the residue to the
            model when one is available.

        Parameters:
            pool (ProviderPool | None): The pool to draw a `route_email` client
                from. None, or a pool with nothing available, still classifies
                everything the rules can answer.

        Returns:
            dict[str, int]: Count per label assigned this cycle.

        Note:
            Deliberately outside `_model_stages`. Roughly seven messages in ten
            are decided from the headers alone, and gating that on a provider
            meant an exhausted free tier froze the to-apply list for the rest of
            the day over work that costs nothing.
        """
        route = None
        if pool is not None and pool.next_available_in() <= 0:
            # Calls go through an executor, so the longer sleep budget applies -
            # same reasoning as `dispatch`.
            route = pool.for_task("route_email", max_wait=THREAD_MAX_WAIT)
        router = MessageRouter(
            self.mail,
            client_factory=_resolved_factory(route),
            executor=self.executor,
        )
        return await router.run(self.limits["classify"])

    async def _model_stages(self, pool):
        """Dispatch and prepare - or skip the lot and say when.

        Summary:
            Run every stage that needs a model, unless no provider can take a
            call yet.

        Parameters:
            pool (ProviderPool): The pool to draw task clients from.

        Returns:
            dict: `handled` and `prepared`, plus `retry_after` in seconds when
                the stages were skipped.

        Note:
            Asking the pool once, up front, replaces four stages each
            discovering the same cooldown for itself and logging its own line
            about it. The cost was never the API - `candidates` filters a
            cooling provider without calling it - but an hour of cooldown at a
            ten-minute cadence still meant thirty log lines saying nothing.

            Only these stages are skipped. Sync, filtering, body fetching, and
            rule-based classification have all already run by the time this is
            called, on purpose: a rate-limited model must not stop new mail
            reaching the inbox view or the to-apply list.
        """
        wait = pool.next_available_in()
        if wait > 0:
            log.info(
                "Handler stages skipped: no provider is available for about "
                "%ds. Mail still synced and sorted by rule; extraction resumes "
                "when one frees up.", int(wait),
            )
            reason = f"No provider available for about {int(wait)}s."
            self.note_skipped("dispatch", reason)
            self.note_skipped("prepare", reason)
            return {"handled": {}, "prepared": {}, "retry_after": int(wait)}

        return {
            "handled": await self._timed(
                "dispatch", lambda: self.dispatch(pool), {}),
            "prepared": await self._timed(
                "prepare", lambda: self.prepare(pool), {}),
        }

    async def dispatch(self, pool):
        """Hand classified messages to their category handler.

        Alerts first: an acknowledgement processed before the alert that
        surfaced the role would create a job directly instead of promoting a
        lead, losing the board metadata and the canonical apply URL.

        Summary:
            Run each category handler with a client bound to its own task.

        Parameters:
            pool (ProviderPool): The pool to draw task clients from.

        Returns:
            dict: Counts per handler.

        Note:
            Awaited, and each handler puts its model call on an executor, so
            none of this blocks the loop that serves the UI. That is why the
            clients get the longer sleep budget: the short one exists for calls
            made on the loop thread, and a handler waiting out a pacing gap in
            a worker thread costs the interface nothing. Capping it at the
            short budget here would make the pool give up and fail over after
            two seconds for no reason.
        """
        limit = self.limits["handle"]
        created, skipped, _ = await AlertHandler(
            self.store, self.mail,
            pool.for_task("extract_alert", max_wait=THREAD_MAX_WAIT),
            executor=self.executor).run(limit)

        acknowledged = await AcknowledgementHandler(
            self.store, self.mail, self.resolver,
            pool.for_task("extract_acknowledgement",
                          max_wait=THREAD_MAX_WAIT),
            executor=self.executor).run(limit)

        updated = await UpdateHandler(
            self.store, self.mail, self.resolver,
            pool.for_task("extract_update", max_wait=THREAD_MAX_WAIT),
            threshold=self.threshold, executor=self.executor).run(limit)

        return {
            "leads_created": created,
            "leads_skipped": skipped,
            "acknowledgements": acknowledged,
            "updates": updated,
        }

    async def prepare(self, pool):
        """Score new leads and build artifacts for the ones worth it.

        Degrades in two independent steps. Without a research provider, leads
        still get scored and simply wait at `new`; without a scoring provider
        this is not reached at all. Neither absence stops the rest of the
        pipeline.

        Summary:
            Run the relevance gate and the artifact builder for this cycle.

        Parameters:
            pool (ProviderPool): The pool to draw task clients from.

        Returns:
            dict: The preparer's per-stage counts, or an empty dict on failure.

        Note:
            Awaited, and the scorer and the builder both put their slow work on
            an executor, so these clients take the longer sleep budget for the
            same reason `dispatch`'s do.
        """
        from pipeline.prepare import LeadPreparer

        research = pool.for_task("research", max_wait=THREAD_MAX_WAIT)
        if self._research_factory is not None:
            try:
                research = self._research_factory()
            except Exception as exc:
                log.info("Research unavailable: %s", exc)
                research = None

        preparer = LeadPreparer(
            self.store, self.mail,
            pool.for_task("score_relevance", max_wait=THREAD_MAX_WAIT),
            research,
            threshold=self.relevance_threshold,
            executor=self.executor,
            letter_client=pool.for_task("write_cover_letter",
                                        max_wait=THREAD_MAX_WAIT),
        )
        try:
            return await preparer.run(prepare_limit=self.limits["prepare"])
        except GroqRateLimited as exc:
            # A backstop, not the primary handler. `LeadPreparer` catches its
            # own limits per lead - it has to, since it is the one that knows
            # which lead to put back - so this only sees one raised outside
            # that loop. It stays because "out of tokens, resume next cycle" is
            # routine wherever it surfaces from, and should never be a
            # traceback.
            log.info(
                "Lead preparation paused by the rate limit; retrying next "
                "cycle, in about %ss", exc.retry_after,
            )
            return {}
        except Exception:
            log.exception("Lead preparation stage failed")
            return {}


def _resolved_factory(client):
    """Adapt an already-resolved client to the factory the router expects.

    Summary:
        Wrap a client (or its absence) as a zero-argument factory.

    Parameters:
        client: The client to hand back, or None when none is available.

    Returns:
        Callable[[], object]: A factory returning `client`.

    Raises:
        GroqNotConfigured: From the returned factory when `client` is None.

    Note:
        None has to raise rather than return None, because that is the signal
        the router already understands for "carry on without a model" - and
        letting it fall through to its default `GroqClient.from_config` would
        reach for an API key the pool has already decided against.
    """
    def factory():
        if client is None:
            raise GroqNotConfigured(
                "No provider is available to classify the remaining mail."
            )
        return client

    return factory


def _count_of(value):
    """How much work a stage got through, from whatever shape it returned.

    Summary:
        Reduce a stage's return value to a single count.

    Parameters:
        value: A stage result - an int, a dict of counts, or None.

    Returns:
        int: The stage's throughput for this cycle, 0 when it cannot be read.

    Note:
        The stages predate the measurement and return four different shapes
        between them. Normalising here keeps that history out of the schema,
        which only wants a number.
    """
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return sum(v for v in value.values() if isinstance(v, int)
                   and not isinstance(v, bool))
    if isinstance(value, (list, tuple)):
        return sum(v for v in value if isinstance(v, int)
                   and not isinstance(v, bool))
    return 0


def _labels(row):
    import json

    try:
        return json.loads(row["labels"] or "[]")
    except (TypeError, ValueError):
        return []


def _column(row, name, default=""):
    """Read a column that may predate the current schema.

    Rows written before a migration added a column still come back without it
    when a cached statement is reused, so this degrades instead of raising.
    """
    try:
        return row[name] or default
    except (IndexError, KeyError):
        return default


def _summarise(result):
    parts = []
    if result.get("synced"):
        parts.append(f"{result['synced']} new message(s)")
    classified = result.get("classified") or {}
    if classified:
        parts.append(", ".join(f"{count} {label}"
                               for label, count in sorted(classified.items())))
    handled = result.get("handled") or {}
    if handled.get("leads_created"):
        parts.append(f"{handled['leads_created']} new lead(s)")
    # Worth a line of its own. Rows disappearing from the to-apply list with no
    # explanation reads as data loss, which is exactly the wrong impression of
    # a deliberate freshness window.
    if result.get("expired"):
        parts.append(f"{result['expired']} stale lead(s) dropped")
    # Said even when other parts exist: "3 new message(s)" with no mention of
    # classification reads as a pipeline that quietly stopped working.
    if result.get("retry_after"):
        parts.append(f"waiting {_minutes(result['retry_after'])} for a model")
    return "; ".join(parts) or "Nothing new."


def _minutes(seconds):
    """Seconds as a short human phrase, for the status line.

    Kept as a name because that is what `_summarise` reads; the spelling itself
    is shared, so a daily ceiling reads as "24h" rather than "1440m".
    """
    return spell_duration(seconds) or "0s"
