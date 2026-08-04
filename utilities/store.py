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
    """
    Summary:
        Reduce a posting URL to a canonical form so the same posting hashes
        identically regardless of how it was written.

    Parameters:
        url (str): Raw posting URL, with or without a scheme. Surrounding
            whitespace is tolerated; a missing scheme is assumed to be https.

    Returns:
        str: The normalized URL - lowercased scheme and hostname, port kept
            when present, trailing slash stripped from the path, query string
            preserved, and params/fragment discarded.

    Raises:
        AttributeError: If `url` is None, raised by the leading `.strip()`.
        ValueError: If the value cannot be parsed as a URL, for example a
            malformed IPv6 literal.
    """
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
    """creates a hash of the url to use as a unique identifier for the job posting

    Summary:
        Derive the legacy URL-based Job ID for a posting.

    Parameters:
        url (str): Raw posting URL. Normalized before hashing, so cosmetic
            differences produce the same hash.

    Returns:
        str: The first 12 hex characters of the SHA-256 digest, uppercased.
            Stable across runs, which is what makes it usable as a Job ID.

    Raises:
        AttributeError: If `url` is None, propagated from `normalize_url`.
        ValueError: If `url` cannot be parsed, propagated from `normalize_url`.
    """
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:12].upper()


def today_iso():
    """
    Summary:
        Return today's local date as an ISO-8601 string.

    Returns:
        str: Today's date formatted as ``YYYY-MM-DD``, the format every date
            column in this schema stores.
    """
    return date.today().isoformat()


class JobStore:
    def __init__(self, db_path=None):
        """
        Summary:
            Open the SQLite database, apply connection pragmas, and create or
            upgrade the schema.

        Parameters:
            db_path (str | None): Path to the database file. When None, the
                module-level `DB_PATH` is read at call time so tests that
                reassign it are honoured.

        Raises:
            sqlite3.Error: If the database cannot be opened.
            RuntimeError: Propagated from `init_db` if a migration fails.
        """
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

        Summary:
            Apply the WAL, busy-timeout, and synchronous pragmas to the open
            connection.

        Raises:
            sqlite3.Error: If a pragma cannot be applied.
        """
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA synchronous=NORMAL")

    def init_db(self):
        """Create or upgrade the schema.

        Delegates to `utilities/migrations.py`, which stamps a fresh database
        at the current version and runs the versioned upgrades on an existing
        one. Backups before a structural change happen there.

        Summary:
            Stamp a new database or run pending migrations on an existing one.

        Returns:
            int: The schema version the database is at after initialisation.

        Raises:
            RuntimeError: If a migration fails; the pre-migration backup path
                is reported in the message.
            sqlite3.Error: If the schema statements cannot be executed.
        """
        return initialise(self.conn, self.db_path)

    def close(self):
        """Release the connection. Long-running processes never call this;
        tests do, to keep ResourceWarnings out of the output.

        Summary:
            Close the SQLite connection, ignoring any failure to do so.

        Note:
            Any exception from `sqlite3.Connection.close` is swallowed. A
            failed close during teardown is not worth surfacing.
        """
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self):
        """
        Summary:
            Enter a `with` block and hand back this store.

        Returns:
            JobStore: This instance, so `with JobStore(path) as store` binds
                the store itself.
        """
        return self

    def __exit__(self, *_exc):
        """
        Summary:
            Leave a `with` block, closing the connection.

        Parameters:
            *_exc: The (type, value, traceback) triple supplied by the runtime.
                Ignored - the connection is closed whether or not the block
                raised.

        Returns:
            bool: Always False, so an exception raised inside the block
                propagates rather than being suppressed.
        """
        self.close()
        return False

    # Job records -----------------------------------------------------------

    def duplicate_jobs(self, posting_url):
        """
        Summary:
            List jobs already recorded against the same posting URL.

        Parameters:
            posting_url (str): The URL to check. Normalized and hashed before
                the lookup, so cosmetic differences still match.

        Returns:
            list[sqlite3.Row]: Matching job rows, newest first. Empty when the
                URL has not been logged before.

        Raises:
            AttributeError: If `posting_url` is None, propagated from
                `url_hash`.
            sqlite3.Error: If the query fails.
        """
        h = url_hash(posting_url)
        return self.conn.execute(
            "SELECT * FROM jobs WHERE url_hash = ? ORDER BY created_at DESC", (h,)
        ).fetchall()

    def next_job_id(self, posting_url):
        """
        Summary:
            Allocate the Job ID for a new application at a posting URL.

        Parameters:
            posting_url (str): The URL the application was made through.

        Returns:
            str: The bare 12-character URL hash for the first application to
                that URL, or that hash with a `-N` correlation suffix when the
                URL has been used before. The suffix keeps genuinely distinct
                postings that share a URL separable.

        Raises:
            AttributeError: If `posting_url` is None, propagated from
                `url_hash`.
            sqlite3.Error: If the query fails.
        """
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

        Summary:
            Allocate a Job ID from an identity key when there is no URL to hash.

        Parameters:
            key (str): The identity key derived from title, company, and
                location. See `utilities/identity.py`.

        Returns:
            str: `key` itself when unused, otherwise `key` with a `-N`
                correlation suffix.

        Raises:
            sqlite3.Error: If the query fails.
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

        Summary:
            Insert a job application and return the Job ID it was filed under.

        Parameters:
            data (dict): The application fields. Required keys are
                `position_title`, `job_type`, `status`, and `application_date`;
                a missing one raises KeyError rather than storing a partial
                row. Optional keys are `posting_url`, `location`, `company`,
                `requires_oa`, `completed_oa`, `received_references`,
                `payment_amount`, `payment_period`, `response_date`, `notes`,
                `board`, and `board_job_id`.

        Returns:
            str: The Job ID assigned to the new row - URL-derived when a
                posting URL was supplied, identity-derived otherwise.

        Raises:
            KeyError: If a required key is absent from `data`.
            sqlite3.Error: If the insert or the commit fails.
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
        """
        Summary:
            Fetch the earliest job filed under an exact identity key.

        Parameters:
            key (str): The identity key to look up. Matched exactly, with no
                fallback - use `find_job` for the fallback chain.

        Returns:
            sqlite3.Row | None: The lowest-`id` matching row, or None when the
                key is unknown. Lowest-`id` is deliberate: when duplicates
                exist, the original row wins.

        Raises:
            sqlite3.Error: If the query fails.
        """
        return self.conn.execute(
            "SELECT * FROM jobs WHERE identity_key = ? ORDER BY id LIMIT 1", (key,)
        ).fetchone()

    def find_job(self, title, company, location=None):
        """Resolve a job from a role description.

        Tries the location-qualified key first, then the bare title+company
        key, so rows written before locations existed are still reachable. See
        `identity.candidate_keys`.

        Summary:
            Find a job from a role description, trying progressively looser
            identity keys.

        Parameters:
            title (str): The position title.
            company (str | None): The employer name, if known.
            location (str | None): The location, if known. Supplying it yields
                a more specific key that is tried first.

        Returns:
            sqlite3.Row | None: The first matching job row, or None when no
                candidate key matches.

        Raises:
            sqlite3.Error: If a lookup query fails.
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

        Summary:
            List identity keys that more than one job row claims.

        Returns:
            list[sqlite3.Row]: One row per collision, each carrying
                `identity_key`, `count`, and a comma-separated `job_ids`.
                Ordered by `count` descending. Empty when there are no
                collisions.

        Raises:
            sqlite3.Error: If the query fails.
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

        Summary:
            Record or enrich one board's URL for a job.

        Parameters:
            job_id (str): The Job ID the source belongs to.
            url (str): The posting URL as seen on that board. Together with
                `job_id` this forms the conflict key.
            board (str | None): Board name, for example "linkedin". Left
                unchanged when None and a value is already stored.
            board_job_id (str | None): The board's own ID for the posting.
                Same NULL-preserving behaviour as `board`.

        Raises:
            sqlite3.Error: If the upsert or the commit fails.
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
        """
        Summary:
            List every board URL recorded for a job.

        Parameters:
            job_id (str): The Job ID to list sources for.

        Returns:
            list[sqlite3.Row]: Source rows in the order they were first seen.
                Empty for a job created without a posting URL.

        Raises:
            sqlite3.Error: If the query fails.
        """
        return self.conn.execute(
            "SELECT * FROM job_sources WHERE job_id = ? ORDER BY first_seen", (job_id,)
        ).fetchall()

    def job_by_board_reference(self, board, board_job_id):
        """Strongest resolution signal: the board's own ID for the posting.

        Summary:
            Resolve a job from a board name and that board's posting ID.

        Parameters:
            board (str | None): Board name. A falsy value short-circuits to
                None rather than running a query that cannot match.
            board_job_id (str | None): The board's ID for the posting. Same
                short-circuit.

        Returns:
            sqlite3.Row | None: The joined job row, or None when either
                argument is falsy or no source matches.

        Raises:
            sqlite3.Error: If the query fails.
        """
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
        """
        Summary:
            Set a job's status, stamping a response date the first time it
            reaches a terminal state.

        Parameters:
            row_id (int): The `jobs.id` primary key - not the `job_id` string.
            status (str): The new status. "Interview", "Offer", and "Rejected"
                stamp today's date into `response_date`.

        Raises:
            sqlite3.Error: If the update or the commit fails.

        Note:
            `response_date` is written with COALESCE, so an existing date is
            never overwritten - the first response is the one that counts.
            Setting a terminal status also drops the job out of
            `jobs_awaiting_response`, and therefore out of future Gmail scans.
        """
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
        """
        Summary:
            Return every job, newest application first.

        Returns:
            list[sqlite3.Row]: All job rows ordered by `application_date`
                descending, then `created_at` descending to break ties within
                a day.

        Raises:
            sqlite3.Error: If the query fails.
        """
        return self.conn.execute(
            "SELECT * FROM jobs ORDER BY application_date DESC, created_at DESC"
        ).fetchall()

    # Dashboard aggregates --------------------------------------------------

    def stats(self):
        """
        Summary:
            Compute the four headline counts shown on the dashboard.

        Returns:
            dict[str, int]: Keys `total`, `heard_back` (status in OA Received,
                Interview, Offer, or Rejected), `offers`, and `pending`
                (status Pending or Applied). Counted in Python rather than SQL
                because the buckets overlap the status list unevenly.

        Raises:
            sqlite3.Error: Propagated from `list_jobs`.
        """
        rows = self.list_jobs()
        total = len(rows)
        heard_back = sum(1 for r in rows if r["status"] in {"OA Received", "Interview", "Offer", "Rejected"})
        offers = sum(1 for r in rows if r["status"] == "Offer")
        pending = sum(1 for r in rows if r["status"] in {"Pending", "Applied"})
        return {"total": total, "heard_back": heard_back, "offers": offers, "pending": pending}

    def daily_counts(self, days=14):
        """
        Summary:
            Count applications per calendar day over a trailing window.

        Parameters:
            days (int | None): Window length in days, ending today. Pass None
                for "all time", which widens the window to start at the
                earliest `application_date` on record.

        Returns:
            list[tuple[str, int]]: One `(iso_date, count)` pair per day in the
                window, in chronological order. Days with no applications are
                present with a count of 0, so the chart has no gaps.

        Raises:
            sqlite3.Error: If a query fails.
            ValueError: If a stored `application_date` is not valid ISO-8601,
                raised by `date.fromisoformat` in the all-time branch.
        """
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
        """
        Summary:
            Turn the daily counts into a running total for the trend line.

        Parameters:
            days (int | None): Window length in days, ending today. None means
                all time. Passed through to `daily_counts`.

        Returns:
            list[tuple[str, int]]: One `(iso_date, running_total)` pair per day.
                The total is seeded with the number of applications made
                before the window opened, so a 14-day view does not restart
                the line at zero.

        Raises:
            sqlite3.Error: Propagated from `daily_counts` or the seed query.
            ValueError: Propagated from `daily_counts`.
        """
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
        """
        Summary:
            Count jobs per status bucket for the pie chart.

        Returns:
            dict[str, int]: Counts keyed by the six display buckets - Pending,
                OA Received, Interview, Offer, Rejected, Withdrawn. Every key
                is present even at zero, so the chart legend stays stable.
                "Applied" is folded into "Pending"; an unrecognised status is
                dropped rather than creating a new bucket.

        Raises:
            sqlite3.Error: Propagated from `list_jobs`.
        """
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
        """
        Summary:
            List the jobs a Gmail scan should still look for replies about.

        Returns:
            list[sqlite3.Row]: Jobs whose status is Pending, Applied, or OA
                Received and which have no `response_date`, newest application
                first.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            This is the pool future scans check. Anything that stamps a
            `response_date` - including an auto-applied AI status - removes the
            job from it, which is why those writes are made reversible.
        """
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

        Summary:
            Store a suggested job/message match, ignoring one already recorded.

        Parameters:
            job_id (str): The Job ID the message appears to concern.
            message (dict): The message. Required keys are `id` (the Gmail
                message ID), `sender`, `subject`, and `date`; `snippet` and
                `body` are optional and default to empty strings.

        Returns:
            bool: True when a new row was inserted, False when this message was
                already matched to this job - which is what makes re-scanning
                cheap and safe.

        Raises:
            KeyError: If a required key is absent from `message`.
            sqlite3.Error: If the insert or the commit fails.
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
        """
        Summary:
            List matches still awaiting the user's confirm-or-dismiss decision.

        Returns:
            list[sqlite3.Row]: Unreviewed, undismissed matches joined to their
                job, newest first. Each row carries the match columns plus
                `position_title`, `company`, and `job_status`.

        Raises:
            sqlite3.Error: If the query fails.
        """
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
        """
        Summary:
            Return the Gmail message IDs already matched to a job.

        Parameters:
            job_id (str): The Job ID to look up.

        Returns:
            set[str]: The message IDs recorded against that job, including
                ones already reviewed or dismissed. A set so a scanner can
                skip seen messages with a membership test.

        Raises:
            sqlite3.Error: If the query fails.
        """
        rows = self.conn.execute(
            "SELECT gmail_message_id FROM email_matches WHERE job_id = ?", (job_id,)
        ).fetchall()
        return {row["gmail_message_id"] for row in rows}

    def confirm_email_match(self, match_id, status):
        """Apply a user-confirmed match to the job, then mark the match reviewed.

        The status write happens here rather than during scanning so nothing
        changes without the user explicitly choosing it.

        Summary:
            Apply a user-confirmed match to its job and mark the match
            reviewed.

        Parameters:
            match_id (int): The `email_matches.id` of the match being
                confirmed.
            status (str): The status to write onto the job.

        Raises:
            sqlite3.Error: If an update or the commit fails.

        Note:
            An unknown `match_id` returns early without raising, so a
            double-click on Confirm is harmless. The match is marked reviewed
            even when its job row has since been deleted, so a match cannot
            become permanently stuck in the queue.
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
        """
        Summary:
            Mark a match dismissed so it leaves the review queue.

        Parameters:
            match_id (int): The `email_matches.id` to dismiss.

        Raises:
            sqlite3.Error: If the update or the commit fails.

        Note:
            An unknown `match_id` updates nothing and does not raise.
            Dismissing changes no job status. It only says "this message was
            not about that application".
        """
        self.conn.execute(
            "UPDATE email_matches SET dismissed = 1 WHERE id = ?", (match_id,)
        )
        self.conn.commit()

    # AI classification -----------------------------------------------------

    def unclassified_email_matches(self, limit=None):
        """Pending matches that have body text but no classification yet.

        Rows already classified are skipped, so a cycle stopped by a rate limit
        resumes where it left off instead of spending quota on repeat work.

        Summary:
            List pending matches that have body text and no classification yet.

        Parameters:
            limit (int | None): Maximum rows to return. None means no limit,
                which is what a full cycle uses.

        Returns:
            list[sqlite3.Row]: Unreviewed, undismissed, unclassified matches
                whose `body_text` is non-empty, joined to their job and
                ordered newest first.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            Matches with no body are excluded rather than skipped later -
            there is nothing for the model to read, so sending them would
            spend quota for a guaranteed non-answer.
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
        """Store what the model inferred. Applies nothing to the job.

        Summary:
            Record a classification result against a match.

        Parameters:
            match_id (int): The `email_matches.id` that was classified.
            label (str): The inferred label, for example "Rejected" or
                "Interview".
            confidence (float): Model confidence in the range 0.0 to 1.0. The
                caller compares this against the auto-apply threshold; this
                method does not.
            reason (str): The model's short justification, shown in the UI.

        Raises:
            sqlite3.Error: If the update or the commit fails.

        Note:
            Stamping `ai_classified_at` is what removes the row from
            `unclassified_email_matches`, so a rate-limited cycle resumes
            correctly.
        """
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

        Summary:
            Apply a confident classification to a job, capturing the state it
            replaced so it can be undone.

        Parameters:
            match_id (int): The `email_matches.id` whose label is being
                applied.
            status (str): The status to write onto the job.

        Returns:
            bool: True when the status was applied. False when the match or
                its job no longer exists, in which case nothing is written.

        Raises:
            sqlite3.Error: If an update or the commit fails.

        Note:
            The previous status and response date are saved onto the match row
            before `update_status` runs. Reversing that order would lose them.
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
        """Put the job back exactly as it stood before the AI touched it.

        Summary:
            Reverse an auto-applied status, restoring the job's previous
            status and response date.

        Parameters:
            match_id (int): The `email_matches.id` whose applied status is
                being reverted.

        Returns:
            bool: True when the job was restored. False when the match is
                unknown or was never auto-applied, so undoing twice is safe.

        Raises:
            sqlite3.Error: If an update or the commit fails.

        Note:
            The classification itself is deliberately kept. Undo means the
            label was wrong, so clearing it would only let the next cycle
            spend quota reaching the same wrong conclusion. Clearing
            `response_date` also returns the job to
            `jobs_awaiting_response`.
        """
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
        """
        Summary:
            Write a key/value setting to the profile table, overwriting any
            existing value for that key.

        Parameters:
            key (str): The setting name, for example "theme" or
                "profile_text". Primary key of the table.
            value (str): The value to store.

        Raises:
            sqlite3.Error: If the upsert or the commit fails.
        """
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
        """
        Summary:
            Read a profile setting, falling back to a default when unset.

        Parameters:
            key (str): The setting name to read.
            default (str): Returned when the key has never been written.
                Defaults to the empty string.

        Returns:
            str: The stored value, or `default` when the key is absent.

        Raises:
            sqlite3.Error: If the query fails.
        """
        row = self.conn.execute("SELECT value FROM profile WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
