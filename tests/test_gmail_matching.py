"""Tests for Gmail query building, company matching, and email_matches storage.

No network access and no Google credentials are needed. The matching helpers are
pure functions, and the store tests run against a temporary SQLite database so
the real job_applications.sqlite3 is never touched.
"""

import base64
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta

import app
import clients.gmail_client as gmail_client
from clients.gmail_client import GMAIL_AVAILABLE as HAVE_GMAIL


@unittest.skipUnless(HAVE_GMAIL, "google/keyring packages not installed")
class CompanySlugTests(unittest.TestCase):
    def test_strips_punctuation_and_case(self):
        self.assertEqual(gmail_client.company_slug("Acme Corp."), "acme")

    def test_strips_common_suffixes(self):
        self.assertEqual(gmail_client.company_slug("Globex Inc"), "globex")
        self.assertEqual(gmail_client.company_slug("Initech LLC"), "initech")

    def test_joins_multiword_names(self):
        self.assertEqual(gmail_client.company_slug("Stark Industries"), "starkindustries")

    def test_empty_input(self):
        self.assertEqual(gmail_client.company_slug(""), "")
        self.assertEqual(gmail_client.company_slug(None), "")


@unittest.skipUnless(HAVE_GMAIL, "google/keyring packages not installed")
class BuildQueryTests(unittest.TestCase):
    def test_includes_company_and_date_window(self):
        query = gmail_client.build_query("Acme Corp", "2026-07-20")
        self.assertIn("from:acme", query)
        self.assertIn("subject:acme", query)
        # Window starts a day early so same-day replies are not dropped.
        self.assertIn("after:2026/07/19", query)

    def test_blank_company_yields_no_query(self):
        self.assertEqual(gmail_client.build_query("", "2026-07-20"), "")


@unittest.skipUnless(HAVE_GMAIL, "google/keyring packages not installed")
class SenderDomainTests(unittest.TestCase):
    def test_extracts_domain_from_display_name_form(self):
        self.assertEqual(
            gmail_client.sender_domain("Acme Careers <no-reply@acme.com>"), "acme.com"
        )

    def test_handles_bare_address(self):
        self.assertEqual(gmail_client.sender_domain("hr@globex.co.uk"), "globex.co.uk")

    def test_returns_empty_for_garbage(self):
        self.assertEqual(gmail_client.sender_domain("not an address"), "")


@unittest.skipUnless(HAVE_GMAIL, "google/keyring packages not installed")
class MessageMatchesCompanyTests(unittest.TestCase):
    def message(self, sender="", subject=""):
        return {"sender": sender, "subject": subject, "date": "", "id": "x"}

    def test_matches_on_sender_domain(self):
        msg = self.message(sender="Careers <careers@acme.com>", subject="Your application")
        self.assertTrue(gmail_client.message_matches_company(msg, "Acme Corp"))

    def test_matches_on_subject(self):
        msg = self.message(sender="noreply@greenhouse.io", subject="Update from Acme")
        self.assertTrue(gmail_client.message_matches_company(msg, "Acme"))

    def test_generic_domain_alone_does_not_match(self):
        # A recruiter mailing from gmail.com must not match every short company slug.
        msg = self.message(sender="recruiter@gmail.com", subject="Following up")
        self.assertFalse(gmail_client.message_matches_company(msg, "Acme"))

    def test_unrelated_message_does_not_match(self):
        msg = self.message(sender="news@othersite.com", subject="Weekly digest")
        self.assertFalse(gmail_client.message_matches_company(msg, "Acme"))

    def test_blank_company_never_matches(self):
        msg = self.message(sender="careers@acme.com", subject="Acme")
        self.assertFalse(gmail_client.message_matches_company(msg, ""))

    def test_body_text_is_not_consulted(self):
        # Matches store body text, but the decision stays on headers alone: a
        # company name appearing in the body must not produce a match.
        msg = self.message(sender="news@unrelated.com", subject="Newsletter")
        msg["body"] = "Acme Acme Acme"
        self.assertFalse(gmail_client.message_matches_company(msg, "Acme"))


def encode_part(text):
    """Base64url without padding, the way the Gmail API returns part data."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


@unittest.skipUnless(HAVE_GMAIL, "google/keyring packages not installed")
class ExtractBodyTests(unittest.TestCase):
    def test_single_plain_text_part(self):
        payload = {"mimeType": "text/plain", "body": {"data": encode_part("Hello there")}}
        self.assertEqual(gmail_client.extract_body(payload), "Hello there")

    def test_prefers_plain_text_over_html(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": encode_part("plain version")}},
                {"mimeType": "text/html", "body": {"data": encode_part("<p>html version</p>")}},
            ],
        }
        self.assertEqual(gmail_client.extract_body(payload), "plain version")

    def test_falls_back_to_html_with_tags_stripped(self):
        markup = "<html><body><p>Thanks for applying &amp; good luck</p></body></html>"
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [{"mimeType": "text/html", "body": {"data": encode_part(markup)}}],
        }
        body = gmail_client.extract_body(payload)
        self.assertIn("Thanks for applying & good luck", body)
        self.assertNotIn("<p>", body)

    def test_html_script_and_style_are_dropped(self):
        markup = "<style>p{color:red}</style><script>alert(1)</script><p>Real text</p>"
        payload = {"mimeType": "text/html", "body": {"data": encode_part(markup)}}
        body = gmail_client.extract_body(payload)
        self.assertEqual(body, "Real text")

    def test_walks_nested_multipart(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": encode_part("buried text")}}
                    ],
                }
            ],
        }
        self.assertEqual(gmail_client.extract_body(payload), "buried text")

    def test_attachments_contribute_nothing(self):
        # An attachment part carries an attachmentId instead of inline data, so
        # nothing is fetched or decoded for it.
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": encode_part("see attached")}},
                {"mimeType": "application/pdf", "body": {"attachmentId": "abc", "size": 9001}},
            ],
        }
        self.assertEqual(gmail_client.extract_body(payload), "see attached")

    def test_missing_or_unreadable_data_yields_empty_string(self):
        self.assertEqual(gmail_client.extract_body({}), "")
        self.assertEqual(gmail_client.extract_body({"mimeType": "text/plain", "body": {}}), "")
        self.assertEqual(
            gmail_client.extract_body(
                {"mimeType": "text/plain", "body": {"data": "!!!not base64!!!"}}
            ),
            "",
        )

    def test_long_body_is_truncated(self):
        payload = {"mimeType": "text/plain", "body": {"data": encode_part("x" * 500)}}
        body = gmail_client.extract_body(payload, max_chars=100)
        self.assertTrue(body.startswith("x" * 100))
        self.assertIn("truncated", body)


class EmailMatchStoreTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.store = app.JobStore(self.db_path)

    def tearDown(self):
        self.store.conn.close()
        os.unlink(self.db_path)

    def add_job(self, company="Acme", status="Applied", days_ago=1):
        applied = (date.today() - timedelta(days=days_ago)).isoformat()
        return self.store.create_job(
            {
                "posting_url": f"https://{company.lower()}.com/jobs/1",
                "position_title": "Engineer",
                "company": company,
                "job_type": "Internship",
                "requires_oa": False,
                "completed_oa": False,
                "received_references": False,
                "payment_amount": "",
                "payment_period": "Unspecified",
                "status": status,
                "application_date": applied,
                "response_date": None,
                "notes": "",
            }
        )

    def sample_message(self, message_id="msg-1"):
        return {
            "id": message_id,
            "sender": "Careers <careers@acme.com>",
            "subject": "Your application to Acme",
            "date": "Tue, 28 Jul 2026 10:00:00 -0400",
        }

    def test_jobs_awaiting_response_excludes_closed_states(self):
        self.add_job(company="Acme", status="Applied")
        self.add_job(company="Globex", status="Rejected")
        awaiting = self.store.jobs_awaiting_response()
        companies = {row["company"] for row in awaiting}
        self.assertEqual(companies, {"Acme"})

    def test_record_email_match_is_idempotent(self):
        job_id = self.add_job()
        self.assertTrue(self.store.record_email_match(job_id, self.sample_message()))
        # Re-scanning the same inbox must not create duplicates.
        self.assertFalse(self.store.record_email_match(job_id, self.sample_message()))
        self.assertEqual(len(self.store.pending_email_matches()), 1)

    def test_known_message_ids_round_trips(self):
        job_id = self.add_job()
        self.store.record_email_match(job_id, self.sample_message("abc"))
        self.assertEqual(self.store.known_message_ids(job_id), {"abc"})

    def test_scan_does_not_change_job_status(self):
        # Recording a match is a suggestion only; the job must be untouched.
        job_id = self.add_job(status="Applied")
        self.store.record_email_match(job_id, self.sample_message())
        job = self.store.conn.execute(
            "SELECT status, response_date FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        self.assertEqual(job["status"], "Applied")
        self.assertIn(job["response_date"], (None, ""))

    def test_confirm_applies_status_and_clears_from_pending(self):
        job_id = self.add_job(status="Applied")
        self.store.record_email_match(job_id, self.sample_message())
        match_id = self.store.pending_email_matches()[0]["id"]
        self.store.confirm_email_match(match_id, "Interview")
        job = self.store.conn.execute(
            "SELECT status, response_date FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        self.assertEqual(job["status"], "Interview")
        self.assertTrue(job["response_date"])
        self.assertEqual(self.store.pending_email_matches(), [])

    def test_dismiss_clears_without_touching_job(self):
        job_id = self.add_job(status="Applied")
        self.store.record_email_match(job_id, self.sample_message())
        match_id = self.store.pending_email_matches()[0]["id"]
        self.store.dismiss_email_match(match_id)
        job = self.store.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        self.assertEqual(job["status"], "Applied")
        self.assertEqual(self.store.pending_email_matches(), [])

    def test_body_and_snippet_round_trip(self):
        job_id = self.add_job()
        message = self.sample_message()
        message["body"] = "Hi, we would like to schedule a call.\n\nBest,\nRecruiting"
        message["snippet"] = "Hi, we would like to schedule a call."
        self.store.record_email_match(job_id, message)
        match = self.store.pending_email_matches()[0]
        self.assertEqual(match["body_text"], message["body"])
        self.assertEqual(match["snippet"], message["snippet"])

    def test_match_without_body_still_records(self):
        # A failed body fetch must not cost us the match.
        job_id = self.add_job()
        self.assertTrue(self.store.record_email_match(job_id, self.sample_message()))
        match = self.store.pending_email_matches()[0]
        self.assertEqual(match["body_text"], "")
        self.assertEqual(match["snippet"], "")

    def test_email_matches_table_is_additive(self):
        # Opening an existing database must not disturb the jobs table.
        job_id = self.add_job()
        reopened = app.JobStore(self.db_path)
        self.assertEqual(len(reopened.list_jobs()), 1)
        self.assertEqual(reopened.list_jobs()[0]["job_id"], job_id)
        reopened.conn.close()


class SchemaMigrationTests(unittest.TestCase):
    """Opening a pre-body database must widen it without losing rows."""

    OLD_SCHEMA = """
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

    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(self.OLD_SCHEMA)
        conn.execute(
            """
            INSERT INTO email_matches (
                job_id, gmail_message_id, sender, subject, received_date, created_at
            )
            VALUES ('JOB1', 'msg-old', 'careers@acme.com', 'Old match', '', '2026-07-01')
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_missing_columns_are_added(self):
        store = app.JobStore(self.db_path)
        columns = {
            row["name"] for row in store.conn.execute("PRAGMA table_info(email_matches)")
        }
        self.assertIn("body_text", columns)
        self.assertIn("snippet", columns)
        store.conn.close()

    def test_existing_rows_survive_with_empty_body(self):
        store = app.JobStore(self.db_path)
        row = store.conn.execute(
            "SELECT subject, body_text FROM email_matches WHERE gmail_message_id = 'msg-old'"
        ).fetchone()
        self.assertEqual(row["subject"], "Old match")
        self.assertIsNone(row["body_text"])
        store.conn.close()

    def test_migration_is_idempotent(self):
        app.JobStore(self.db_path).conn.close()
        # A second open must not fail on already-present columns.
        store = app.JobStore(self.db_path)
        self.assertEqual(
            store.conn.execute("SELECT COUNT(*) AS n FROM email_matches").fetchone()["n"], 1
        )
        store.conn.close()


if __name__ == "__main__":
    unittest.main()
