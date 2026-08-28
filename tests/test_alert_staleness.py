"""Alerts too old to yield anything are retired without paying for them.

Extraction is the most expensive model call in the pipeline. A lead built from
an alert older than `LEAD_FRESHNESS_DAYS` is deleted by `purge_stale_leads` on
the same cycle that created it, so the work is not merely low-value - its yield
is arithmetically zero.

The alert queue reached 469 with its oldest entry seven weeks back, draining
newest-first at fifteen a cycle. The tail was never going to be reached, and
reaching it would have bought nothing. Retiring it first is what makes the
oldest-first ordering affordable, and oldest-first is what stops anything
inside the window waiting for ever.
"""

import asyncio
import time
import unittest

from pipeline.alerts import AlertHandler
from utilities.mailstore import (
    ALERT_STALENESS_KEY,
    CATEGORY_ALERT,
    LEAD_FRESHNESS_DAYS,
    MailStore,
    alert_staleness_days,
)
from utilities.store import JobStore


def make_stores():
    store = JobStore(":memory:")
    return store, MailStore(store.conn)


def add_alert(mail, message_id, days_ago):
    """
    Summary:
        Insert one unhandled alert received a given number of days ago.

    Parameters:
        mail (MailStore): The store to write to.
        message_id (str): Gmail id.
        days_ago (float): How long ago the alert arrived.

    Note:
        `received_ts` is written directly rather than through a Date header, so
        the age under test is exact rather than dependent on header parsing.
    """
    mail.upsert_message({"id": message_id, "sender": "jobs@board.test",
                         "subject": "5 new jobs", "date": ""})
    mail.store_body(message_id, "Software Engineer at Acme.")
    mail.record_category(message_id, CATEGORY_ALERT, 0.9, "digest")
    mail.conn.execute(
        "UPDATE messages SET received_ts = ? WHERE gmail_message_id = ?",
        (int(time.time() - days_ago * 86400), message_id),
    )
    mail.commit()


async def immediate(fn, *args):
    return fn(*args)


class Counting:
    """A parser client that records how often it was asked."""

    def __init__(self):
        self.calls = 0

    def complete_json(self, *args, **kwargs):
        self.calls += 1
        return {"postings": []}


class TheCutoffIsConfigurableTests(unittest.TestCase):
    def setUp(self):
        self.store, self.mail = make_stores()

    def tearDown(self):
        self.store.close()

    def test_it_defaults_to_the_lead_freshness_window(self):
        # Not an arbitrary default: that is the age at which extraction stops
        # being able to produce a lead that survives the same cycle.
        self.assertEqual(alert_staleness_days(self.store), LEAD_FRESHNESS_DAYS)

    def test_a_saved_value_is_used(self):
        self.store.save_profile_value(ALERT_STALENESS_KEY, "3")
        self.assertEqual(alert_staleness_days(self.store), 3)

    def test_a_nonsense_value_degrades_to_the_default(self):
        self.store.save_profile_value(ALERT_STALENESS_KEY, "soon")
        self.assertEqual(alert_staleness_days(self.store), LEAD_FRESHNESS_DAYS)

    def test_the_cutoff_can_never_be_zero_days(self):
        # Zero would retire every alert the moment it was classified.
        self.store.save_profile_value(ALERT_STALENESS_KEY, "0")
        self.assertGreaterEqual(alert_staleness_days(self.store), 1)


class ThePreviewMatchesWhatHappensTests(unittest.TestCase):
    """The number shown before saving must be the number actually retired."""

    def setUp(self):
        self.store, self.mail = make_stores()
        for index, age in enumerate((1, 5, 20, 40, 60)):
            add_alert(self.mail, f"alert-{index}", age)

    def tearDown(self):
        self.store.close()

    def test_the_preview_counts_without_changing_anything(self):
        self.assertEqual(self.mail.stale_alert_count(14), 3)
        self.assertEqual(
            self.mail.queue_depths()["awaiting_handling_job_alert"], 5)

    def test_the_preview_moves_with_the_cutoff(self):
        self.assertEqual(self.mail.stale_alert_count(3), 4)
        self.assertEqual(self.mail.stale_alert_count(50), 1)
        self.assertEqual(self.mail.stale_alert_count(365), 0)

    def test_retiring_takes_exactly_what_the_preview_promised(self):
        expected = self.mail.stale_alert_count(14)
        self.assertEqual(self.mail.retire_stale_alerts(14), expected)
        self.assertEqual(
            self.mail.queue_depths()["awaiting_handling_job_alert"],
            5 - expected,
        )

    def test_retiring_is_idempotent(self):
        self.mail.retire_stale_alerts(14)
        self.assertEqual(self.mail.retire_stale_alerts(14), 0)


class UndatedAlertsAreKeptTests(unittest.TestCase):
    def setUp(self):
        self.store, self.mail = make_stores()

    def tearDown(self):
        self.store.close()

    def test_an_alert_with_no_received_time_is_treated_as_recent(self):
        # The opposite choice from `purge_stale_leads`, deliberately. Retiring
        # is throwing a posting away unseen, so an undated alert is extracted.
        # Paying for one advert is the cheap mistake; silently discarding an
        # interview is not.
        self.mail.upsert_message({"id": "undated", "sender": "jobs@board.test",
                                  "subject": "Jobs", "date": ""})
        self.mail.store_body("undated", "Engineer at Acme.")
        self.mail.record_category("undated", CATEGORY_ALERT, 0.9, "digest")
        self.mail.conn.execute(
            "UPDATE messages SET received_ts = NULL "
            "WHERE gmail_message_id = 'undated'")
        self.mail.commit()

        self.assertEqual(self.mail.stale_alert_count(14), 0)
        self.assertEqual(self.mail.retire_stale_alerts(14), 0)


class TheHandlerSpendsNothingOnStaleAlertsTests(unittest.TestCase):
    def setUp(self):
        self.store, self.mail = make_stores()

    def tearDown(self):
        self.store.close()

    def handler(self, client, days=14):
        return AlertHandler(self.store, self.mail, client, executor=immediate,
                            staleness_days=days)

    def test_a_stale_alert_is_never_sent_to_a_model(self):
        add_alert(self.mail, "old", 60)
        client = Counting()

        asyncio.run(self.handler(client).run(limit=10))

        self.assertEqual(client.calls, 0)
        self.assertEqual(
            self.mail.queue_depths()["awaiting_handling_job_alert"], 0)

    def test_a_fresh_alert_is_still_extracted(self):
        add_alert(self.mail, "fresh", 2)
        client = Counting()

        asyncio.run(self.handler(client).run(limit=10))

        self.assertEqual(client.calls, 1)

    def test_the_handler_reports_what_it_retired(self):
        add_alert(self.mail, "old-1", 30)
        add_alert(self.mail, "old-2", 40)
        add_alert(self.mail, "fresh", 1)

        handler = self.handler(Counting())
        asyncio.run(handler.run(limit=10))
        self.assertEqual(handler.retired, 2)


class NothingWaitsForEverTests(unittest.TestCase):
    """Oldest-first, now that the tail is no longer seven weeks of adverts."""

    def setUp(self):
        self.store, self.mail = make_stores()

    def tearDown(self):
        self.store.close()

    def test_the_oldest_alert_inside_the_window_is_taken_first(self):
        add_alert(self.mail, "newer", 1)
        add_alert(self.mail, "older", 10)

        seen = []

        class Recording:
            def complete_json(self, messages, *args, **kwargs):
                seen.append(len(seen))
                return {"postings": []}

        handler = self.handler_for(Recording())
        pending = [row["gmail_message_id"] for row in handler._pending(10)]
        self.assertEqual(pending, ["older", "newer"])

    def handler_for(self, client):
        return AlertHandler(self.store, self.mail, client, executor=immediate,
                            staleness_days=14)

    def test_a_message_that_always_fails_does_not_hold_up_the_queue(self):
        # The invariant AGENTS.md states and this handler did not obey: a
        # failure specific to one message may never end the pass.
        #
        # The failure is injected at `handle` rather than at the model client,
        # because `parse_alert` already catches anything the parser raises. The
        # gap this closes is everything *after* parsing - identity resolution,
        # the link writes, a posting with a shape the store rejects - which
        # used to propagate out of `dispatch` and take `prepare` with it.
        add_alert(self.mail, "poison", 5)
        add_alert(self.mail, "good", 2)

        handler = self.handler_for(Counting())
        seen = []

        async def handle(message):
            seen.append(message["gmail_message_id"])
            if message["gmail_message_id"] == "poison":
                raise RuntimeError("could not link this posting")
            return (1, 0, 1)

        handler.handle = handle
        created, _skipped, _linked = asyncio.run(handler.run(limit=10))

        self.assertEqual(seen, ["poison", "good"],
                         "the second alert must still be reached")
        self.assertEqual(created, 1)
        self.assertEqual(handler.failed, 1)
        # The failed one is marked handled, not left pending: it was tried,
        # and a message that fails identically every cycle is what
        # `handled_at` exists to stop being re-charged for. (`good` is left
        # unmarked only because the stub above replaces the real `handle`,
        # which is what does the marking on the success path.)
        self.assertIsNotNone(
            self.store.conn.execute(
                "SELECT handled_at FROM messages "
                "WHERE gmail_message_id = 'poison'"
            ).fetchone()["handled_at"]
        )


if __name__ == "__main__":
    unittest.main()
