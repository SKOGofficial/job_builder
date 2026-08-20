"""Job updates: a rejection, an interview invite, an OA, an offer.

Two decisions per email, kept independent on purpose:

1. **Which job is this about?** -> link it, so the email shows on that role's
   timeline.
2. **Should the status change?** -> only above the confidence threshold.

They are independent because a message can be confidently *placed* and only
tentatively *understood*. Showing the user an email on the right job and
letting them decide is useful on its own, so linking happens even when the
status write does not.

Every status write goes through the existing reversible machinery in
`utilities/store.py` - the previous status and response date are captured
first, so undo restores the job exactly. That matters most for Rejected:
applying it stamps a response date, which drops the job out of the pool future
scans check.
"""

import asyncio
import logging

from clients.llm_client import GroqRateLimited
from clients.providers.base import ProviderUnavailable
from pipeline.extract import STATUS_UNCLEAR, extract_update
from utilities.mailstore import CATEGORY_UPDATE

log = logging.getLogger(__name__)


class UpdateHandler:
    """Applies status changes carried by update emails.

    Extraction is a blocking model call and goes to an executor; resolving and
    writing the status stay on the calling thread, which owns the sqlite
    connection.
    """

    def __init__(self, store, mail, resolver, client=None, threshold=0.85,
                 executor=None):
        self.store = store
        self.mail = mail
        self.resolver = resolver
        self.client = client
        self.threshold = threshold
        self.executor = executor or asyncio.to_thread

    async def handle(self, message):
        """Process one update email.

        Returns a dict describing what happened, for logging and the activity
        feed: `linked`, `status_applied`, `status`, `identity_key`.

        Summary:
            Extract the status an update email reports, resolve it to a job,
            and apply it when both the model and the resolver are confident.

        Parameters:
            message (Mapping): The stored message row to process.

        Returns:
            dict: Under `linked`, `status_applied`, `status`, `identity_key`,
                and `ambiguous`.

        Raises:
            GroqRateLimited: Propagated from extraction so `run` can stop the
                batch cleanly.
        """
        outcome = {"linked": False, "status_applied": False,
                   "status": None, "identity_key": None, "ambiguous": False}

        extracted = {}
        if self.client is not None:
            extracted = await self.executor(
                extract_update, dict(message), self.client)

        resolution = self.resolver.resolve(dict(message), extracted)
        # Marked before the unresolved branch returns: an update about a role
        # that is not in the applications list will never resolve, however many
        # times it is retried, and retrying costs a model call each time.
        #
        # Unless there was no model to extract with. Then `extracted` is empty,
        # the resolver had only the sender domain, and calling that a failed
        # attempt would discard a status update because a provider was cooling
        # off.
        if resolution.resolved or self.client is not None:
            self.mail.mark_handled(message["gmail_message_id"])
        if not resolution.resolved:
            outcome["ambiguous"] = resolution.ambiguous
            log.info("Update %s unresolved: %s",
                     message["gmail_message_id"], resolution.reason)
            self.mail.commit()
            return outcome

        outcome["identity_key"] = resolution.identity_key
        outcome["linked"] = self.resolver.link(
            message["gmail_message_id"], resolution, CATEGORY_UPDATE)

        status = extracted.get("status", STATUS_UNCLEAR)
        outcome["status"] = status
        confidence = min(extracted.get("confidence", 0.0), resolution.confidence)

        if status != STATUS_UNCLEAR and confidence >= self.threshold:
            outcome["status_applied"] = self._apply(resolution.identity_key, status)
        elif status != STATUS_UNCLEAR:
            log.info(
                "Update %s suggests %s at confidence %.2f, below the %.2f "
                "threshold - linked but not applied",
                message["gmail_message_id"], status, confidence, self.threshold,
            )

        self.mail.commit()
        return outcome

    def _apply(self, identity_key, status):
        """Write the status through the reversible path.

        Captures the previous status and response date first so the change can
        be undone exactly, matching what `apply_ai_status` does for the legacy
        per-job scanner.
        """
        job = self.store.job_by_identity(identity_key)
        if job is None:
            # The identity resolved against a lead, not an application. A lead
            # cannot receive a status update - it has not been applied to - so
            # there is nothing to write. The link still stands.
            return False

        previous_status = job["status"]
        previous_response = job["response_date"]
        self.store.update_status(job["id"], status)
        self.store.save_profile_value(
            f"undo:{job['job_id']}",
            f"{previous_status}|{previous_response or ''}",
        )
        log.info("Job %s moved %s -> %s from an update email",
                 job["job_id"], previous_status, status)
        return True

    def undo(self, job_id):
        """Restore a job to the state recorded before an automatic write."""
        stored = self.store.get_profile_value(f"undo:{job_id}", "")
        if not stored:
            return False
        previous_status, _, previous_response = stored.partition("|")
        self.store.conn.execute(
            "UPDATE jobs SET status = ?, response_date = ? WHERE job_id = ?",
            (previous_status, previous_response or None, job_id),
        )
        self.store.save_profile_value(f"undo:{job_id}", "")
        self.store.conn.commit()
        return True

    async def run(self, limit=50):
        """
        Summary:
            Apply each unhandled update email to its job, stopping cleanly if
            the model's rate limit is reached.

        Parameters:
            limit (int): Most update emails to process in one pass.

        Returns:
            dict: Counts under `processed`, `applied`, and `unresolved`.

        Note:
            A rate limit ends the pass rather than failing the whole cycle.
            Unreached emails stay unlinked and are retried next cycle.
        """
        processed = applied = unresolved = 0
        for message in self._pending(limit):
            try:
                outcome = await self.handle(message)
            except ProviderUnavailable as exc:
                # Not a rate limit and not a parse failure: the request never
                # reached a model. Stop the pass and leave every message in it
                # untouched - crucially *without* `mark_handled`, because
                # nothing was tried, and a retry is exactly what these need.
                log.warning(
                    "Update handling stopped after %d message(s): no provider could "
                    "serve the request (%s). Nothing was marked handled, so the batch "
                    "retries next cycle.",
                    processed, exc,
                )
                break
            except GroqRateLimited as exc:
                log.info(
                    "Update handling paused by the rate limit after %d "
                    "message(s); retrying next cycle, in about %ss",
                    processed, exc.retry_after,
                )
                break
            processed += 1
            applied += int(outcome["status_applied"])
            unresolved += int(not outcome["identity_key"])
        return {"processed": processed, "applied": applied,
                "unresolved": unresolved}

    def _pending(self, limit):
        """Update emails not yet processed, oldest first.

        Summary:
            List the update emails this handler still has to process.

        Parameters:
            limit (int): Most rows to return.

        Returns:
            list[sqlite3.Row]: Unhandled update messages.

        Note:
            Oldest first, so a sequence of updates on one job is applied in the
            order it arrived rather than ending on the earliest status.
        """
        return self.mail.messages_awaiting_handling(
            CATEGORY_UPDATE, limit, newest_first=False)
