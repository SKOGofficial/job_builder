"""`handled_at`, and the router's rule-then-model split.

Handlers used to select their backlog as "in my category and linked to
nothing". Nothing about that query says whether a message has been *tried*, so
anything that legitimately produces no link - a digest carrying no parseable
posting, an update about a role that was never applied to - matched it again on
every cycle and was re-extracted at full model cost, for ever.

The router half is the same idea one stage earlier: whatever the rules can
answer must never reach a provider, and whatever they cannot must still be
classified when a provider is available.
"""

import asyncio
import unittest

from pipeline.alerts import AlertHandler
from pipeline.router import MessageRouter
from utilities.mailstore import (
    CATEGORY_ACKNOWLEDGEMENT,
    CATEGORY_ALERT,
    MailStore,
)
from utilities.store import JobStore


def make_stores():
    store = JobStore(":memory:")
    return store, MailStore(store.conn)


def add_message(mail, message_id, sender, subject, body="", category=None):
    mail.upsert_message({"id": message_id, "sender": sender,
                         "subject": subject, "date": ""})
    if body:
        mail.store_body(message_id, body)
    if category:
        mail.record_category(message_id, category, 0.9, "test")
    mail.commit()


async def immediate(fn, *args):
    return fn(*args)


class NoPostings:
    """A parser client that finds nothing, however hard it is asked."""

    def __init__(self):
        self.calls = 0

    def complete_json(self, *args, **kwargs):
        self.calls += 1
        return []


class TestEmptyAlertsAreNotRetriedForever(unittest.TestCase):
    def setUp(self):
        self.store, self.mail = make_stores()
        add_message(self.mail, "a1", "MyGreenhouse <no-reply@example.test>",
                    "Welcome to MyGreenhouse. Start your search.",
                    "Nothing to parse here.", CATEGORY_ALERT)

    def test_an_alert_with_no_postings_is_still_marked_handled(self):
        handler = AlertHandler(self.store, self.mail, NoPostings(),
                               executor=immediate)
        asyncio.run(handler.run(limit=10))

        self.assertEqual(self.mail.messages_awaiting_handling(CATEGORY_ALERT), [])
        self.assertIsNotNone(self.mail.message("a1")["handled_at"])

    def test_the_second_cycle_does_not_pay_for_it_again(self):
        client = NoPostings()
        handler = AlertHandler(self.store, self.mail, client, executor=immediate)

        asyncio.run(handler.run(limit=10))
        asyncio.run(handler.run(limit=10))

        self.assertEqual(client.calls, 1,
                         "the same empty digest must not be re-extracted")

    def test_an_unclassified_message_is_not_in_any_backlog(self):
        add_message(self.mail, "a2", "someone@example.test", "No category yet")
        self.assertEqual(
            [row["gmail_message_id"]
             for row in self.mail.messages_awaiting_handling(CATEGORY_ALERT)],
            ["a1"],
        )


class TestNoProviderIsNotAnAttempt(unittest.TestCase):
    """The failure mode `handled_at` could easily have introduced.

    Marking a message handled means no handler ever looks at it again. A cycle
    that had no model to extract with has not tried, it has been unable to try,
    and treating the two the same would discard real leads and real receipts
    every time a provider was cooling off.
    """

    def test_an_alert_is_left_alone_when_there_is_no_model(self):
        store, mail = make_stores()
        add_message(mail, "a1", "Board <alerts@unknownboard.test>",
                    "5 new jobs for you", "<p>nothing parseable</p>",
                    CATEGORY_ALERT)

        handler = AlertHandler(store, mail, client=None, executor=immediate)
        asyncio.run(handler.run(limit=10))

        self.assertIsNone(mail.message("a1")["handled_at"])
        self.assertEqual(
            [row["gmail_message_id"]
             for row in mail.messages_awaiting_handling(CATEGORY_ALERT)],
            ["a1"],
        )

    def test_an_acknowledgement_is_left_alone_when_there_is_no_model(self):
        from pipeline.acknowledgements import AcknowledgementHandler
        from pipeline.resolver import JobResolver

        store, mail = make_stores()
        add_message(mail, "k1", "Careers <no-reply@unknowncorp.test>",
                    "Thank you for applying", "We got it.",
                    CATEGORY_ACKNOWLEDGEMENT)

        handler = AcknowledgementHandler(store, mail, JobResolver(store, mail),
                                         client=None, executor=immediate)
        asyncio.run(handler.run(limit=10))

        self.assertIsNone(mail.message("k1")["handled_at"])


class TestBacklogOrdering(unittest.TestCase):
    def test_alerts_come_newest_first_and_receipts_oldest_first(self):
        """Opposite orders, for opposite reasons.

        A fresh posting is worth more than an old one, so alerts lead with the
        newest. An acknowledgement sets an application date, so the receipt that
        arrived first has to be the one that promotes the lead.
        """
        _store, mail = make_stores()
        for index, stamp in enumerate(["Tue, 28 Jul 2026 10:00:00 -0400",
                                       "Wed, 05 Aug 2026 10:00:00 -0400"]):
            for category in (CATEGORY_ALERT, CATEGORY_ACKNOWLEDGEMENT):
                key = f"{category}-{index}"
                mail.upsert_message({"id": key, "sender": "a@b.test",
                                     "subject": "s", "date": stamp})
                mail.record_category(key, category, 0.9, "test")
        mail.commit()

        alerts = mail.messages_awaiting_handling(CATEGORY_ALERT)
        receipts = mail.messages_awaiting_handling(
            CATEGORY_ACKNOWLEDGEMENT, newest_first=False)

        self.assertEqual([row["gmail_message_id"] for row in alerts],
                         ["job_alert-1", "job_alert-0"])
        self.assertEqual([row["gmail_message_id"] for row in receipts],
                         ["job_acknowledgement-0", "job_acknowledgement-1"])


class CountingClient:
    """Stands in for a provider, and records how often it was reached."""

    last_model = "test-model"

    def __init__(self):
        self.calls = 0

    def complete_json(self, messages, parse, fallback):
        self.calls += 1
        return {"label": "irrelevant", "confidence": 0.5, "reason": "stub"}


class TestRouterUsesRulesFirst(unittest.TestCase):
    def setUp(self):
        self.store, self.mail = make_stores()
        self.client = CountingClient()

    def route(self):
        router = MessageRouter(self.mail, client_factory=lambda: self.client,
                               executor=immediate)
        counts = asyncio.run(router.run(limit=10))
        return router, counts

    def test_a_board_digest_never_reaches_the_model(self):
        add_message(self.mail, "m1",
                    "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
                    "Software Engineer, Platform at Doppel", "body")

        router, counts = self.route()

        self.assertEqual(self.client.calls, 0)
        self.assertEqual(counts, {CATEGORY_ALERT: 1})
        self.assertEqual(router.by_rule, 1)

    def test_the_label_is_attributed_to_the_rules(self):
        add_message(self.mail, "m1", "Indeed <donotreply@match.indeed.com>",
                    "Software Engineer @ AHEAD", "body")
        self.route()

        self.assertEqual(self.mail.message("m1")["category_model"], "rules")

    def test_what_the_rules_decline_still_goes_to_the_model(self):
        add_message(self.mail, "m1", "no-reply@tcomcareers.com",
                    "Thank you for your interest in TCOM, L.P.", "body")

        router, _counts = self.route()

        self.assertEqual(self.client.calls, 1)
        self.assertEqual(router.by_rule, 0)

    def test_a_mixed_batch_splits_between_the_two(self):
        add_message(self.mail, "m1",
                    "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
                    "Backend Engineer at Stripe", "body")
        add_message(self.mail, "m2", "hello@somewhere.test",
                    "Thank you for your interest in us", "body")

        router, _counts = self.route()

        self.assertEqual(router.by_rule, 1)
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(router.processed, 2)


class TestRoutingWithoutAProvider(unittest.TestCase):
    def test_rules_classify_even_when_no_model_can_be_built(self):
        """The reason classification moved out of the model-gated stages.

        An exhausted free tier used to freeze the to-apply list for the rest of
        the day over work that costs nothing.
        """
        from clients.llm_client import GroqNotConfigured

        _store, mail = make_stores()
        add_message(mail, "m1", "ZipRecruiter <alerts@ziprecruiter.com>",
                    "$157K/yr Test Engineer job in Chantilly, VA", "body")
        add_message(mail, "m2", "someone@example.test",
                    "Thank you for your interest", "body")

        def unavailable():
            raise GroqNotConfigured("no provider")

        router = MessageRouter(mail, client_factory=unavailable,
                               executor=immediate)
        counts = asyncio.run(router.run(limit=10))

        self.assertEqual(counts, {CATEGORY_ALERT: 1})
        self.assertIsNone(mail.message("m2")["category"],
                          "the undecidable one waits for a provider")


if __name__ == "__main__":
    unittest.main()
