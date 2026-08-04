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

import logging

from pipeline.extract import STATUS_UNCLEAR, extract_update
from utilities.mailstore import CATEGORY_UPDATE

log = logging.getLogger(__name__)


class UpdateHandler:
    def __init__(self, store, mail, resolver, client=None, threshold=0.85):
        self.store = store
        self.mail = mail
        self.resolver = resolver
        self.client = client
        self.threshold = threshold

    def handle(self, message):
        """Process one update email.

        Returns a dict describing what happened, for logging and the activity
        feed: `linked`, `status_applied`, `status`, `identity_key`.
        """
        outcome = {"linked": False, "status_applied": False,
                   "status": None, "identity_key": None, "ambiguous": False}

        extracted = {}
        if self.client is not None:
            extracted = extract_update(dict(message), self.client)

        resolution = self.resolver.resolve(dict(message), extracted)
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

    def run(self, limit=50):
        processed = applied = unresolved = 0
        for message in self._pending(limit):
            outcome = self.handle(message)
            processed += 1
            applied += int(outcome["status_applied"])
            unresolved += int(not outcome["identity_key"])
        return {"processed": processed, "applied": applied,
                "unresolved": unresolved}

    def _pending(self, limit):
        return self.mail.conn.execute(
            """
            SELECT * FROM messages
            WHERE category = ?
              AND gmail_message_id NOT IN (SELECT gmail_message_id FROM message_links)
            ORDER BY received_ts ASC
            LIMIT ?
            """,
            (CATEGORY_UPDATE, limit),
        ).fetchall()
