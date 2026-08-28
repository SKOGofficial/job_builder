"""One vocabulary for "pending", and an end to unbounded retries.

Two honest counts of the classification backlog disagreed by 264 messages.
`count_awaiting_classification` requires a non-empty body and read 0; anything
counting `category IS NULL` read 265, because the rough filter drops a message
on its headers and nothing ever fetches a body for it - so it can never leave
that set. Both numbers were right about different things, and neither said
which.

The cost was not only the confusion. `unclassified_headers` had no verdict
clause and no limit, so the free rule tier re-read all 264 dropped rows on
every ten-minute cycle and declined all of them, for ever.

The other half is the attempt ceiling. The model queue is oldest-first, so a
message no provider can answer sits at its head and is tried *first* on every
cycle, at cost, with nothing anywhere recording that it had ever failed.
"""

import asyncio
import unittest

from pipeline.classify import RULE_MODEL
from pipeline.router import MessageRouter
from pipeline.rough_filter import DROP_DENYLISTED, DROP_PERSONAL
from utilities.mailstore import (
    CATEGORY_ALERT,
    EMPTY_BODY_REASON,
    MAX_CLASSIFY_ATTEMPTS,
    VERDICT_PASSED,
    MailStore,
)
from utilities.store import JobStore


def make_stores():
    store = JobStore(":memory:")
    return store, MailStore(store.conn)


def add_message(mail, message_id, subject="Subject", sender="a@example.com",
                verdict=None, body=None, category=None):
    """
    Summary:
        Insert one message in a chosen pipeline state.

    Parameters:
        mail (MailStore): The store to write to.
        message_id (str): Gmail id.
        subject (str): Subject line.
        sender (str): From header.
        verdict (str | None): Filter verdict to stamp, if any.
        body (str | None): Body to store. An empty string stores an empty
            body, which is the state that used to be a silent dead letter.
        category (str | None): Category to record, if any.
    """
    mail.upsert_message({"id": message_id, "sender": sender,
                         "subject": subject, "date": ""})
    if verdict is not None:
        mail.set_filter_verdict(message_id, verdict)
    if body is not None:
        mail.store_body(message_id, body)
    if category is not None:
        mail.record_category(message_id, category, 0.9, "test")
    mail.commit()


async def immediate(fn, *args):
    return fn(*args)


class Exploding:
    """A client that fails the same way every time, for one message."""

    def __init__(self):
        self.calls = 0

    def complete_json(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("this payload will never parse")


class DroppedMailIsNotABacklogTests(unittest.TestCase):
    def setUp(self):
        self.store, self.mail = make_stores()

    def tearDown(self):
        self.store.close()

    def test_the_rule_tier_does_not_re_read_dropped_mail(self):
        add_message(self.mail, "dropped", verdict=DROP_PERSONAL)
        add_message(self.mail, "denied", verdict=DROP_DENYLISTED)
        add_message(self.mail, "passed", verdict=VERDICT_PASSED)

        ids = [row["gmail_message_id"]
               for row in self.mail.unclassified_headers()]
        self.assertEqual(ids, ["passed"])

    def test_a_message_with_no_verdict_yet_still_reaches_the_rules(self):
        # The filter and the rule tier run in the same cycle, and the order is
        # not something this query should depend on.
        add_message(self.mail, "unfiltered")
        ids = [row["gmail_message_id"]
               for row in self.mail.unclassified_headers()]
        self.assertEqual(ids, ["unfiltered"])

    def test_dropped_mail_is_reported_as_dropped_not_as_pending(self):
        add_message(self.mail, "dropped", verdict=DROP_PERSONAL)
        add_message(self.mail, "denied", verdict=DROP_DENYLISTED)

        depths = self.mail.queue_depths()
        self.assertEqual(depths["awaiting_rules"], 0)
        self.assertEqual(depths["awaiting_classification"], 0)
        self.assertEqual(
            self.mail.filtered_out(),
            {DROP_PERSONAL: 1, DROP_DENYLISTED: 1},
        )

    def test_every_queue_is_counted_by_the_predicate_that_drains_it(self):
        add_message(self.mail, "needs-verdict")
        add_message(self.mail, "needs-body", verdict=VERDICT_PASSED)
        add_message(self.mail, "needs-model", verdict=VERDICT_PASSED,
                    body="a body the rules cannot place")
        add_message(self.mail, "needs-handling", verdict=VERDICT_PASSED,
                    body="body", category=CATEGORY_ALERT)

        depths = self.mail.queue_depths()
        self.assertEqual(depths["awaiting_filter"], 1)
        self.assertEqual(depths["awaiting_body"], 1)
        self.assertEqual(depths["awaiting_handling_job_alert"], 1)
        # The badge and the breakdown must be the same number, always.
        self.assertEqual(depths["awaiting_classification"],
                         self.mail.count_awaiting_classification())


class EmptyBodiesAreRetiredTests(unittest.TestCase):
    def setUp(self):
        self.store, self.mail = make_stores()

    def tearDown(self):
        self.store.close()

    def test_a_fetched_but_empty_body_is_retired_with_a_reason(self):
        add_message(self.mail, "empty", verdict=VERDICT_PASSED, body="")

        self.assertEqual(self.mail.retire_unclassifiable(), 1)
        row = self.store.conn.execute(
            "SELECT classify_attempts, classify_error FROM messages "
            "WHERE gmail_message_id = 'empty'"
        ).fetchone()
        self.assertEqual(row["classify_attempts"], MAX_CLASSIFY_ATTEMPTS)
        self.assertEqual(row["classify_error"], EMPTY_BODY_REASON)
        self.assertEqual(self.mail.queue_depths()["dead_lettered"], 1)

    def test_a_message_whose_body_has_not_been_fetched_is_left_alone(self):
        # Still waiting its turn at the body fetcher. Retiring it here would
        # throw away mail that has never been looked at.
        add_message(self.mail, "waiting", verdict=VERDICT_PASSED)
        self.assertEqual(self.mail.retire_unclassifiable(), 0)

    def test_retiring_is_idempotent(self):
        add_message(self.mail, "empty", verdict=VERDICT_PASSED, body="")
        self.mail.retire_unclassifiable()
        self.assertEqual(self.mail.retire_unclassifiable(), 0)


class AttemptsAreBoundedTests(unittest.TestCase):
    def setUp(self):
        self.store, self.mail = make_stores()

    def tearDown(self):
        self.store.close()

    def router(self, client):
        return MessageRouter(self.mail, client_factory=lambda: client,
                             executor=immediate)

    def test_a_message_that_never_parses_stops_being_offered(self):
        add_message(self.mail, "poison", subject="Nothing a rule can place",
                    verdict=VERDICT_PASSED, body="a body")
        client = Exploding()
        router = self.router(client)

        for _ in range(MAX_CLASSIFY_ATTEMPTS + 2):
            asyncio.run(router.run(limit=10))

        self.assertEqual(client.calls, MAX_CLASSIFY_ATTEMPTS)
        self.assertEqual(self.mail.count_awaiting_classification(), 0)
        row = self.store.conn.execute(
            "SELECT classify_attempts, classify_error FROM messages "
            "WHERE gmail_message_id = 'poison'"
        ).fetchone()
        self.assertEqual(row["classify_attempts"], MAX_CLASSIFY_ATTEMPTS)
        self.assertIn("never parse", row["classify_error"])

    def test_a_retired_message_does_not_block_the_ones_behind_it(self):
        # The whole point of the ceiling. Oldest-first means the bad message is
        # tried first every time; the good one behind it must still get through.
        add_message(self.mail, "poison", subject="Unplaceable",
                    verdict=VERDICT_PASSED, body="a body")

        class OnlyFailsTheFirst:
            def __init__(self):
                self.seen = []

            def complete_json(self, messages, parse, fallback, *args):
                body = messages[-1]["content"]
                self.seen.append(body)
                if "Unplaceable" in body:
                    raise RuntimeError("no")
                return {"label": CATEGORY_ALERT, "confidence": 0.9,
                        "reason": "ok"}

        client = OnlyFailsTheFirst()
        router = self.router(client)
        add_message(self.mail, "good", subject="Also unplaceable by rules",
                    verdict=VERDICT_PASSED, body="a good body")

        asyncio.run(router.run(limit=10))
        self.assertEqual(
            self.store.conn.execute(
                "SELECT category FROM messages WHERE gmail_message_id = 'good'"
            ).fetchone()["category"],
            CATEGORY_ALERT,
        )

    def test_a_rate_limit_is_not_counted_against_the_message(self):
        # A 429 applies to every message behind this one too. Counting it would
        # retire a queue's worth of good mail for the crime of being at the
        # front during a bad afternoon.
        from clients.llm_client import GroqRateLimited

        add_message(self.mail, "unlucky", subject="Unplaceable",
                    verdict=VERDICT_PASSED, body="a body")

        class AlwaysLimited:
            def complete_json(self, *args, **kwargs):
                raise GroqRateLimited("slow down", retry_after=30)

        router = self.router(AlwaysLimited())
        for _ in range(MAX_CLASSIFY_ATTEMPTS + 2):
            asyncio.run(router.run(limit=10))

        self.assertEqual(
            self.store.conn.execute(
                "SELECT classify_attempts FROM messages "
                "WHERE gmail_message_id = 'unlucky'"
            ).fetchone()["classify_attempts"],
            0,
        )
        self.assertEqual(self.mail.count_awaiting_classification(), 1)

    def test_the_rules_still_answer_a_message_the_model_kept_failing(self):
        # Attempts are recorded against the message, not against the tier. A
        # rule learning to place a sender later must still work.
        add_message(self.mail, "unplaceable", subject="Unplaceable",
                    verdict=VERDICT_PASSED, body="a body")
        self.mail.record_classify_failure("unplaceable", "first")
        self.mail.record_classify_failure("unplaceable", "second")
        self.mail.commit()

        self.assertEqual(
            [row["gmail_message_id"]
             for row in self.mail.unclassified_headers()],
            ["unplaceable"],
        )

        self.mail.record_category("unplaceable", CATEGORY_ALERT, 1.0, "rule",
                                  RULE_MODEL)
        self.mail.commit()
        self.assertEqual(self.mail.queue_depths()["awaiting_rules"], 0)


if __name__ == "__main__":
    unittest.main()
