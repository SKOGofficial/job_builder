"""A failed model call must leave the message exactly as it found it.

This is the regression suite for a real data loss. `GROQ_MODEL` in `.env` named
a decommissioned model, so every extraction call returned HTTP 404.
`parse_alert` caught the resulting bare exception and returned no postings;
`AlertHandler` read "no postings" as "this digest contains nothing" and stamped
`handled_at`, which permanently removes a message from the backlog. 70 real job
alerts were retired that way before it was caught.

The distinction the whole fix rests on:

- The model answered and there was nothing in it -> handled. Marking it is what
  stops an empty board digest being re-extracted at full cost every cycle.
- The call never reached a model -> **not** handled. Nothing was tried, so
  nothing may be recorded as tried.

`test_the_bug_itself` is the one that fails against the unfixed code.
"""

import asyncio
import unittest

from clients.providers.base import ProviderRateLimited, ProviderUnavailable
from pipeline.acknowledgements import AcknowledgementHandler
from pipeline.alerts import AlertHandler
from pipeline.resolver import JobResolver
from utilities.mailstore import CATEGORY_ACKNOWLEDGEMENT, CATEGORY_ALERT, MailStore
from utilities.store import JobStore


def make_stores():
    store = JobStore(":memory:")
    return store, MailStore(store.conn)


def add_alert(mail, message_id="a1",
              sender="Board <alerts@unknownboard.test>",
              subject="5 new jobs for you",
              body="<p>Software Engineer at Acme</p>"):
    """An alert no deterministic parser claims, so extraction must run."""
    mail.upsert_message({"id": message_id, "sender": sender,
                         "subject": subject, "date": ""})
    mail.store_body(message_id, body)
    mail.record_category(message_id, CATEGORY_ALERT, 0.9, "digest")
    mail.commit()


async def immediate(fn, *args):
    return fn(*args)


class BrokenClient:
    """A configured provider that cannot serve: the decommissioned model."""

    last_model = "llama-3.3-70b-versatile"

    def __init__(self, error=None):
        self.calls = 0
        self.error = error or ProviderUnavailable(
            "Groq returned HTTP 404: model_not_found",
            provider="Groq", status=404,
        )

    def complete_json(self, *args, **kwargs):
        self.calls += 1
        raise self.error


class EmptyClient:
    """A provider that works and honestly reports an empty digest."""

    last_model = "working-model"

    def __init__(self):
        self.calls = 0

    def complete_json(self, *args, **kwargs):
        self.calls += 1
        return []


class TestTheBugItself(unittest.TestCase):
    def test_the_bug_itself(self):
        """A 404 must not retire the alert. Fails against the unfixed code."""
        store, mail = make_stores()
        add_alert(mail)

        client = BrokenClient()
        asyncio.run(AlertHandler(store, mail, client, executor=immediate).run(10))

        self.assertIsNone(
            mail.message("a1")["handled_at"],
            "a call that never reached a model is not an attempt",
        )
        self.assertEqual(
            [row["gmail_message_id"]
             for row in mail.messages_awaiting_handling(CATEGORY_ALERT)],
            ["a1"],
            "the alert must still be in the backlog for the next cycle",
        )

    def test_the_batch_stops_rather_than_burning_through_it(self):
        """One dead provider must not retire the whole backlog in one pass."""
        store, mail = make_stores()
        for index in range(5):
            add_alert(mail, f"a{index}")

        client = BrokenClient()
        asyncio.run(AlertHandler(store, mail, client, executor=immediate).run(10))

        self.assertEqual(client.calls, 1,
                         "stop on the first failure; the rest is the same failure")
        self.assertEqual(
            len(mail.messages_awaiting_handling(CATEGORY_ALERT, limit=10)), 5)

    def test_recovery_needs_no_repair(self):
        """Once a provider works again, the untouched backlog just runs."""
        store, mail = make_stores()
        add_alert(mail)

        asyncio.run(AlertHandler(store, mail, BrokenClient(),
                                 executor=immediate).run(10))
        self.assertIsNone(mail.message("a1")["handled_at"])

        # Same message, working provider that reports an honestly empty digest.
        asyncio.run(AlertHandler(store, mail, EmptyClient(),
                                 executor=immediate).run(10))
        self.assertIsNotNone(mail.message("a1")["handled_at"])


class TestTheDistinctionIsPreserved(unittest.TestCase):
    """The fix must not resurrect the retry leak it sits next to."""

    def test_an_honestly_empty_digest_is_still_marked_handled(self):
        store, mail = make_stores()
        add_alert(mail, body="<p>Nothing resembling a posting.</p>")

        client = EmptyClient()
        asyncio.run(AlertHandler(store, mail, client, executor=immediate).run(10))

        self.assertIsNotNone(mail.message("a1")["handled_at"])
        self.assertEqual(mail.messages_awaiting_handling(CATEGORY_ALERT), [])

    def test_it_is_not_re_extracted_on_the_next_cycle(self):
        store, mail = make_stores()
        add_alert(mail)
        client = EmptyClient()
        handler = AlertHandler(store, mail, client, executor=immediate)

        asyncio.run(handler.run(10))
        asyncio.run(handler.run(10))

        self.assertEqual(client.calls, 1)

    def test_a_rate_limit_still_leaves_the_message_alone(self):
        """The precedent this fix generalises, still working."""
        store, mail = make_stores()
        add_alert(mail)

        client = BrokenClient(error=ProviderRateLimited(
            "busy", retry_after=30, provider="Groq"))
        asyncio.run(AlertHandler(store, mail, client, executor=immediate).run(10))

        self.assertIsNone(mail.message("a1")["handled_at"])


class TestAcknowledgementsToo(unittest.TestCase):
    def test_a_receipt_survives_a_dead_provider(self):
        """Losing one of these loses the record that an application was sent."""
        store, mail = make_stores()
        mail.upsert_message({"id": "k1", "sender": "Careers <no-reply@acme.test>",
                             "subject": "Thank you for applying", "date": ""})
        mail.store_body("k1", "We received your application.")
        mail.record_category("k1", CATEGORY_ACKNOWLEDGEMENT, 0.95, "receipt")
        mail.commit()

        handler = AcknowledgementHandler(
            store, mail, JobResolver(store, mail),
            client=BrokenClient(), executor=immediate)
        asyncio.run(handler.run(10))

        self.assertIsNone(mail.message("k1")["handled_at"])


if __name__ == "__main__":
    unittest.main()
