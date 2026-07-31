"""SQLite persistence and the pure helpers that derive Job IDs.

This module holds no Tkinter code so it can be imported and tested headlessly.
"""

import hashlib
import os
import sqlite3
from datetime import date, datetime, timedelta
from urllib.parse import urlparse, urlunparse

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
        self.conn = sqlite3.connect(db_path or DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.init_db()

    def init_db(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL UNIQUE,
                url_hash TEXT NOT NULL,
                posting_url TEXT NOT NULL,
                position_title TEXT NOT NULL,
                company TEXT,
                job_type TEXT NOT NULL,
                requires_oa INTEGER NOT NULL DEFAULT 0,
                completed_oa INTEGER NOT NULL DEFAULT 0,
                received_references INTEGER NOT NULL DEFAULT 0,
                payment_amount TEXT,
                payment_period TEXT,
                status TEXT NOT NULL,
                application_date TEXT NOT NULL,
                response_date TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS profile (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS email_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                gmail_message_id TEXT NOT NULL,
                sender TEXT,
                subject TEXT,
                received_date TEXT,
                snippet TEXT,
                body_text TEXT,
                ai_status TEXT,
                ai_confidence REAL,
                ai_reason TEXT,
                ai_classified_at TEXT,
                ai_applied INTEGER NOT NULL DEFAULT 0,
                ai_previous_status TEXT,
                ai_previous_response_date TEXT,
                reviewed INTEGER NOT NULL DEFAULT 0,
                dismissed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(job_id, gmail_message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_url_hash ON jobs(url_hash);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_application_date ON jobs(application_date);
            CREATE INDEX IF NOT EXISTS idx_email_matches_job_id ON email_matches(job_id);
            """
        )
        self.migrate()
        self.conn.commit()

    def migrate(self):
        """Add columns that later versions introduced.

        CREATE TABLE IF NOT EXISTS leaves an existing table alone, so a database
        made before message bodies were stored keeps the old column set until it
        is widened here. Adding a column is additive and keeps existing rows.
        """
        existing = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(email_matches)")
        }
        added = [
            ("snippet", "TEXT"),
            ("body_text", "TEXT"),
            ("ai_status", "TEXT"),
            ("ai_confidence", "REAL"),
            ("ai_reason", "TEXT"),
            ("ai_classified_at", "TEXT"),
            ("ai_applied", "INTEGER NOT NULL DEFAULT 0"),
            ("ai_previous_status", "TEXT"),
            ("ai_previous_response_date", "TEXT"),
        ]
        for column, declaration in added:
            if column not in existing:
                self.conn.execute(
                    f"ALTER TABLE email_matches ADD COLUMN {column} {declaration}"
                )

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

    def create_job(self, data):
        now = datetime.now().isoformat(timespec="seconds")
        job_id = self.next_job_id(data["posting_url"])
        self.conn.execute(
            """
            INSERT INTO jobs (
                job_id, url_hash, posting_url, position_title, company, job_type,
                requires_oa, completed_oa, received_references, payment_amount,
                payment_period, status, application_date, response_date, notes,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                url_hash(data["posting_url"]),
                normalize_url(data["posting_url"]),
                data["position_title"],
                data["company"],
                data["job_type"],
                int(data["requires_oa"]),
                int(data["completed_oa"]),
                int(data["received_references"]),
                data["payment_amount"],
                data["payment_period"],
                data["status"],
                data["application_date"],
                data["response_date"],
                data["notes"],
                now,
                now,
            ),
        )
        self.conn.commit()
        return job_id

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
