"""Persistence for the mailbox mirror, leads, and generated artifacts.

Split from `store.py` rather than bolted onto `JobStore` for two reasons: the
tables here belong to the ingest pipeline rather than to application logging,
and keeping them apart means UI work on the applications side and pipeline work
do not keep colliding in one very large module.

Both classes share a single sqlite connection - the split is organisational,
not transactional, so an operation spanning both (promoting a lead into a job)
is still one atomic unit.

The linking model is the part worth understanding before reading further.
`message_links` points at an `identity_key`, never at a `jobs.id` or a
`job_leads.id`. A lead that is later promoted to a real application keeps its
identity, so every email already attached to it stays attached with no
migration and no re-linking pass. That is why the job detail page can show the
alert email that first surfaced the role alongside the rejection that closed
it.
"""

import json
import logging
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

log = logging.getLogger(__name__)

# --- vocabulary --------------------------------------------------------------
#
# Kept here rather than in `theme.py` because these are pipeline states with no
# presentation meaning, and `theme.py` is heavily imported by the UI.

CATEGORY_ALERT = "job_alert"
CATEGORY_UPDATE = "job_update"
CATEGORY_ACKNOWLEDGEMENT = "job_acknowledgement"
CATEGORY_IRRELEVANT = "irrelevant"

#: Categories that describe a real job. `irrelevant` is a common and expected
#: outcome, not an error - the rough filter deliberately passes plenty of
#: non-job mail through for the model to reject.
JOB_CATEGORIES = (CATEGORY_ALERT, CATEGORY_UPDATE, CATEGORY_ACKNOWLEDGEMENT)
CATEGORIES = JOB_CATEGORIES + (CATEGORY_IRRELEVANT,)

VERDICT_PASSED = "passed"

LEAD_NEW = "new"
LEAD_PREPARING = "preparing"
LEAD_READY = "ready"
LEAD_DISMISSED = "dismissed"
LEAD_APPLIED = "applied"
LEAD_STATUSES = (LEAD_NEW, LEAD_PREPARING, LEAD_READY, LEAD_DISMISSED, LEAD_APPLIED)

#: Statuses the to-apply list shows by default. `ready` first - that is the
#: state the user can actually act on.
LEAD_OPEN_STATUSES = (LEAD_READY, LEAD_PREPARING, LEAD_NEW)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def parse_received(raw):
    """RFC 2822 Date header to a sortable epoch, or None.

    Gmail hands back whatever the sender wrote, which includes malformed dates
    and exotic timezones. A message with an unparseable date is still worth
    keeping, so this degrades to None rather than raising and losing the row.
    """
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    try:
        return int(parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


class MailStore:
    """Pipeline-side persistence. Shares `JobStore`'s connection."""

    def __init__(self, conn):
        self.conn = conn

    # --- messages ----------------------------------------------------------

    def has_message(self, gmail_message_id):
        row = self.conn.execute(
            "SELECT 1 FROM messages WHERE gmail_message_id = ?", (gmail_message_id,)
        ).fetchone()
        return row is not None

    def known_message_ids(self, candidate_ids):
        """Which of these IDs are already stored.

        Bulk form so a sync pass can skip everything it has seen in one query
        rather than one round trip per message.
        """
        ids = list(candidate_ids)
        if not ids:
            return set()
        found = set()
        # SQLite caps host parameters (999 on older builds), so chunk.
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ", ".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"SELECT gmail_message_id FROM messages WHERE gmail_message_id IN ({marks})",
                tuple(chunk),
            ).fetchall()
            found.update(row["gmail_message_id"] for row in rows)
        return found

    def upsert_message(self, header):
        """Store a message's headers. Returns True when the row is new.

        Bodies are a separate call (`store_body`) made only after the rough
        filter has passed the message, so a dropped message costs one metadata
        fetch and nothing more.
        """
        received = header.get("date")
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO messages (
                gmail_message_id, thread_id, sender, subject, received_date,
                received_ts, labels, list_unsubscribe, snippet, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                header["id"],
                header.get("thread_id"),
                header.get("sender", ""),
                header.get("subject", ""),
                received,
                parse_received(received),
                json.dumps(header.get("labels") or []),
                header.get("list_unsubscribe", ""),
                header.get("snippet", ""),
                _now(),
            ),
        )
        return cursor.rowcount > 0

    def set_filter_verdict(self, gmail_message_id, verdict):
        self.conn.execute(
            "UPDATE messages SET filter_verdict = ? WHERE gmail_message_id = ?",
            (verdict, gmail_message_id),
        )

    def messages_awaiting_body(self, limit=50):
        """Passed the rough filter, body not downloaded yet."""
        return self.conn.execute(
            """
            SELECT * FROM messages
            WHERE filter_verdict = ? AND body_text IS NULL
            ORDER BY received_ts DESC
            LIMIT ?
            """,
            (VERDICT_PASSED, limit),
        ).fetchall()

    def store_body(self, gmail_message_id, body, snippet=None):
        self.conn.execute(
            """
            UPDATE messages
            SET body_text = ?,
                snippet = COALESCE(?, snippet),
                body_fetched_at = ?
            WHERE gmail_message_id = ?
            """,
            (body, snippet, _now(), gmail_message_id),
        )

    def messages_awaiting_classification(self, limit=None):
        """Has a body, has not been classified.

        Ordered oldest first so a resumed backfill makes forward progress
        through the backlog rather than re-walking the newest mail.
        """
        sql = """
            SELECT * FROM messages
            WHERE body_text IS NOT NULL
              AND TRIM(body_text) <> ''
              AND category IS NULL
            ORDER BY received_ts ASC
        """
        if limit is None:
            return self.conn.execute(sql).fetchall()
        return self.conn.execute(sql + " LIMIT ?", (limit,)).fetchall()

    def count_awaiting_classification(self):
        return self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM messages
            WHERE body_text IS NOT NULL AND TRIM(body_text) <> '' AND category IS NULL
            """
        ).fetchone()["n"]

    def record_category(self, gmail_message_id, category, confidence, reason):
        self.conn.execute(
            """
            UPDATE messages
            SET category = ?, category_confidence = ?, category_reason = ?,
                classified_at = ?
            WHERE gmail_message_id = ?
            """,
            (category, confidence, reason, _now(), gmail_message_id),
        )

    def message(self, gmail_message_id):
        return self.conn.execute(
            "SELECT * FROM messages WHERE gmail_message_id = ?", (gmail_message_id,)
        ).fetchone()

    def messages_by_category(self, category, limit=100):
        return self.conn.execute(
            """
            SELECT * FROM messages WHERE category = ?
            ORDER BY received_ts DESC LIMIT ?
            """,
            (category, limit),
        ).fetchall()

    # --- observability -----------------------------------------------------

    def filter_stats(self):
        """Drop counts per rule.

        Worth surfacing: if the denylist rule stops growing, the "not job
        related" button is not discoverable enough, and the LLM is being paid
        to reject the same newsletters every day.
        """
        rows = self.conn.execute(
            """
            SELECT COALESCE(filter_verdict, 'unfiltered') AS verdict, COUNT(*) AS count
            FROM messages GROUP BY verdict ORDER BY count DESC
            """
        ).fetchall()
        return {row["verdict"]: row["count"] for row in rows}

    def category_stats(self):
        rows = self.conn.execute(
            """
            SELECT COALESCE(category, 'unclassified') AS category, COUNT(*) AS count
            FROM messages GROUP BY category ORDER BY count DESC
            """
        ).fetchall()
        return {row["category"]: row["count"] for row in rows}

    # --- retention ---------------------------------------------------------

    def prune_bodies(self, older_than_days=30):
        """Drop stored bodies for irrelevant mail past the retention window.

        Keeps the ID, headers, and classification so the message is never
        re-fetched or re-classified - only the bulk goes. Linked messages are
        never pruned regardless of age: they are the per-role timeline the user
        actually reads.

        Returns the number of bodies cleared. Callers should VACUUM afterwards
        or the file never shrinks.
        """
        cutoff = int((datetime.now() - timedelta(days=older_than_days)).timestamp())
        cursor = self.conn.execute(
            """
            UPDATE messages
            SET body_text = NULL
            WHERE body_text IS NOT NULL
              AND category = ?
              AND (received_ts IS NULL OR received_ts < ?)
              AND gmail_message_id NOT IN (SELECT gmail_message_id FROM message_links)
            """,
            (CATEGORY_IRRELEVANT, cutoff),
        )
        self.conn.commit()
        return cursor.rowcount

    # --- links -------------------------------------------------------------

    def link_message(self, gmail_message_id, identity_key, link_type,
                     confidence=None, resolved_by=None):
        """Attach a message to a job identity. Idempotent."""
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO message_links (
                gmail_message_id, identity_key, link_type, confidence,
                resolved_by, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (gmail_message_id, identity_key, link_type, confidence, resolved_by, _now()),
        )
        return cursor.rowcount > 0

    def unlink_message(self, gmail_message_id, identity_key):
        """Undo a link. Needed because an auto-link can be wrong."""
        cursor = self.conn.execute(
            "DELETE FROM message_links WHERE gmail_message_id = ? AND identity_key = ?",
            (gmail_message_id, identity_key),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def links_for_message(self, gmail_message_id):
        return self.conn.execute(
            "SELECT * FROM message_links WHERE gmail_message_id = ?", (gmail_message_id,)
        ).fetchall()

    def messages_for_identity(self, identity_key):
        """The per-role email timeline, oldest first.

        This is what the job detail page renders: acknowledgement, then OA
        invite, then rejection, in the order they arrived.
        """
        return self.conn.execute(
            """
            SELECT m.*, l.link_type, l.confidence, l.resolved_by
            FROM messages m
            JOIN message_links l ON l.gmail_message_id = m.gmail_message_id
            WHERE l.identity_key = ?
            ORDER BY m.received_ts ASC, m.gmail_message_id ASC
            """,
            (identity_key,),
        ).fetchall()

    def unlinked_messages(self, limit=100):
        """Job-related mail the resolver could not place.

        The review queue. Without it these messages are classified, stored, and
        attached to nothing - which looks exactly like the pipeline working.
        """
        marks = ", ".join("?" for _ in JOB_CATEGORIES)
        return self.conn.execute(
            f"""
            SELECT * FROM messages
            WHERE category IN ({marks})
              AND gmail_message_id NOT IN (SELECT gmail_message_id FROM message_links)
            ORDER BY received_ts DESC
            LIMIT ?
            """,
            (*JOB_CATEGORIES, limit),
        ).fetchall()

    def count_unlinked(self):
        marks = ", ".join("?" for _ in JOB_CATEGORIES)
        return self.conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM messages
            WHERE category IN ({marks})
              AND gmail_message_id NOT IN (SELECT gmail_message_id FROM message_links)
            """,
            JOB_CATEGORIES,
        ).fetchone()["n"]

    # --- leads -------------------------------------------------------------

    def upsert_lead(self, lead):
        """Create a lead, or refresh the source details of an existing one.

        Unique on `identity_key`, so the same posting arriving from three
        boards over three days produces one row. A repeat sighting refreshes
        the apply URL (the older one may have expired) but never resets status
        or relevance - that would resurrect a lead the user dismissed.
        """
        now = _now()
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO job_leads (
                identity_key, identity_scheme, title, company, location,
                apply_url, tracking_url, board, board_job_id, source_message_id,
                status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lead["identity_key"],
                lead.get("identity_scheme"),
                lead["title"],
                lead.get("company"),
                lead.get("location"),
                lead.get("apply_url"),
                lead.get("tracking_url"),
                lead.get("board"),
                lead.get("board_job_id"),
                lead.get("source_message_id"),
                lead.get("status", LEAD_NEW),
                now,
                now,
            ),
        )
        if cursor.rowcount == 0:
            self.conn.execute(
                """
                UPDATE job_leads
                SET apply_url = COALESCE(?, apply_url),
                    tracking_url = COALESCE(?, tracking_url),
                    updated_at = ?
                WHERE identity_key = ?
                """,
                (lead.get("apply_url"), lead.get("tracking_url"), now,
                 lead["identity_key"]),
            )
        return cursor.rowcount > 0

    def lead_by_identity(self, identity_key):
        return self.conn.execute(
            "SELECT * FROM job_leads WHERE identity_key = ?", (identity_key,)
        ).fetchone()

    def lead(self, lead_id):
        return self.conn.execute(
            "SELECT * FROM job_leads WHERE id = ?", (lead_id,)
        ).fetchone()

    def list_leads(self, statuses=LEAD_OPEN_STATUSES):
        """The to-apply list. Ready rows first, then newest."""
        if statuses is None:
            return self.conn.execute(
                "SELECT * FROM job_leads ORDER BY created_at DESC"
            ).fetchall()
        marks = ", ".join("?" for _ in statuses)
        return self.conn.execute(
            f"""
            SELECT * FROM job_leads
            WHERE status IN ({marks})
            ORDER BY CASE status WHEN 'ready' THEN 0 ELSE 1 END,
                     relevance_score DESC NULLS LAST,
                     created_at DESC
            """,
            tuple(statuses),
        ).fetchall()

    def set_lead_status(self, lead_id, status, error=None):
        self.conn.execute(
            "UPDATE job_leads SET status = ?, prepare_error = ?, updated_at = ? WHERE id = ?",
            (status, error, _now(), lead_id),
        )
        self.conn.commit()

    def set_lead_relevance(self, lead_id, score, reason):
        self.conn.execute(
            """
            UPDATE job_leads
            SET relevance_score = ?, relevance_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (score, reason, _now(), lead_id),
        )
        self.conn.commit()

    def leads_awaiting_relevance(self, limit=50):
        return self.conn.execute(
            """
            SELECT * FROM job_leads
            WHERE relevance_score IS NULL AND status = ?
            ORDER BY created_at ASC LIMIT ?
            """,
            (LEAD_NEW, limit),
        ).fetchall()

    def leads_awaiting_preparation(self, threshold, limit=10):
        """Scored above the bar, not yet prepared.

        The gate that keeps Opus spend proportional: only leads the cheap model
        thinks are worth pursuing reach the expensive research pass.
        """
        return self.conn.execute(
            """
            SELECT * FROM job_leads
            WHERE status = ? AND relevance_score IS NOT NULL AND relevance_score >= ?
            ORDER BY relevance_score DESC, created_at ASC
            LIMIT ?
            """,
            (LEAD_NEW, threshold, limit),
        ).fetchall()

    # --- research ----------------------------------------------------------

    def save_research(self, identity_key, summary, payload, model=None,
                      input_tokens=None, output_tokens=None):
        self.conn.execute(
            """
            INSERT INTO job_research (
                identity_key, summary, payload, model, input_tokens, output_tokens, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_key) DO UPDATE SET
                summary = excluded.summary,
                payload = excluded.payload,
                model = excluded.model,
                input_tokens = excluded.input_tokens,
                output_tokens = excluded.output_tokens,
                fetched_at = excluded.fetched_at
            """,
            (identity_key, summary,
             json.dumps(payload) if payload is not None else None,
             model, input_tokens, output_tokens, _now()),
        )
        self.conn.commit()

    def research_for(self, identity_key):
        return self.conn.execute(
            "SELECT * FROM job_research WHERE identity_key = ?", (identity_key,)
        ).fetchone()

    def research_spend_since(self, since_iso):
        """Token spend since a timestamp, for the daily ceiling in 6.5."""
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COUNT(*) AS calls
            FROM job_research WHERE fetched_at >= ?
            """,
            (since_iso,),
        ).fetchone()
        return dict(row)

    # --- artifacts ---------------------------------------------------------

    def save_artifact(self, identity_key, kind, path, model=None):
        self.conn.execute(
            """
            INSERT INTO job_artifacts (identity_key, kind, path, model, generated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(identity_key, kind) DO UPDATE SET
                path = excluded.path,
                model = excluded.model,
                generated_at = excluded.generated_at
            """,
            (identity_key, kind, path, model, _now()),
        )
        self.conn.commit()

    def artifacts_for(self, identity_key):
        return self.conn.execute(
            "SELECT * FROM job_artifacts WHERE identity_key = ? ORDER BY kind",
            (identity_key,),
        ).fetchall()

    # --- experiences -------------------------------------------------------

    def add_experience(self, entry):
        cursor = self.conn.execute(
            """
            INSERT INTO experiences (
                kind, organisation, role, start_date, end_date, bullet, tags,
                impact, sort_order, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.get("kind", "work"),
                entry.get("organisation"),
                entry.get("role"),
                entry.get("start_date"),
                entry.get("end_date"),
                entry["bullet"],
                entry.get("tags"),
                entry.get("impact"),
                entry.get("sort_order", 0),
                _now(),
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def list_experiences(self, kind=None):
        if kind:
            return self.conn.execute(
                """
                SELECT * FROM experiences WHERE kind = ?
                ORDER BY sort_order, COALESCE(end_date, '9999'), id
                """,
                (kind,),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM experiences ORDER BY sort_order, COALESCE(end_date, '9999'), id"
        ).fetchall()

    def delete_experience(self, experience_id):
        self.conn.execute("DELETE FROM experiences WHERE id = ?", (experience_id,))
        self.conn.commit()

    # --- sender denylist ---------------------------------------------------

    def deny_sender(self, domain, reason=None):
        """Mark a domain as never job related. Feeds rough-filter rule 3."""
        domain = (domain or "").strip().lower()
        if not domain:
            return False
        self.conn.execute(
            "INSERT OR IGNORE INTO sender_denylist (domain, added_at, reason) VALUES (?, ?, ?)",
            (domain, _now(), reason),
        )
        self.conn.commit()
        return True

    def allow_sender(self, domain):
        self.conn.execute(
            "DELETE FROM sender_denylist WHERE domain = ?", ((domain or "").strip().lower(),)
        )
        self.conn.commit()

    def denied_domains(self):
        return {
            row["domain"]
            for row in self.conn.execute("SELECT domain FROM sender_denylist")
        }

    # --- cursors -----------------------------------------------------------
    #
    # Sync state (Gmail historyId, backfill position) lives in the existing
    # profile key/value table under a prefix, rather than earning a table of
    # its own for three rows.

    CURSOR_PREFIX = "cursor:"

    def get_cursor(self, name, default=None):
        row = self.conn.execute(
            "SELECT value FROM profile WHERE key = ?", (self.CURSOR_PREFIX + name,)
        ).fetchone()
        return row["value"] if row else default

    def set_cursor(self, name, value):
        self.conn.execute(
            """
            INSERT INTO profile (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (self.CURSOR_PREFIX + name, None if value is None else str(value)),
        )
        self.conn.commit()

    def commit(self):
        self.conn.commit()
