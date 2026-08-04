"""SQLite persistence for applications, and the helpers that derive Job IDs.

This module holds no UI code so it can be imported and tested headlessly.

Two identity schemes coexist here. `url_hash` is the original: a hash of the
normalized posting URL. `identity_key` (see `utilities/identity.py`) is the
current one, derived from title/company/location, because different boards
hand out different URLs for the same role. Rows created before the rework keep
their URL-derived `job_id` as a stable handle - `email_matches` references it -
while `identity_key` carries the actual identity.

The mailbox mirror, leads, and generated artifacts live in
`utilities/mailstore.py` and share this connection.
"""

import hashlib
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from urllib.parse import urlparse, urlunparse

from utilities.identity import candidate_keys, identity_key, identity_scheme
from utilities.migrations import initialise

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "job_applications.sqlite3")


def normalize_url(url):
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        parsed = urlparse(f"https://{url.strip()}")
    hostname = (parsed.hostname or "").lower()
    netloc = hostname
    if parsed.port:
        netloc = f"{hostname}:{parsed.port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), netloc, path, "", parsed.query, ""))


def url_hash(url):
    """creates a hash of the url to use as a unique identifier for the job posting"""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:12].upper()


def today_iso():
    return date.today().isoformat()


class JobStore:
    def __init__(self, db_path=None):
        # Resolved at call time rather than as a default argument value. A
        # default would bind DB_PATH at import, so tests that reassign it would
        # silently keep using the real database.
        self.db_path = db_path or DB_PATH
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.configure_connection()
        self.init_db()

    def configure_connection(self):
        """Pragmas for a process that stays up.

        WAL lets the maintenance CLI and the backup timer read while the app
        writes; without it either one blocks the poller. `busy_timeout` turns
        the remaining contention into a short wait instead of an immediate
        "database is locked". Both are no-ops on an in-memory test database.
        """
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA synchronous=NORMAL")

    def init_db(self):
        """Create or upgrade the schema.

        Delegates to `utilities/migrations.py`, which stamps a fresh database
        at the current version and runs the versioned upgrades on an existing
        one. Backups before a structural change happen there.
        """
        return initialise(self.conn, self.db_path)

    def close(self):
        """Release the connection. Long-running processes never call this;
        tests do, to keep ResourceWarnings out of the output."""
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False

    # Job records -----------------------------------------------------------

    def duplicate_jobs(self, posting_url):
        h = url_hash(posting_url)
        return self.conn.execute(
            "SELECT * FROM jobs WHERE url_hash = ? ORDER BY created_at DESC", (h,)
        ).fetchall()

    def next_job_id(self, posting_url):
        base = url_hash(posting_url)
        rows = self.conn.execute(
            "SELECT job_id FROM jobs WHERE url_hash = ? ORDER BY job_id", (base,)
        ).fetchall()
        if not rows:
            return base
        return f"{base}-{len(rows) + 1}"

    def next_identity_job_id(self, key):
        """Job ID for a row that has no posting URL to hash.

        Applies the same `-2` correlation suffix as `next_job_id` so a second
        genuinely-distinct posting under one identity can still be stored.
        """
        rows = self.conn.execute(
            "SELECT job_id FROM jobs WHERE job_id = ? OR job_id LIKE ?", (key, f"{key}-%")
        ).fetchall()
        return key if not rows else f"{key}-{len(rows) + 1}"

    def create_job(self, data):
        """Insert an application.

        `data` keeps the shape the add-application form has always sent, so the
        UI needs no change. Three keys are optional and default sensibly:
        `location` (new with the identity model), and `posting_url` (a job
        created from an acknowledgement email may never have had one).
        """
        now = datetime.now().isoformat(timespec="seconds")
        posting_url = (data.get("posting_url") or "").strip()
        location = (data.get("location") or "").strip()
        key = identity_key(data["position_title"], data.get("company"), location)

        if posting_url:
            job_id = self.next_job_id(posting_url)
            stored_hash = url_hash(posting_url)
            stored_url = normalize_url(posting_url)
        else:
            job_id = self.next_identity_job_id(key)
            stored_hash = None
            stored_url = None

        self.conn.execute(
            """
            INSERT INTO jobs (
                job_id, identity_key, identity_scheme, url_hash, posting_url,
                position_title, company, location, job_type,
                requires_oa, completed_oa, received_references, payment_amount,
                payment_period, status, application_date, response_date, notes,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                key,
                identity_scheme(location),
                stored_hash,
                stored_url,
                data["position_title"],
                data.get("company"),
                location or None,
                data["job_type"],
                int(data.get("requires_oa") or 0),
                int(data.get("completed_oa") or 0),
                int(data.get("received_references") or 0),
                data.get("payment_amount"),
                data.get("payment_period"),
                data["status"],
                data["application_date"],
                data.get("response_date"),
                data.get("notes"),
                now,
                now,
            ),
        )
        if posting_url:
            self.add_job_source(job_id, stored_url, data.get("board"),
                                data.get("board_job_id"))
        self.conn.commit()
        return job_id

    # Identity lookup -------------------------------------------------------

    def job_by_identity(self, key):
        return self.conn.execute(
            "SELECT * FROM jobs WHERE identity_key = ? ORDER BY id LIMIT 1", (key,)
        ).fetchone()

    def find_job(self, title, company, location=None):
        """Resolve a job from a role description.

        Tries the location-qualified key first, then the bare title+company
        key, so rows written before locations existed are still reachable. See
        `identity.candidate_keys`.
        """
        for key in candidate_keys(title, company, location):
            row = self.job_by_identity(key)
            if row is not None:
                return row
        return None

    def duplicate_identity_groups(self):
        """Jobs sharing one identity key, for the merge review page.

        Populated mainly by the v1 backfill, where the same role logged from
        two boards under two URLs collapses onto one key. Never merged
        automatically - a wrong merge destroys application history invisibly.
        """
        return self.conn.execute(
            """
            SELECT identity_key, COUNT(*) AS count,
                   GROUP_CONCAT(job_id) AS job_ids
            FROM jobs
            WHERE identity_key IS NOT NULL
            GROUP BY identity_key
            HAVING COUNT(*) > 1
            ORDER BY count DESC
            """
        ).fetchall()

    # Job sources -----------------------------------------------------------

    def add_job_source(self, job_id, url, board=None, board_job_id=None):
        """Record one board's URL for a job.

        A repeat of the same URL *enriches* rather than no-ops. `create_job`
        stores the URL before anything has parsed a board out of it, so the
        alert parser almost always arrives second with the useful half - and a
        plain INSERT OR IGNORE would throw that away, leaving
        `job_by_board_reference` permanently unable to find the row.

        Existing values are never overwritten with NULL.
        """
        self.conn.execute(
            """
            INSERT INTO job_sources (job_id, url, board, board_job_id, first_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(job_id, url) DO UPDATE SET
                board = COALESCE(excluded.board, board),
                board_job_id = COALESCE(excluded.board_job_id, board_job_id)
            """,
            (job_id, url, board, board_job_id,
             datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def job_sources(self, job_id):
        return self.conn.execute(
            "SELECT * FROM job_sources WHERE job_id = ? ORDER BY first_seen", (job_id,)
        ).fetchall()

    def job_by_board_reference(self, board, board_job_id):
        """Strongest resolution signal: the board's own ID for the posting."""
        if not board or not board_job_id:
            return None
        return self.conn.execute(
            """
            SELECT j.* FROM jobs j
            JOIN job_sources s ON s.job_id = j.job_id
            WHERE s.board = ? AND s.board_job_id = ?
            LIMIT 1
            """,
            (board, board_job_id),
        ).fetchone()

    def update_status(self, row_id, status):
        now = datetime.now().isoformat(timespec="seconds")
        response_date = today_iso() if status in {"Interview", "Offer", "Rejected"} else None
        self.conn.execute(
            """
            UPDATE jobs
            SET status = ?,
                response_date = COALESCE(response_date, ?),
                updated_at = ?
            WHERE id = ?
            """,
            (status, response_date, now, row_id),
        )
        self.conn.commit()

    def list_jobs(self):
        return self.conn.execute(
            "SELECT * FROM jobs ORDER BY application_date DESC, created_at DESC"
        ).fetchall()

    # Dashboard aggregates --------------------------------------------------

    def stats(self):
        rows = self.list_jobs()
        total = len(rows)
        heard_back = sum(1 for r in rows if r["status"] in {"OA Received", "Interview", "Offer", "Rejected"})
        offers = sum(1 for r in rows if r["status"] == "Offer")
        pending = sum(1 for r in rows if r["status"] in {"Pending", "Applied"})
        return {"total": total, "heard_back": heard_back, "offers": offers, "pending": pending}

    def daily_counts(self, days=14):
        if days is None:
            earliest = self.conn.execute(
                "SELECT MIN(application_date) AS earliest FROM jobs"
            ).fetchone()["earliest"]
            start = date.fromisoformat(earliest) if earliest else date.today()
            days = (date.today() - start).days + 1
        else:
            start = date.today() - timedelta(days=days - 1)
        labels = [(start + timedelta(days=i)).isoformat() for i in range(days)]
        counts = dict.fromkeys(labels, 0)
        rows = self.conn.execute(
            """
            SELECT application_date, COUNT(*) AS count
            FROM jobs
            WHERE application_date >= ?
            GROUP BY application_date
            """,
            (start.isoformat(),),
        ).fetchall()
        for row in rows:
            counts[row["application_date"]] = row["count"]
        return list(counts.items())

    def cumulative_counts(self, days=14):
        daily = self.daily_counts(days)
        if not daily:
            return daily
        base = self.conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE application_date < ?", (daily[0][0],)
        ).fetchone()["count"]
        result = []
        running = base
        for day, count in daily:
            running += count
            result.append((day, running))
        return result

    def status_counts(self):
        rows = self.list_jobs()
        buckets = {
            "Pending": 0,
            "OA Received": 0,
            "Interview": 0,
            "Offer": 0,
            "Rejected": 0,
            "Withdrawn": 0,
        }
        for row in rows:
            status = row["status"]
            if status in {"Pending", "Applied"}:
                buckets["Pending"] += 1
            elif status in buckets:
                buckets[status] += 1
        return buckets

    # Email matches ---------------------------------------------------------

    def jobs_awaiting_response(self):
        return self.conn.execute(
            """
            SELECT * FROM jobs
            WHERE status IN ('Pending', 'Applied', 'OA Received')
              AND (response_date IS NULL OR response_date = '')
            ORDER BY application_date DESC
            """
        ).fetchall()

    def record_email_match(self, job_id, message):
        """Store a suggested match. Ignores duplicates so re-scanning is safe.

        `snippet` and `body_text` are optional: a caller that only has headers,
        or whose body fetch failed, still records a usable match.
        """
        now = datetime.now().isoformat(timespec="seconds")
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO email_matches (
                job_id, gmail_message_id, sender, subject, received_date,
                snippet, body_text, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                message["id"],
                message["sender"],
                message["subject"],
                message["date"],
                message.get("snippet", ""),
                message.get("body", ""),
                now,
            ),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def pending_email_matches(self):
        return self.conn.execute(
            """
            SELECT m.*, j.position_title, j.company, j.status AS job_status
            FROM email_matches m
            JOIN jobs j ON j.job_id = m.job_id
            WHERE m.reviewed = 0 AND m.dismissed = 0
            ORDER BY m.created_at DESC
            """
        ).fetchall()

    def known_message_ids(self, job_id):
        rows = self.conn.execute(
            "SELECT gmail_message_id FROM email_matches WHERE job_id = ?", (job_id,)
        ).fetchall()
        return {row["gmail_message_id"] for row in rows}

    def confirm_email_match(self, match_id, status):
        """Apply a user-confirmed match to the job, then mark the match reviewed.

        The status write happens here rather than during scanning so nothing
        changes without the user explicitly choosing it.
        """
        match = self.conn.execute(
            "SELECT * FROM email_matches WHERE id = ?", (match_id,)
        ).fetchone()
        if match is None:
            return
        job = self.conn.execute(
            "SELECT id FROM jobs WHERE job_id = ?", (match["job_id"],)
        ).fetchone()
        if job is not None:
            self.update_status(job["id"], status)
        self.conn.execute(
            "UPDATE email_matches SET reviewed = 1 WHERE id = ?", (match_id,)
        )
        self.conn.commit()

    def dismiss_email_match(self, match_id):
        self.conn.execute(
            "UPDATE email_matches SET dismissed = 1 WHERE id = ?", (match_id,)
        )
        self.conn.commit()

    # AI classification -----------------------------------------------------

    def unclassified_email_matches(self, limit=None):
        """Pending matches that have body text but no classification yet.

        Rows already classified are skipped, so a cycle stopped by a rate limit
        resumes where it left off instead of spending quota on repeat work.
        """
        sql = """
            SELECT m.*, j.position_title, j.company, j.status AS job_status
            FROM email_matches m
            JOIN jobs j ON j.job_id = m.job_id
            WHERE m.reviewed = 0
              AND m.dismissed = 0
              AND m.ai_classified_at IS NULL
              AND m.body_text IS NOT NULL
              AND TRIM(m.body_text) <> ''
            ORDER BY m.created_at DESC
        """
        if limit is None:
            return self.conn.execute(sql).fetchall()
        return self.conn.execute(sql + " LIMIT ?", (limit,)).fetchall()

    def record_classification(self, match_id, label, confidence, reason):
        """Store what the model inferred. Applies nothing to the job."""
        self.conn.execute(
            """
            UPDATE email_matches
            SET ai_status = ?, ai_confidence = ?, ai_reason = ?, ai_classified_at = ?
            WHERE id = ?
            """,
            (
                label,
                confidence,
                reason,
                datetime.now().isoformat(timespec="seconds"),
                match_id,
            ),
        )
        self.conn.commit()

    def apply_ai_status(self, match_id, status):
        """Apply a confident classification, remembering what it replaced.

        The previous status and response date are captured before the write so
        undo_ai_status can restore the job exactly. Without them an auto-applied
        Rejected would be unrecoverable: update_status stamps a response date,
        and jobs_awaiting_response then drops the job from future Gmail scans.
        """
        match = self.conn.execute(
            "SELECT job_id FROM email_matches WHERE id = ?", (match_id,)
        ).fetchone()
        if match is None:
            return False
        job = self.conn.execute(
            "SELECT id, status, response_date FROM jobs WHERE job_id = ?",
            (match["job_id"],),
        ).fetchone()
        if job is None:
            return False
        self.conn.execute(
            """
            UPDATE email_matches
            SET ai_applied = 1, ai_previous_status = ?, ai_previous_response_date = ?
            WHERE id = ?
            """,
            (job["status"], job["response_date"], match_id),
        )
        self.update_status(job["id"], status)
        self.conn.commit()
        return True

    def undo_ai_status(self, match_id):
        """Put the job back exactly as it stood before the AI touched it."""
        match = self.conn.execute(
            "SELECT * FROM email_matches WHERE id = ?", (match_id,)
        ).fetchone()
        if match is None or not match["ai_applied"]:
            return False
        self.conn.execute(
            "UPDATE jobs SET status = ?, response_date = ?, updated_at = ? WHERE job_id = ?",
            (
                match["ai_previous_status"],
                match["ai_previous_response_date"],
                datetime.now().isoformat(timespec="seconds"),
                match["job_id"],
            ),
        )
        # The classification is kept: undo means the label was wrong, so the
        # message must not be picked up and reclassified on the next cycle.
        self.conn.execute(
            """
            UPDATE email_matches
            SET ai_applied = 0, ai_previous_status = NULL, ai_previous_response_date = NULL
            WHERE id = ?
            """,
            (match_id,),
        )
        self.conn.commit()
        return True

    # Profile key/value -----------------------------------------------------

    def save_profile_value(self, key, value):
        self.conn.execute(
            """
            INSERT INTO profile (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def get_profile_value(self, key, default=""):
        row = self.conn.execute("SELECT value FROM profile WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
