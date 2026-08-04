"""Schema migrations.

The job here is narrow and important: prove that an existing database with real
rows in it comes out the other side intact. `AGENTS.md` makes preserving
`job_applications.sqlite3` a hard constraint, and a migration is the only code
in the project that can violate it silently.

Every test builds a v0 database from the pre-migration DDL - reproduced below
verbatim rather than imported, so that changing the live schema cannot quietly
change what "v0" means and make these tests pass for the wrong reason.
"""

import os
import sqlite3
import tempfile
import unittest

from utilities.migrations import (
    backfill_identity_keys,
    current_version,
    initialise,
    pending_migrations,
)
from utilities.schema import SCHEMA_VERSION, column_names

#: The schema exactly as `JobStore.init_db` created it before the identity
#: rework. Frozen on purpose - do not update it when schema.py changes.
V0_SQL = """
CREATE TABLE jobs (
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

CREATE TABLE profile (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE email_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    gmail_message_id TEXT NOT NULL,
    sender TEXT,
    subject TEXT,
    received_date TEXT,
    reviewed INTEGER NOT NULL DEFAULT 0,
    dismissed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, gmail_message_id)
);
"""


def make_v0(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(V0_SQL)
    conn.commit()
    return conn


def add_job(conn, job_id, title, company, **overrides):
    values = {
        "job_id": job_id,
        "url_hash": job_id,
        "posting_url": f"https://example.com/{job_id}",
        "position_title": title,
        "company": company,
        "job_type": "Full time",
        "status": "Applied",
        "application_date": "2026-01-15",
        "notes": "kept",
        "created_at": "2026-01-15T09:00:00",
        "updated_at": "2026-01-15T09:00:00",
    }
    values.update(overrides)
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({marks})", tuple(values.values()))
    conn.commit()


class TestFreshDatabase(unittest.TestCase):
    def test_fresh_is_stamped_current_and_skips_migrations(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        applied = initialise(conn)
        self.assertEqual(applied, [], "a fresh database should run no migrations")
        self.assertEqual(current_version(conn), SCHEMA_VERSION)

    def test_fresh_has_every_table(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        initialise(conn)
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for expected in ("jobs", "job_sources", "messages", "message_links",
                         "job_leads", "job_research", "job_artifacts",
                         "experiences", "sender_denylist", "profile",
                         "email_matches"):
            self.assertIn(expected, names)

    def test_initialise_is_idempotent(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        initialise(conn)
        self.assertEqual(initialise(conn), [])
        self.assertEqual(current_version(conn), SCHEMA_VERSION)


class TestV0Upgrade(unittest.TestCase):
    def test_v0_reports_pending_work(self):
        conn = make_v0()
        self.assertEqual(current_version(conn), 0)
        self.assertTrue(pending_migrations(conn))

    def test_rows_survive(self):
        conn = make_v0()
        add_job(conn, "AAA111", "Software Engineer", "Google")
        add_job(conn, "BBB222", "Data Analyst", "Acme Inc.")
        initialise(conn)

        rows = conn.execute("SELECT * FROM jobs ORDER BY job_id").fetchall()
        self.assertEqual([r["job_id"] for r in rows], ["AAA111", "BBB222"])
        self.assertEqual(rows[0]["position_title"], "Software Engineer")
        self.assertEqual(rows[0]["notes"], "kept")
        self.assertEqual(rows[0]["posting_url"], "https://example.com/AAA111")
        self.assertEqual(rows[0]["created_at"], "2026-01-15T09:00:00")

    def test_new_columns_appear(self):
        conn = make_v0()
        add_job(conn, "AAA111", "Software Engineer", "Google")
        initialise(conn)
        columns = column_names(conn, "jobs")
        for expected in ("location", "identity_key", "identity_scheme"):
            self.assertIn(expected, columns)

    def test_identity_key_is_backfilled(self):
        conn = make_v0()
        add_job(conn, "AAA111", "Software Engineer", "Google")
        initialise(conn)
        row = conn.execute("SELECT identity_key, identity_scheme FROM jobs").fetchone()
        self.assertTrue(row["identity_key"])
        self.assertEqual(len(row["identity_key"]), 12)
        # No location on legacy rows, so the title+company scheme applies.
        self.assertEqual(row["identity_scheme"], "tc")

    def test_posting_url_becomes_nullable(self):
        conn = make_v0()
        add_job(conn, "AAA111", "Software Engineer", "Google")
        initialise(conn)
        # A job created from an acknowledgement email has no URL at all. Under
        # v0 this raised IntegrityError.
        conn.execute(
            "INSERT INTO jobs (job_id, position_title, job_type, status, "
            "application_date, created_at, updated_at) "
            "VALUES ('CCC333', 'Designer', 'Full time', 'Applied', "
            "'2026-02-01', '2026-02-01T00:00:00', '2026-02-01T00:00:00')"
        )
        conn.commit()
        row = conn.execute("SELECT posting_url FROM jobs WHERE job_id='CCC333'").fetchone()
        self.assertIsNone(row["posting_url"])

    def test_version_is_stamped_after_upgrade(self):
        conn = make_v0()
        applied = initialise(conn)
        # Every migration runs, in order, and none is left pending. Asserted
        # against SCHEMA_VERSION rather than a literal list so adding a
        # migration does not require editing this test.
        self.assertEqual(applied, list(range(1, SCHEMA_VERSION + 1)))
        self.assertEqual(current_version(conn), SCHEMA_VERSION)
        self.assertEqual(pending_migrations(conn), [])

    def test_second_run_is_a_no_op(self):
        conn = make_v0()
        add_job(conn, "AAA111", "Software Engineer", "Google")
        initialise(conn)
        before = conn.execute("SELECT identity_key FROM jobs").fetchone()["identity_key"]
        self.assertEqual(initialise(conn), [])
        after = conn.execute("SELECT identity_key FROM jobs").fetchone()["identity_key"]
        self.assertEqual(before, after)

    def test_legacy_email_match_columns_are_added(self):
        conn = make_v0()
        initialise(conn)
        columns = column_names(conn, "email_matches")
        for expected in ("snippet", "body_text", "ai_status", "ai_applied",
                         "ai_previous_status"):
            self.assertIn(expected, columns)

    def test_existing_email_matches_survive(self):
        conn = make_v0()
        add_job(conn, "AAA111", "Software Engineer", "Google")
        conn.execute(
            "INSERT INTO email_matches (job_id, gmail_message_id, sender, subject, "
            "received_date, created_at) VALUES "
            "('AAA111', 'msg-1', 'a@google.com', 'Your application', "
            "'2026-01-20', '2026-01-20T10:00:00')"
        )
        conn.commit()
        initialise(conn)
        row = conn.execute("SELECT * FROM email_matches").fetchone()
        self.assertEqual(row["gmail_message_id"], "msg-1")
        self.assertEqual(row["job_id"], "AAA111")
        # job_id still resolves to a real job - the migration must not have
        # rewritten it, or every stored match would be orphaned.
        job = conn.execute(
            "SELECT 1 FROM jobs WHERE job_id = ?", (row["job_id"],)
        ).fetchone()
        self.assertIsNotNone(job)


class TestBackfillCollisions(unittest.TestCase):
    def test_duplicates_are_reported_not_merged(self):
        conn = make_v0()
        # Same role logged twice from two boards - different URLs, so v0 saw
        # them as distinct. They now collide on one identity key.
        add_job(conn, "AAA111", "Software Engineer", "Google")
        add_job(conn, "BBB222", "Sr. Software Engineer", "Google")
        add_job(conn, "CCC333", "Software Engineer", "Google LLC")
        initialise(conn)

        rows = conn.execute("SELECT job_id, identity_key FROM jobs ORDER BY job_id").fetchall()
        self.assertEqual(len(rows), 3, "no row may be deleted by a backfill")
        keys = {r["job_id"]: r["identity_key"] for r in rows}
        self.assertEqual(keys["AAA111"], keys["CCC333"], "the legal suffix should collapse")
        self.assertNotEqual(keys["AAA111"], keys["BBB222"], "seniority should not collapse")

    def test_collisions_are_returned_for_review(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        initialise(conn)
        for job_id, title in (("AAA111", "Software Engineer"),
                              ("BBB222", "Software Engineer")):
            conn.execute(
                "INSERT INTO jobs (job_id, position_title, company, job_type, status, "
                "application_date, created_at, updated_at) VALUES "
                f"('{job_id}', '{title}', 'Google', 'Full time', 'Applied', "
                "'2026-01-15', '2026-01-15T00:00:00', '2026-01-15T00:00:00')"
            )
        conn.commit()
        collisions = backfill_identity_keys(conn)
        self.assertEqual(len(collisions), 1)


class TestBackupOnDisk(unittest.TestCase):
    def test_structural_migration_writes_a_backup(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "job_applications.sqlite3")
        conn = make_v0(path)
        add_job(conn, "AAA111", "Software Engineer", "Google")
        conn.close()

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        initialise(conn, db_path=path)
        conn.close()

        backups = os.listdir(os.path.join(directory, "backups"))
        self.assertEqual(len(backups), 1, "a structural migration must leave a backup")

        # The backup must be a readable database still holding the old rows.
        restored = sqlite3.connect(os.path.join(directory, "backups", backups[0]))
        restored.row_factory = sqlite3.Row
        row = restored.execute("SELECT position_title FROM jobs").fetchone()
        self.assertEqual(row["position_title"], "Software Engineer")
        restored.close()


if __name__ == "__main__":
    unittest.main()
