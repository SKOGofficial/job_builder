"""Provider routing and usage persistence.

The usage ledger is what makes a per-day ceiling real. An in-memory counter
resets on exactly the restart a runaway loop causes, so these tests care most
about what survives reopening the database and what the rolling window counts.
"""

import unittest
from datetime import datetime, timedelta

from utilities.mailstore import MailStore
from utilities.store import JobStore


def iso(offset_hours=0):
    """Timestamp `offset_hours` in the past, in the format `_now` writes."""
    return (datetime.now() - timedelta(hours=offset_hours)).isoformat(
        timespec="seconds"
    )


class ProviderStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = JobStore(":memory:")
        self.mail = MailStore(self.store.conn)
        self.addCleanup(self.store.conn.close)

    def usage(self, provider, outcome="ok", hours_ago=0, **extra):
        row = {"provider": provider, "task": "route_email", "outcome": outcome,
               "at": iso(hours_ago)}
        row.update(extra)
        return row

    # --- routing ---------------------------------------------------------

    def test_no_route_means_no_opinion(self):
        """Absent must stay distinguishable from "chose the default"."""
        self.assertEqual(self.mail.provider_routes(), {})

    def test_route_round_trips(self):
        self.mail.set_provider_route("research", "gemini", "anthropic")
        self.assertEqual(
            self.mail.provider_routes(), {"research": ("gemini", "anthropic")}
        )

    def test_route_is_replaced_not_duplicated(self):
        self.mail.set_provider_route("route_email", "groq", "gemini")
        self.mail.set_provider_route("route_email", "gemini", None)
        self.assertEqual(
            self.mail.provider_routes(), {"route_email": ("gemini", None)}
        )

    def test_a_task_can_be_turned_off(self):
        self.mail.set_provider_route("score_relevance", None, None)
        self.assertEqual(
            self.mail.provider_routes(), {"score_relevance": (None, None)}
        )

    def test_clearing_removes_the_row_rather_than_pinning_a_default(self):
        self.mail.set_provider_route("research", "gemini", "anthropic")
        self.assertTrue(self.mail.clear_provider_route("research"))
        self.assertEqual(self.mail.provider_routes(), {})

    def test_clearing_an_absent_route_reports_nothing_happened(self):
        self.assertFalse(self.mail.clear_provider_route("research"))

    def test_routes_survive_reopening(self):
        self.mail.set_provider_route("extract_alert", "gemini", "groq")
        reopened = MailStore(self.store.conn)
        self.assertEqual(
            reopened.provider_routes(), {"extract_alert": ("gemini", "groq")}
        )

    # --- usage -----------------------------------------------------------

    def test_empty_batch_writes_nothing(self):
        self.assertEqual(self.mail.record_provider_usage([]), 0)

    def test_requests_are_counted_per_provider(self):
        self.mail.record_provider_usage(
            [self.usage("gemini"), self.usage("gemini"), self.usage("groq")]
        )
        since = iso(24)
        self.assertEqual(self.mail.provider_requests_since("gemini", since), 2)
        self.assertEqual(self.mail.provider_requests_since("groq", since), 1)
        self.assertEqual(self.mail.provider_requests_since("anthropic", since), 0)

    def test_the_window_excludes_older_rows(self):
        self.mail.record_provider_usage(
            [self.usage("gemini", hours_ago=30), self.usage("gemini", hours_ago=1)]
        )
        self.assertEqual(self.mail.provider_requests_since("gemini", iso(24)), 1)

    def test_failures_count_against_the_budget_too(self):
        """A 429 is exactly the event a daily budget must remember."""
        self.mail.record_provider_usage(
            [self.usage("gemini"), self.usage("gemini", outcome="rate_limited")]
        )
        self.assertEqual(self.mail.provider_requests_since("gemini", iso(24)), 2)

    def test_a_daily_denial_survives_a_restart(self):
        """The point of persisting it: reopening must not un-exhaust the cap."""
        self.mail.record_provider_usage([self.usage("gemini", outcome="denied_day")])
        reopened = MailStore(self.store.conn)
        self.assertTrue(reopened.provider_denied_day_since("gemini", iso(24)))
        self.assertFalse(reopened.provider_denied_day_since("groq", iso(24)))

    def test_a_denial_outside_the_window_no_longer_binds(self):
        self.mail.record_provider_usage(
            [self.usage("gemini", outcome="denied_day", hours_ago=30)]
        )
        self.assertFalse(self.mail.provider_denied_day_since("gemini", iso(24)))

    def test_summary_reports_tokens_failures_and_last_model(self):
        self.mail.record_provider_usage([
            self.usage("gemini", model="gemini-2.0-flash", total_tokens=100,
                       hours_ago=2),
            self.usage("gemini", outcome="rate_limited", model="gemini-2.0-flash",
                       total_tokens=0, hours_ago=1),
            self.usage("groq", model="llama-3.3-70b-versatile", total_tokens=50),
        ])
        summary = self.mail.provider_usage_since(iso(24))
        self.assertEqual(summary["gemini"]["requests"], 2)
        self.assertEqual(summary["gemini"]["tokens"], 100)
        self.assertEqual(summary["gemini"]["failures"], 1)
        self.assertEqual(summary["gemini"]["model"], "gemini-2.0-flash")
        self.assertEqual(summary["groq"]["failures"], 0)

    def test_pruning_keeps_the_budgeting_window(self):
        self.mail.record_provider_usage([
            self.usage("gemini", hours_ago=24 * 40),
            self.usage("gemini", hours_ago=1),
        ])
        self.assertEqual(self.mail.prune_provider_usage(older_than_days=30), 1)
        self.assertEqual(self.mail.provider_requests_since("gemini", iso(24)), 1)


class AttributionTests(unittest.TestCase):
    """The model name reaching both classification tables."""

    def setUp(self):
        self.store = JobStore(":memory:")
        self.mail = MailStore(self.store.conn)
        self.addCleanup(self.store.conn.close)

    def add_match(self):
        """Create a job with one matched reply, and return the match id."""
        job_id = self.store.create_job({
            "posting_url": "https://acme.test/jobs/1",
            "position_title": "Engineer",
            "company": "Acme",
            "job_type": "Internship",
            "status": "Applied",
            "application_date": "2026-01-15",
            "notes": "",
        })
        self.store.record_email_match(job_id, {
            "id": "m1",
            "sender": "careers@acme.test",
            "subject": "Update",
            "date": "Tue, 20 Jan 2026 10:00:00 -0400",
            "snippet": "hi",
            "body": "We are moving forward.",
        })
        return self.store.conn.execute(
            "SELECT id FROM email_matches WHERE gmail_message_id = 'm1'"
        ).fetchone()["id"]

    def add_message(self):
        self.mail.upsert_message({
            "id": "m1",
            "thread_id": "t1",
            "sender": "alerts@board.test",
            "subject": "Jobs for you",
            "date": "Tue, 20 Jan 2026 10:00:00 -0400",
            "snippet": "hi",
        })

    def test_match_records_the_model(self):
        match_id = self.add_match()
        self.store.record_classification(match_id, "Interview", 0.9, "why", "gemini-2.0-flash")
        row = self.store.conn.execute(
            "SELECT ai_status, ai_model FROM email_matches WHERE id = ?", (match_id,)
        ).fetchone()
        self.assertEqual(row["ai_status"], "Interview")
        self.assertEqual(row["ai_model"], "gemini-2.0-flash")

    def test_match_model_is_optional(self):
        """Existing four-argument callers, and every test double, still work."""
        match_id = self.add_match()
        self.store.record_classification(match_id, "Rejected", 0.9, "why")
        row = self.store.conn.execute(
            "SELECT ai_status, ai_model FROM email_matches WHERE id = ?", (match_id,)
        ).fetchone()
        self.assertEqual(row["ai_status"], "Rejected")
        self.assertIsNone(row["ai_model"])

    def test_message_records_the_model(self):
        self.add_message()
        self.mail.record_category("m1", "job_alert", 0.9, "why", "gemini-2.0-flash")
        self.mail.commit()
        row = self.mail.message("m1")
        self.assertEqual(row["category"], "job_alert")
        self.assertEqual(row["category_model"], "gemini-2.0-flash")

    def test_message_model_is_optional(self):
        self.add_message()
        self.mail.record_category("m1", "job_alert", 0.9, "why")
        self.mail.commit()
        self.assertIsNone(self.mail.message("m1")["category_model"])


if __name__ == "__main__":
    unittest.main()
