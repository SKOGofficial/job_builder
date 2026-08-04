"""Job alerts to leads.

One alert email carries five to ten postings, so this is the stage that links
one message to many identities - the reason `message_links` is a link table
rather than a column on `messages`.

Two guards matter here:

- **Dedupe against applications.** Boards keep recommending a role you applied
  to three weeks ago. A to-apply list that resurrects finished work stops being
  trusted, and untrusted lists get ignored.
- **Never create a `jobs` row.** An alert is an advert, not evidence of
  anything. Only an acknowledgement email or an explicit user action promotes a
  lead into an application, or the dashboard fills with roles nobody applied to
  and every metric that divides by application count becomes meaningless.
"""

import logging

from clients.llm_client import GroqRateLimited
from pipeline.parsers import parse_alert
from utilities.identity import identity_key, identity_scheme
from utilities.mailstore import CATEGORY_ALERT

log = logging.getLogger(__name__)


class AlertHandler:
    def __init__(self, store, mail, client=None):
        self.store = store
        self.mail = mail
        self.client = client

    def handle(self, message):
        """Process one alert email. Returns (created, skipped, linked)."""
        postings = parse_alert(dict(message), self.client)
        if not postings:
            log.info("No postings extracted from alert %s",
                     message["gmail_message_id"])
            return 0, 0, 0

        created = skipped = linked = 0
        for posting in postings:
            key = identity_key(posting.title, posting.company, posting.location)

            # Already applied to this role - do not resurrect it as a lead.
            if self.store.job_by_identity(key) is not None:
                skipped += 1
                if self.mail.link_message(message["gmail_message_id"], key,
                                          CATEGORY_ALERT, 1.0, "alert_parser"):
                    linked += 1
                continue

            is_new = self.mail.upsert_lead({
                "identity_key": key,
                "identity_scheme": identity_scheme(posting.location),
                "title": posting.title,
                "company": posting.company,
                "location": posting.location,
                "apply_url": posting.apply_url,
                "tracking_url": posting.tracking_url,
                "board": posting.board,
                "board_job_id": posting.board_job_id,
                "source_message_id": message["gmail_message_id"],
            })
            created += int(is_new)

            # Link whether the lead is new or a repeat sighting: the timeline
            # should show every alert that surfaced this role, and the link
            # survives promotion because it points at the identity.
            if self.mail.link_message(message["gmail_message_id"], key,
                                      CATEGORY_ALERT, 1.0, "alert_parser"):
                linked += 1

        self.mail.commit()
        log.info("Alert %s produced %d new lead(s), %d already applied to",
                 message["gmail_message_id"], created, skipped)
        return created, skipped, linked

    def run(self, limit=50):
        """Process every alert email that has not been linked yet.

        Summary:
            Turn each unhandled alert email into leads, stopping cleanly if the
            model's rate limit is reached.

        Parameters:
            limit (int): Most alert emails to process in one pass.

        Returns:
            tuple[int, int, int]: Leads created, postings skipped because they
                are already applied to, and message links written.

        Note:
            A rate limit ends the pass rather than failing it. Everything
            already written is kept, and the emails not reached stay unlinked,
            so the next cycle picks them up with a fresh token budget.
        """
        totals = [0, 0, 0]
        for message in self._pending(limit):
            try:
                created, skipped, linked = self.handle(message)
            except GroqRateLimited as exc:
                log.info(
                    "Alert handling paused by the rate limit after %d lead(s); "
                    "the remaining alerts retry next cycle, in about %ss",
                    totals[0], exc.retry_after,
                )
                break
            totals[0] += created
            totals[1] += skipped
            totals[2] += linked
        return tuple(totals)

    def _pending(self, limit):
        return self.mail.conn.execute(
            """
            SELECT * FROM messages
            WHERE category = ?
              AND gmail_message_id NOT IN (SELECT gmail_message_id FROM message_links)
            ORDER BY received_ts DESC
            LIMIT ?
            """,
            (CATEGORY_ALERT, limit),
        ).fetchall()
