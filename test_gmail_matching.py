"""Tests for Gmail query building, company matching, and email_matches storage.

No network access and no Google credentials are needed. The matching helpers are
pure functions, and the store tests run against a temporary SQLite database so
the real job_applications.sqlite3 is never touched.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta

import app

try:
    import gmail_client

    HAVE_GMAIL = True
except ImportError:
    HAVE_GMAIL = False


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
        # Only headers are ever fetched, so a company name appearing elsewhere
        # must not produce a match.
        msg = self.message(sender="news@unrelated.com", subject="Newsletter")
        msg["body"] = "Acme Acme Acme"
        self.assertFalse(gmail_client.message_matches_company(msg, "Acme"))


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

    def test_email_matches_table_is_additive(self):
        # Opening an existing database must not disturb the jobs table.
        job_id = self.add_job()
        reopened = app.JobStore(self.db_path)
        self.assertEqual(len(reopened.list_jobs()), 1)
        self.assertEqual(reopened.list_jobs()[0]["job_id"], job_id)
        reopened.conn.close()


if __name__ == "__main__":
    unittest.main()
