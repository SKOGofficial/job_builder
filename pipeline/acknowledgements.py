"""Acknowledgements: the bridge between the two lists.

"Thanks for applying" is the signal that a role belongs on the applied list and
no longer on the to-apply list. Three cases, in priority order:

- **Matches a lead** -> promote it. The role leaves the to-apply list and
  appears as an application with its email history already attached, because
  `message_links` points at the identity and the identity survives promotion.
  This is the main path and the one worth getting right.
- **Matches an existing job** -> move it to Applied if it is still Pending, and
  backfill the application date if it is missing.
- **Matches nothing** -> create the job. An acknowledgement is evidence the
  application really was submitted, which makes this the one place
  auto-creating a `jobs` row is correct. An alert is just an advert; this is a
  receipt.

Dates come from the email, never from `today_iso()`. Mail gets processed late -
after downtime, during a backfill - and a wrong application date quietly
corrupts every time series on the dashboard.
"""

import logging
from datetime import date, datetime

from clients.llm_client import GroqRateLimited
from pipeline.extract import extract_acknowledgement
from utilities.identity import identity_key, identity_scheme
from utilities.mailstore import (
    CATEGORY_ACKNOWLEDGEMENT,
    LEAD_APPLIED,
    parse_received,
)

log = logging.getLogger(__name__)

#: Statuses that mean "submitted, no decision yet". An acknowledgement should
#: not drag a job backwards out of Interview or Offer just because a delayed
#: receipt arrived late.
PRE_DECISION_STATUSES = {"Pending", "Applied"}


def application_date_from(message):
    """The date the email says, falling back to today.

    A stamp of `today_iso()` on a message received three weeks ago puts a false
    spike in the applications-over-time chart, so the header wins whenever it
    parses.
    """
    stamp = parse_received(message.get("received_date"))
    if stamp is None:
        return date.today().isoformat()
    return datetime.fromtimestamp(stamp).date().isoformat()


class AcknowledgementHandler:
    def __init__(self, store, mail, resolver, client=None):
        self.store = store
        self.mail = mail
        self.resolver = resolver
        self.client = client

    def handle(self, message):
        """Process one acknowledgement email.

        Returns `{"action": promoted|updated|created|unresolved, "job_id": ...}`.
        """
        extracted = {}
        if self.client is not None:
            extracted = extract_acknowledgement(dict(message), self.client)

        resolution = self.resolver.resolve(dict(message), extracted)
        applied_on = application_date_from(dict(message))

        if resolution.resolved:
            key = resolution.identity_key
            self.resolver.link(message["gmail_message_id"], resolution,
                               CATEGORY_ACKNOWLEDGEMENT)
            result = self._apply_to_identity(key, applied_on)
            self.mail.commit()
            return result

        # Unresolved but the model read a title and company: this is a role we
        # have never seen, so create it. That is the receipt case.
        if extracted.get("title") and extracted.get("company"):
            key = identity_key(extracted["title"], extracted["company"],
                               extracted.get("location"))
            self.mail.link_message(message["gmail_message_id"], key,
                                   CATEGORY_ACKNOWLEDGEMENT,
                                   extracted.get("confidence", 0.5),
                                   "acknowledgement_extract")
            job_id = self._create_job(extracted, applied_on)
            self.mail.commit()
            return {"action": "created", "job_id": job_id, "identity_key": key}

        log.info("Acknowledgement %s unresolved: %s",
                 message["gmail_message_id"], resolution.reason)
        self.mail.commit()
        return {"action": "unresolved", "job_id": None,
                "identity_key": None, "ambiguous": resolution.ambiguous}

    def _apply_to_identity(self, key, applied_on):
        lead = self.mail.lead_by_identity(key)
        if lead is not None and lead["status"] != LEAD_APPLIED:
            job_id = self.promote_lead(lead, applied_on)
            return {"action": "promoted", "job_id": job_id, "identity_key": key}

        job = self.store.job_by_identity(key)
        if job is not None:
            self._mark_applied(job, applied_on)
            return {"action": "updated", "job_id": job["job_id"],
                    "identity_key": key}

        # Identity resolved against a lead already marked applied, or a row
        # that vanished between resolution and now.
        return {"action": "noop", "job_id": None, "identity_key": key}

    def promote_lead(self, lead, applied_on=None):
        """Turn a lead into an application.

        The identity carries over unchanged, which is the whole point: every
        `message_links` row already attached to the lead now resolves to the
        job, so the alert email that first surfaced the role stays on its
        timeline.

        Prefers the lead's own fields over anything extracted from the email.
        The board parser produced clean structured data; an acknowledgement
        often says something looser like "your application to our Engineering
        team".
        """
        applied_on = applied_on or date.today().isoformat()
        job_id = self.store.create_job({
            "posting_url": lead["apply_url"] or "",
            "position_title": lead["title"],
            "company": lead["company"],
            "location": lead["location"] or "",
            "job_type": "Full time",
            "status": "Applied",
            "application_date": applied_on,
            "notes": self._provenance(lead),
            "board": lead["board"],
            "board_job_id": lead["board_job_id"],
        })
        if lead["apply_url"]:
            self.store.add_job_source(job_id, lead["apply_url"], lead["board"],
                                      lead["board_job_id"])
        self.mail.set_lead_status(lead["id"], LEAD_APPLIED)
        log.info("Promoted lead %s (%s at %s) to application %s",
                 lead["id"], lead["title"], lead["company"], job_id)
        return job_id

    @staticmethod
    def _provenance(lead):
        parts = ["Created from an acknowledgement email."]
        if lead["board"]:
            parts.append(f"Sourced from {lead['board']}.")
        return " ".join(parts)

    def _mark_applied(self, job, applied_on):
        """Move a tracked job to Applied without dragging it backwards."""
        updates = []
        values = []
        if job["status"] in PRE_DECISION_STATUSES and job["status"] != "Applied":
            updates.append("status = ?")
            values.append("Applied")
        if not job["application_date"]:
            updates.append("application_date = ?")
            values.append(applied_on)
        if not updates:
            return False
        updates.append("updated_at = ?")
        values.append(datetime.now().isoformat(timespec="seconds"))
        values.append(job["id"])
        self.store.conn.execute(
            f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?", tuple(values)
        )
        self.store.conn.commit()
        return True

    def _create_job(self, extracted, applied_on):
        job_id = self.store.create_job({
            "posting_url": "",
            "position_title": extracted["title"],
            "company": extracted["company"],
            "location": extracted.get("location") or "",
            "job_type": "Full time",
            "status": "Applied",
            "application_date": applied_on,
            "notes": "Created from an acknowledgement email.",
        })
        log.info("Created application %s from an acknowledgement for %s at %s",
                 job_id, extracted["title"], extracted["company"])
        return job_id

    def run(self, limit=50):
        """
        Summary:
            Process each unhandled acknowledgement email, stopping cleanly if
            the model's rate limit is reached.

        Parameters:
            limit (int): Most acknowledgement emails to process in one pass.

        Returns:
            dict: Count per action taken - `promoted`, `updated`, `created`,
                `unresolved`, or `noop`.

        Note:
            A rate limit ends the pass rather than failing the whole cycle.
            Promotions already written are kept; unreached emails stay unlinked
            and are retried next cycle.
        """
        counts = {}
        for message in self._pending(limit):
            try:
                result = self.handle(message)
            except GroqRateLimited as exc:
                log.info(
                    "Acknowledgement handling paused by the rate limit after "
                    "%d message(s); retrying next cycle, in about %ss",
                    sum(counts.values()), exc.retry_after,
                )
                break
            counts[result["action"]] = counts.get(result["action"], 0) + 1
        return counts

    def _pending(self, limit):
        return self.mail.conn.execute(
            """
            SELECT * FROM messages
            WHERE category = ?
              AND gmail_message_id NOT IN (SELECT gmail_message_id FROM message_links)
            ORDER BY received_ts ASC
            LIMIT ?
            """,
            (CATEGORY_ACKNOWLEDGEMENT, limit),
        ).fetchall()
