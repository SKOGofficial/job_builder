"""Free work must not queue behind expensive work.

The router used to hand the rule tier the same `limit` rows the model would
take, oldest first. On a real mailbox that made the cheap tier useless exactly
when it was needed: all 60 of the oldest unclassified messages were ones the
rules decline, so the rule pass answered none of them, the model took all 60 at
one or two per cycle against a rate-limited provider, and the 104 messages
behind them that the rules could have answered instantly and for free were
never reached at all.

`limit` now means what it should - how many *model calls* a pass may make - and
the rules sweep the whole backlog.
"""

import asyncio
import unittest

from pipeline.router import MessageRouter
from utilities.mailstore import CATEGORY_ALERT, MailStore
from utilities.store import JobStore


def make_mail():
    return MailStore(JobStore(":memory:").conn)


async def immediate(fn, *args):
    return fn(*args)


class CountingClient:
    last_model = "test-model"

    def __init__(self):
        self.calls = 0

    def complete_json(self, messages, parse, fallback):
        self.calls += 1
        return {"label": "irrelevant", "confidence": 0.5, "reason": "stub"}


def add(mail, message_id, sender, subject, stamp):
    mail.upsert_message({"id": message_id, "sender": sender,
                         "subject": subject, "date": stamp})
    mail.store_body(message_id, "body text")
    mail.commit()


OLD = "Tue, 28 Jul 2026 10:00:00 -0400"
NEW = "Wed, 05 Aug 2026 10:00:00 -0400"

#: Declined by the rules on purpose - see `pipeline/classify.py`.
OPAQUE = ("Careers <no-reply@unknowncorp.test>", "Thank you for your interest")
BOARD = ("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
         "Backend Engineer at Stripe")


class TestRulesSweepTheWholeBacklog(unittest.TestCase):
    def setUp(self):
        self.mail = make_mail()
        self.client = CountingClient()

    def route(self, limit):
        router = MessageRouter(self.mail, client_factory=lambda: self.client,
                               executor=immediate)
        asyncio.run(router.run(limit=limit))
        return router

    def test_a_wall_of_opaque_mail_does_not_hide_the_easy_mail(self):
        """The bug, reproduced: the old batch would be 100% opaque."""
        for index in range(100):
            add(self.mail, f"opaque-{index}", *OPAQUE, OLD)
        for index in range(5):
            add(self.mail, f"board-{index}", *BOARD, NEW)

        router = self.route(limit=2)

        self.assertEqual(router.by_rule, 5,
                         "every board alert is labelled, however deep it sits")
        for index in range(5):
            self.assertEqual(
                self.mail.message(f"board-{index}")["category"], CATEGORY_ALERT)

    def test_the_limit_now_bounds_model_calls_only(self):
        for index in range(100):
            add(self.mail, f"opaque-{index}", *OPAQUE, OLD)
        for index in range(5):
            add(self.mail, f"board-{index}", *BOARD, NEW)

        self.route(limit=2)

        self.assertEqual(self.client.calls, 2,
                         "the expensive tier is still rationed")

    def test_rule_results_are_committed_before_the_model_is_asked(self):
        """A slow or dying model pass must not take the free labels with it."""
        add(self.mail, "board-1", *BOARD, NEW)
        add(self.mail, "opaque-1", *OPAQUE, OLD)

        class Exploding:
            last_model = "boom"

            def complete_json(self, *args, **kwargs):
                raise RuntimeError("model died mid-pass")

        router = MessageRouter(self.mail, client_factory=Exploding,
                               executor=immediate)
        asyncio.run(router.run(limit=10))

        self.assertEqual(self.mail.message("board-1")["category"],
                         CATEGORY_ALERT,
                         "the rule label survives the model blowing up")

    def test_no_provider_still_classifies_everything_the_rules_can(self):
        from clients.llm_client import GroqNotConfigured

        for index in range(3):
            add(self.mail, f"board-{index}", *BOARD, NEW)
        add(self.mail, "opaque-1", *OPAQUE, OLD)

        def unavailable():
            raise GroqNotConfigured("no provider")

        router = MessageRouter(self.mail, client_factory=unavailable,
                               executor=immediate)
        asyncio.run(router.run(limit=10))

        self.assertEqual(router.by_rule, 3)
        self.assertIsNone(self.mail.message("opaque-1")["category"])


class TestHeadersOnlyQuery(unittest.TestCase):
    def test_it_returns_every_unclassified_message(self):
        mail = make_mail()
        for index in range(5):
            add(mail, f"m{index}", *BOARD, NEW)
        self.assertEqual(len(mail.unclassified_headers()), 5)

    def test_a_classified_message_drops_out(self):
        mail = make_mail()
        add(mail, "m1", *BOARD, NEW)
        mail.record_category("m1", CATEGORY_ALERT, 0.9, "test")
        mail.commit()
        self.assertEqual(mail.unclassified_headers(), [])

    def test_it_carries_no_body(self):
        """Loading thousands of bodies to regex a subject line is the cost."""
        mail = make_mail()
        add(mail, "m1", *BOARD, NEW)
        row = mail.unclassified_headers()[0]
        self.assertEqual(row.keys(), ["gmail_message_id", "sender", "subject"])


if __name__ == "__main__":
    unittest.main()
