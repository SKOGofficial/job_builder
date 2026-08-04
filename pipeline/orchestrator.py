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

from clients.llm_client import GroqRateLimited
from pipeline.acknowledgements import AcknowledgementHandler
from pipeline.alerts import AlertHandler
from pipeline.resolver import JobResolver
from pipeline.rough_filter import build_filter
from pipeline.router import MessageRouter
from pipeline.sync import BodyFetcher, MailboxSync
from pipeline.updates import UpdateHandler
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

        self.state = IDLE
        self.message = ""
        self.last_result = {}

    @property
    def busy(self):
        return self.state == RUNNING

    def _client(self):
        """A Groq client, or None when it is not configured.

        Returning None rather than raising is deliberate: sync and filtering
        are useful on their own, and an unconfigured model should degrade the
        pipeline rather than stop it.
        """
        if self.client_factory is None:
            from clients.llm_client import GroqClient, GroqNotConfigured

            try:
                return GroqClient.from_config()
            except GroqNotConfigured as exc:
                log.info("Model stages skipped: %s", exc)
                return None
        try:
            return self.client_factory()
        except Exception as exc:
            log.info("Model stages skipped: %s", exc)
            return None

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
        result = {}
        try:
            result["synced"] = await self.sync.run(self.limits["sync"])
            result["filter"] = self.apply_filter()
            result["bodies"] = await self.bodies.run(self.limits["bodies"])

            client = self._client()
            if client is not None:
                router = MessageRouter(
                    self.mail,
                    client_factory=lambda: client,
                    executor=self.executor,
                )
                result["classified"] = await router.run(self.limits["classify"])
                result["handled"] = await self.dispatch(client)
                result["prepared"] = await self.prepare(client)
            else:
                result["classified"] = {}
                result["handled"] = {}
                result["prepared"] = {}

            self.state = IDLE
            self.message = _summarise(result)
        except Exception as exc:
            self.state = ERROR
            self.message = f"Pipeline cycle failed: {exc}"
            log.exception("Pipeline cycle failed")
            result["error"] = str(exc)
        self.last_result = result
        return result

    async def dispatch(self, client):
        """Hand classified messages to their category handler.

        Alerts first: an acknowledgement processed before the alert that
        surfaced the role would create a job directly instead of promoting a
        lead, losing the board metadata and the canonical apply URL.

        Summary:
            Run the alert, acknowledgement, and update handlers in order.

        Parameters:
            client (GroqClient): The model client the handlers extract with.

        Returns:
            dict: Counts under `leads_created`, `leads_skipped`,
                `acknowledgements`, and `updates`.

        Note:
            Awaited rather than called directly. Each handler makes blocking
            model calls, and this runs on the loop that serves the web UI, so a
            synchronous call here froze the interface for as long as the batch
            took - minutes, once the rate limit started forcing waits.
        """
        limit = self.limits["handle"]
        created, skipped, _ = await AlertHandler(
            self.store, self.mail, client, executor=self.executor).run(limit)

        acknowledged = await AcknowledgementHandler(
            self.store, self.mail, self.resolver, client,
            executor=self.executor).run(limit)

        updated = await UpdateHandler(
            self.store, self.mail, self.resolver, client,
            threshold=self.threshold, executor=self.executor).run(limit)

        return {
            "leads_created": created,
            "leads_skipped": skipped,
            "acknowledgements": acknowledged,
            "updates": updated,
        }

    async def prepare(self, client):
        """Score new leads and build artifacts for the ones worth it.

        Degrades in two independent steps. Without the research client, leads
        still get scored and simply wait at `new`; without the Groq client this
        is not reached at all. Neither absence stops the rest of the pipeline.

        Summary:
            Run the scoring and artifact-building stage.

        Parameters:
            client (GroqClient): The model client used to score leads.

        Returns:
            dict: Counts under `scored`, `prepared`, and `failed`; empty when
                the stage could not run.
        """
        from pipeline.prepare import LeadPreparer

        research = self._research_client()
        preparer = LeadPreparer(self.store, self.mail, client, research,
                                threshold=self.relevance_threshold,
                                executor=self.executor)
        try:
            return await preparer.run(prepare_limit=self.limits["prepare"])
        except GroqRateLimited as exc:
            # Routine. Without this the stage reported a traceback and an
            # error for what is simply "out of tokens, resume next cycle".
            log.info(
                "Lead preparation paused by the rate limit; retrying next "
                "cycle, in about %ss", exc.retry_after,
            )
            return {}
        except Exception:
            log.exception("Lead preparation stage failed")
            return {}

    def _research_client(self):
        """A Claude client with a spend limiter, or None when unconfigured."""
        if self._research_factory is not None:
            try:
                return self._research_factory()
            except Exception as exc:
                log.info("Research unavailable: %s", exc)
                return None

        from clients.research_client import (
            ResearchClient,
            ResearchNotConfigured,
            SpendLimiter,
        )

        try:
            return ResearchClient.from_config(limiter=SpendLimiter(self.mail))
        except ResearchNotConfigured as exc:
            log.info("Research unavailable: %s", exc)
            return None


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
    return "; ".join(parts) or "Nothing new."
