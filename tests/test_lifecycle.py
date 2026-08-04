"""End-to-end row movement between the two lists.

The test that justifies the central design decision. `message_links` points at
an `identity_key` rather than at a `jobs.id` or a `job_leads.id`, and the claim
is that a lead promoted into an application keeps every email already attached
to it, with no migration and no re-linking pass.

`test_alert_email_survives_promotion` is that claim. If it ever fails, the job
detail page loses the alert that first surfaced the role, and the linking model
needs rethinking rather than patching.

No network: the model client is a scripted fake, the same way
`tests/test_llm_classification.py` injects its HTTP call.
"""

import unittest

from clients.llm_client import GroqRateLimited
from pipeline.acknowledgements import AcknowledgementHandler, application_date_from
from pipeline.alerts import AlertHandler
from pipeline.resolver import JobResolver
from pipeline.updates import UpdateHandler
from utilities.identity import identity_key
from utilities.mailstore import (
    CATEGORY_ACKNOWLEDGEMENT,
    CATEGORY_ALERT,
    CATEGORY_UPDATE,
    LEAD_APPLIED,
    MailStore,
)
from utilities.store import JobStore

LINKEDIN_ALERT = """
<html><body>
  <table>
    <tr><td>
      <a href="https://www.linkedin.com/comm/jobs/view/4123456789/?trackingId=aaa">
        Senior Backend Engineer
      </a>
      Stripe &middot; San Francisco, CA (Remote)
    </td></tr>
    <tr><td>
      <a href="https://www.linkedin.com/comm/jobs/view/4987654321/?trackingId=bbb">
        Product Designer
      </a>
      Figma &middot; New York, NY
    </td></tr>
    <tr><td>
      <a href="https://www.linkedin.com/comm/psettings/email">Unsubscribe</a>
    </td></tr>
  </table>
</body></html>
"""


class FakeClient:
    """Scripted stand-in for GroqClient.

    Returns the next queued reply for each call, so a test states exactly what
    the model "said" without a network or an API key.
    """

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.calls = []

    def complete_json(self, messages, parser, fallback, max_tokens=200):
        self.calls.append(messages)
        if not self.replies:
            return fallback
        return parser(self.replies.pop(0))


def make_app():
    store = JobStore(":memory:")
    mail = MailStore(store.conn)
    resolver = JobResolver(store, mail)
    return store, mail, resolver


def add_message(mail, message_id, sender, subject, body, category,
                date_header="Mon, 02 Feb 2026 09:00:00 -0800"):
    mail.upsert_message({
        "id": message_id, "thread_id": "t", "sender": sender,
        "subject": subject, "date": date_header, "labels": [], "snippet": "",
    })
    mail.store_body(message_id, body)
    mail.record_category(message_id, category, 0.95, "test")
    mail.commit()
    return mail.message(message_id)


class TestAlertToLeads(unittest.TestCase):
    def test_digest_becomes_several_leads(self):
        store, mail, _ = make_app()
        message = add_message(mail, "alert-1", "jobs-noreply@linkedin.com",
                              "2 new jobs", LINKEDIN_ALERT, CATEGORY_ALERT)

        created, skipped, linked = AlertHandler(store, mail).handle(message)
        self.assertEqual(created, 2)
        self.assertEqual(skipped, 0)

        titles = sorted(row["title"] for row in mail.list_leads())
        self.assertEqual(titles, ["Product Designer", "Senior Backend Engineer"])

    def test_one_email_links_to_many_identities(self):
        # The reason message_links is a link table and not a column.
        store, mail, _ = make_app()
        message = add_message(mail, "alert-1", "jobs-noreply@linkedin.com",
                              "2 new jobs", LINKEDIN_ALERT, CATEGORY_ALERT)
        AlertHandler(store, mail).handle(message)
        self.assertEqual(len(mail.links_for_message("alert-1")), 2)

    def test_apply_url_is_canonical_not_tracking(self):
        # A tracking wrapper can expire or be single-use; a dead link defeats
        # the entire click-through flow.
        store, mail, _ = make_app()
        message = add_message(mail, "alert-1", "jobs-noreply@linkedin.com",
                              "2 new jobs", LINKEDIN_ALERT, CATEGORY_ALERT)
        AlertHandler(store, mail).handle(message)

        lead = mail.lead_by_identity(
            identity_key("Senior Backend Engineer", "Stripe",
                         "San Francisco, CA (Remote)"))
        self.assertIsNotNone(lead)
        self.assertEqual(lead["apply_url"],
                         "https://www.linkedin.com/jobs/view/4123456789/")
        self.assertIn("trackingId", lead["tracking_url"])
        self.assertEqual(lead["board_job_id"], "4123456789")

    def test_repeat_digest_does_not_duplicate(self):
        store, mail, _ = make_app()
        first = add_message(mail, "alert-1", "jobs-noreply@linkedin.com",
                            "2 new jobs", LINKEDIN_ALERT, CATEGORY_ALERT)
        second = add_message(mail, "alert-2", "jobs-noreply@linkedin.com",
                             "2 new jobs", LINKEDIN_ALERT, CATEGORY_ALERT)
        handler = AlertHandler(store, mail)
        handler.handle(first)
        created, _, _ = handler.handle(second)

        self.assertEqual(created, 0, "same posting must not become a second lead")
        self.assertEqual(len(mail.list_leads()), 2)

    def test_already_applied_role_is_not_resurrected(self):
        # Boards keep recommending roles you have already applied to. A list
        # that resurrects finished work stops being trusted.
        store, mail, _ = make_app()
        store.create_job({
            "posting_url": "", "position_title": "Senior Backend Engineer",
            "company": "Stripe", "location": "San Francisco, CA (Remote)",
            "job_type": "Full time", "status": "Applied",
            "application_date": "2026-01-01",
        })
        message = add_message(mail, "alert-1", "jobs-noreply@linkedin.com",
                              "2 new jobs", LINKEDIN_ALERT, CATEGORY_ALERT)

        created, skipped, _ = AlertHandler(store, mail).handle(message)
        self.assertEqual(created, 1)
        self.assertEqual(skipped, 1)
        self.assertEqual([r["title"] for r in mail.list_leads()],
                         ["Product Designer"])


class TestAcknowledgementPromotes(unittest.TestCase):
    def setUp(self):
        self.store, self.mail, self.resolver = make_app()
        alert = add_message(self.mail, "alert-1", "jobs-noreply@linkedin.com",
                            "2 new jobs", LINKEDIN_ALERT, CATEGORY_ALERT,
                            date_header="Mon, 02 Feb 2026 09:00:00 -0800")
        AlertHandler(self.store, self.mail).handle(alert)
        self.key = identity_key("Senior Backend Engineer", "Stripe",
                                "San Francisco, CA (Remote)")

    def test_alert_email_survives_promotion(self):
        # THE test. The alert that surfaced the role must still be on the
        # job's timeline after the role becomes an application.
        ack = add_message(
            self.mail, "ack-1", "no-reply@stripe.com",
            "Thanks for applying", "We received your application.",
            CATEGORY_ACKNOWLEDGEMENT,
            date_header="Tue, 03 Feb 2026 10:00:00 -0800")
        client = FakeClient(['{"title": "Senior Backend Engineer", '
                            '"company": "Stripe", '
                            '"location": "San Francisco, CA (Remote)", '
                            '"confidence": 0.95, "reason": "receipt"}'])

        result = AcknowledgementHandler(
            self.store, self.mail, self.resolver, client).handle(ack)

        self.assertEqual(result["action"], "promoted")
        timeline = self.mail.messages_for_identity(self.key)
        self.assertEqual([row["gmail_message_id"] for row in timeline],
                         ["alert-1", "ack-1"])

    def test_lead_leaves_the_to_apply_list(self):
        ack = add_message(
            self.mail, "ack-1", "no-reply@stripe.com", "Thanks for applying",
            "We received your application.", CATEGORY_ACKNOWLEDGEMENT,
            date_header="Tue, 03 Feb 2026 10:00:00 -0800")
        client = FakeClient(['{"title": "Senior Backend Engineer", '
                            '"company": "Stripe", '
                            '"location": "San Francisco, CA (Remote)", '
                            '"confidence": 0.95, "reason": "receipt"}'])
        AcknowledgementHandler(self.store, self.mail, self.resolver, client).handle(ack)

        open_titles = [row["title"] for row in self.mail.list_leads()]
        self.assertNotIn("Senior Backend Engineer", open_titles)
        self.assertEqual(self.mail.lead_by_identity(self.key)["status"], LEAD_APPLIED)

    def test_job_keeps_the_leads_clean_fields(self):
        # The acknowledgement often says something looser than the board did.
        ack = add_message(
            self.mail, "ack-1", "no-reply@stripe.com", "Thanks for applying",
            "Thanks for applying to our Engineering team.",
            CATEGORY_ACKNOWLEDGEMENT,
            date_header="Tue, 03 Feb 2026 10:00:00 -0800")
        client = FakeClient(['{"title": "Engineering team", "company": "Stripe", '
                            '"location": null, "confidence": 0.6, "reason": "vague"}'])
        AcknowledgementHandler(self.store, self.mail, self.resolver, client).handle(ack)

        job = self.store.job_by_identity(self.key)
        self.assertIsNotNone(job, "should resolve to the lead, not the vague title")
        self.assertEqual(job["position_title"], "Senior Backend Engineer")
        self.assertEqual(job["board_job_id"] if "board_job_id" in job.keys() else None,
                         None)  # board data lives in job_sources, not on jobs
        sources = self.store.job_sources(job["job_id"])
        self.assertEqual(sources[0]["board_job_id"], "4123456789")

    def test_application_date_comes_from_the_email(self):
        # A backfill processing three-week-old mail must not stamp today.
        ack = add_message(
            self.mail, "ack-1", "no-reply@stripe.com", "Thanks for applying",
            "We received your application.", CATEGORY_ACKNOWLEDGEMENT,
            date_header="Tue, 06 Jan 2026 11:30:00 -0800")
        client = FakeClient(['{"title": "Senior Backend Engineer", '
                            '"company": "Stripe", '
                            '"location": "San Francisco, CA (Remote)", '
                            '"confidence": 0.95, "reason": "receipt"}'])
        AcknowledgementHandler(self.store, self.mail, self.resolver, client).handle(ack)

        job = self.store.job_by_identity(self.key)
        self.assertEqual(job["application_date"], "2026-01-06")

    def test_unknown_role_is_created_from_the_receipt(self):
        # No lead, no job - but an acknowledgement is evidence the application
        # really happened, so the job is created.
        ack = add_message(
            self.mail, "ack-9", "careers@newcorp.com", "Application received",
            "Thanks for applying to Data Engineer.", CATEGORY_ACKNOWLEDGEMENT)
        client = FakeClient(['{"title": "Data Engineer", "company": "NewCorp", '
                            '"location": "Remote", "confidence": 0.9, '
                            '"reason": "receipt"}'])

        result = AcknowledgementHandler(
            self.store, self.mail, self.resolver, client).handle(ack)
        self.assertEqual(result["action"], "created")

        job = self.store.find_job("Data Engineer", "NewCorp", "Remote")
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "Applied")


class TestUpdatesAfterPromotion(unittest.TestCase):
    def setUp(self):
        self.store, self.mail, self.resolver = make_app()
        alert = add_message(self.mail, "alert-1", "jobs-noreply@linkedin.com",
                            "2 new jobs", LINKEDIN_ALERT, CATEGORY_ALERT,
                            date_header="Mon, 02 Feb 2026 09:00:00 -0800")
        AlertHandler(self.store, self.mail).handle(alert)
        ack = add_message(self.mail, "ack-1", "no-reply@stripe.com",
                          "Thanks for applying", "We received it.",
                          CATEGORY_ACKNOWLEDGEMENT,
                          date_header="Tue, 03 Feb 2026 10:00:00 -0800")
        AcknowledgementHandler(
            self.store, self.mail, self.resolver,
            FakeClient(['{"title": "Senior Backend Engineer", "company": "Stripe", '
                        '"location": "San Francisco, CA (Remote)", '
                        '"confidence": 0.95, "reason": "receipt"}']),
        ).handle(ack)
        self.key = identity_key("Senior Backend Engineer", "Stripe",
                                "San Francisco, CA (Remote)")

    def test_confident_rejection_applies_and_appears_on_the_timeline(self):
        update = add_message(self.mail, "upd-1", "no-reply@stripe.com",
                             "Update on your application",
                             "We will not be moving forward.", CATEGORY_UPDATE,
                             date_header="Wed, 11 Feb 2026 14:00:00 -0800")
        client = FakeClient(['{"title": "Senior Backend Engineer", '
                            '"company": "Stripe", '
                            '"location": "San Francisco, CA (Remote)", '
                            '"status": "Rejected", "confidence": 0.97, '
                            '"reason": "explicit rejection"}'])

        outcome = UpdateHandler(self.store, self.mail, self.resolver,
                                client).handle(update)
        self.assertTrue(outcome["status_applied"])
        self.assertEqual(self.store.job_by_identity(self.key)["status"], "Rejected")

        timeline = [r["gmail_message_id"] for r in
                    self.mail.messages_for_identity(self.key)]
        self.assertEqual(timeline, ["alert-1", "ack-1", "upd-1"])

    def test_low_confidence_links_without_applying(self):
        # Placing an email and understanding it are independent decisions.
        update = add_message(self.mail, "upd-2", "no-reply@stripe.com",
                             "A quick note", "Some ambiguous text.",
                             CATEGORY_UPDATE,
                             date_header="Wed, 11 Feb 2026 14:00:00 -0800")
        client = FakeClient(['{"title": "Senior Backend Engineer", '
                            '"company": "Stripe", '
                            '"location": "San Francisco, CA (Remote)", '
                            '"status": "Interview", "confidence": 0.4, '
                            '"reason": "unsure"}'])

        outcome = UpdateHandler(self.store, self.mail, self.resolver,
                                client).handle(update)
        self.assertTrue(outcome["linked"], "should still show on the timeline")
        self.assertFalse(outcome["status_applied"])
        self.assertEqual(self.store.job_by_identity(self.key)["status"], "Applied")

    def test_status_write_is_reversible(self):
        update = add_message(self.mail, "upd-3", "no-reply@stripe.com",
                             "Update", "We will not be moving forward.",
                             CATEGORY_UPDATE,
                             date_header="Wed, 11 Feb 2026 14:00:00 -0800")
        client = FakeClient(['{"title": "Senior Backend Engineer", '
                            '"company": "Stripe", '
                            '"location": "San Francisco, CA (Remote)", '
                            '"status": "Rejected", "confidence": 0.97, '
                            '"reason": "rejection"}'])
        handler = UpdateHandler(self.store, self.mail, self.resolver, client)
        handler.handle(update)

        job = self.store.job_by_identity(self.key)
        self.assertEqual(job["status"], "Rejected")
        self.assertTrue(handler.undo(job["job_id"]))

        restored = self.store.job_by_identity(self.key)
        self.assertEqual(restored["status"], "Applied")
        self.assertIsNone(restored["response_date"])


class RateLimitedClient:
    """Raises GroqRateLimited on every call, like an exhausted free tier."""

    def __init__(self, retry_after=42):
        self.retry_after = retry_after
        self.calls = 0

    def complete_json(self, messages, parser, fallback, max_tokens=200):
        self.calls += 1
        raise GroqRateLimited("rate limited", retry_after=self.retry_after)


class TestRateLimitStopsBatchesCleanly(unittest.TestCase):
    """A 429 must pause a batch, not fail it and not crash the cycle.

    Before this, a rate limit during alert extraction was swallowed by a
    catch-all and logged as "Model extraction failed" - so a real rate limit
    looked like an unparseable email, and the handler kept walking the batch
    into the same wall. In updates and acknowledgements it escaped entirely and
    took the whole pipeline cycle down with it.
    """

    def setUp(self):
        self.store, self.mail, self.resolver = make_app()
        self.client = RateLimitedClient()

    def _alert(self, message_id="alert-1"):
        return add_message(self.mail, message_id, "jobs-noreply@unknownboard.io",
                           "5 new jobs", "<a href='https://x.test/1'>Job</a>",
                           CATEGORY_ALERT)

    def test_alert_run_stops_instead_of_walking_the_whole_batch(self):
        for index in range(3):
            self._alert(f"alert-{index}")
        handler = AlertHandler(self.store, self.mail, self.client)

        created, _skipped, _linked = handler.run(limit=10)

        self.assertEqual(created, 0)
        self.assertEqual(self.client.calls, 1,
                         "must stop after the first 429, not retry each message")

    def test_alert_messages_stay_unlinked_so_they_retry(self):
        self._alert()
        AlertHandler(self.store, self.mail, self.client).run(limit=10)
        # Unlinked is what makes the next cycle pick it up again.
        self.assertEqual(
            [row["gmail_message_id"] for row in self.mail.unlinked_messages()],
            ["alert-1"],
        )

    def test_update_run_stops_without_raising(self):
        add_message(self.mail, "upd-1", "no-reply@stripe.com", "Update",
                    "Some text.", CATEGORY_UPDATE)
        result = UpdateHandler(self.store, self.mail, self.resolver,
                               self.client).run(limit=10)
        self.assertEqual(result["processed"], 0)

    def test_acknowledgement_run_stops_without_raising(self):
        add_message(self.mail, "ack-1", "no-reply@stripe.com", "Thanks",
                    "We received it.", CATEGORY_ACKNOWLEDGEMENT)
        counts = AcknowledgementHandler(self.store, self.mail, self.resolver,
                                        self.client).run(limit=10)
        self.assertEqual(sum(counts.values()), 0)

    def test_work_done_before_the_limit_is_kept(self):
        # A limit part-way through a batch must not discard earlier results.
        good = FakeClient(['{"postings": [{"title": "Data Engineer", '
                           '"company": "Acme", "location": null, "url": null}]}'])
        self._alert("alert-a")
        created, _s, _l = AlertHandler(self.store, self.mail, good).handle(
            self.mail.message("alert-a"))
        self.assertEqual(created, 1)

        self._alert("alert-b")
        AlertHandler(self.store, self.mail, self.client).run(limit=10)
        self.assertEqual(len(self.mail.list_leads()), 1,
                         "the lead created before the limit must survive")


class TestApplicationDateParsing(unittest.TestCase):
    def test_reads_the_header(self):
        self.assertEqual(
            application_date_from({"received_date": "Tue, 06 Jan 2026 11:30:00 -0800"}),
            "2026-01-06")

    def test_falls_back_when_unparseable(self):
        result = application_date_from({"received_date": "not a date"})
        self.assertRegex(result, r"^\d{4}-\d{2}-\d{2}$")


if __name__ == "__main__":
    unittest.main()
