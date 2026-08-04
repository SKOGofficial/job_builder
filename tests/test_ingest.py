"""Mailbox sync, classification routing, and board parsing.

Gmail is faked at the module boundary - `pipeline.sync` calls
`clients.gmail_client` functions by name, so patching those covers the History
API, the bounded full sync, and the expired-cursor fallback without a network
or a Google account.

The behaviour most worth pinning down is the recovery path. Gmail keeps history
for about a week, so any downtime longer than that lands on the full sync, and
if that path is broken the poller silently stops seeing new mail.
"""

import asyncio
import unittest
from unittest import mock

from clients.gmail_client import GmailHistoryExpired
from pipeline.parsers import indeed, linkedin, parse_alert, parser_for
from pipeline.parsers.base import split_company_location, strip_tags
from pipeline.router import build_router_messages, parse_route
from pipeline.sync import CURSOR_HISTORY_ID, BodyFetcher, MailboxSync
from utilities.mailstore import (
    CATEGORY_ALERT,
    CATEGORY_IRRELEVANT,
    VERDICT_PASSED,
    MailStore,
)
from utilities.store import JobStore


def make_mail():
    store = JobStore(":memory:")
    return store, MailStore(store.conn)


def header(message_id, sender="a@b.com", subject="s"):
    return {"id": message_id, "thread_id": "t", "sender": sender,
            "subject": subject, "date": "Mon, 02 Feb 2026 09:00:00 -0800",
            "labels": [], "snippet": "", "list_unsubscribe": ""}


async def immediate(func, *args):
    """Executor stand-in that runs inline, so tests stay synchronous."""
    return func(*args)


class TestIncrementalSync(unittest.TestCase):
    def test_history_path_stores_new_messages(self):
        store, mail = make_mail()
        mail.set_cursor(CURSOR_HISTORY_ID, "1000")

        with mock.patch("pipeline.sync.gmail_client") as gmail:
            gmail.list_history.return_value = (["m1", "m2"], "1100")
            gmail.get_message_headers.side_effect = lambda mid, creds: header(mid)
            sync = MailboxSync(mail, executor=immediate,
                               credential_loader=lambda: "creds")
            stored = asyncio.run(sync.run())

        self.assertEqual(stored, 2)
        self.assertEqual(mail.get_cursor(CURSOR_HISTORY_ID), "1100")
        self.assertTrue(mail.has_message("m1"))

    def test_already_seen_messages_are_not_refetched(self):
        store, mail = make_mail()
        mail.set_cursor(CURSOR_HISTORY_ID, "1000")
        mail.upsert_message(header("m1"))
        mail.commit()

        with mock.patch("pipeline.sync.gmail_client") as gmail:
            gmail.list_history.return_value = (["m1", "m2"], "1100")
            gmail.get_message_headers.side_effect = lambda mid, creds: header(mid)
            sync = MailboxSync(mail, executor=immediate,
                               credential_loader=lambda: "creds")
            stored = asyncio.run(sync.run())

        self.assertEqual(stored, 1)
        fetched = [call.args[0] for call in gmail.get_message_headers.call_args_list]
        self.assertEqual(fetched, ["m2"])

    def test_expired_cursor_falls_back_to_full_sync(self):
        # Any downtime longer than Gmail's ~week of history lands here.
        store, mail = make_mail()
        mail.set_cursor(CURSOR_HISTORY_ID, "stale")

        with mock.patch("pipeline.sync.gmail_client") as gmail:
            gmail.GmailHistoryExpired = GmailHistoryExpired
            gmail.list_history.side_effect = GmailHistoryExpired("too old")
            gmail.get_profile.return_value = {"historyId": "9000"}
            gmail.iter_message_ids.return_value = ["m1"]
            gmail.get_message_headers.side_effect = lambda mid, creds: header(mid)
            sync = MailboxSync(mail, executor=immediate,
                               credential_loader=lambda: "creds")
            stored = asyncio.run(sync.run())

        self.assertEqual(stored, 1)
        self.assertEqual(mail.get_cursor(CURSOR_HISTORY_ID), "9000")
        gmail.iter_message_ids.assert_called_once()

    def test_full_sync_reads_history_id_before_listing(self):
        # Taking it afterwards would skip anything that arrived mid-walk - a
        # race that loses mail exactly when the mailbox is busiest.
        store, mail = make_mail()
        order = []

        with mock.patch("pipeline.sync.gmail_client") as gmail:
            gmail.get_profile.side_effect = lambda creds: (
                order.append("profile") or {"historyId": "5000"})
            gmail.iter_message_ids.side_effect = lambda q, creds, mx: (
                order.append("list") or [])
            sync = MailboxSync(mail, executor=immediate,
                               credential_loader=lambda: "creds")
            asyncio.run(sync.run())

        self.assertEqual(order, ["profile", "list"])

    def test_unreadable_message_does_not_abort_the_pass(self):
        store, mail = make_mail()
        mail.set_cursor(CURSOR_HISTORY_ID, "1000")

        def flaky(message_id, creds):
            if message_id == "bad":
                raise RuntimeError("410 gone")
            return header(message_id)

        with mock.patch("pipeline.sync.gmail_client") as gmail:
            gmail.list_history.return_value = (["bad", "good"], "1100")
            gmail.get_message_headers.side_effect = flaky
            sync = MailboxSync(mail, executor=immediate,
                               credential_loader=lambda: "creds")
            stored = asyncio.run(sync.run())

        self.assertEqual(stored, 1)
        self.assertTrue(mail.has_message("good"))
        self.assertFalse(mail.has_message("bad"), "retried on the next run")

    def test_no_credentials_is_reported_not_raised(self):
        store, mail = make_mail()

        def boom():
            raise RuntimeError("not connected")

        sync = MailboxSync(mail, executor=immediate, credential_loader=boom)
        self.assertEqual(asyncio.run(sync.run()), 0)
        self.assertIn("not connected", sync.last_error)


class TestBodyFetcher(unittest.TestCase):
    def test_fetches_only_passed_messages(self):
        store, mail = make_mail()
        mail.upsert_message(header("passed"))
        mail.upsert_message(header("dropped"))
        mail.set_filter_verdict("passed", VERDICT_PASSED)
        mail.set_filter_verdict("dropped", "dropped_social_or_forum")
        mail.commit()

        with mock.patch("pipeline.sync.gmail_client") as gmail:
            gmail.get_message_body.return_value = {"body": "hello", "snippet": "h"}
            fetched = asyncio.run(BodyFetcher(
                mail, executor=immediate,
                credential_loader=lambda: "creds").run())

        self.assertEqual(fetched, 1)
        self.assertEqual(mail.message("passed")["body_text"], "hello")
        self.assertIsNone(mail.message("dropped")["body_text"])

    def test_empty_body_is_stored_so_it_is_not_refetched_forever(self):
        store, mail = make_mail()
        mail.upsert_message(header("m1"))
        mail.set_filter_verdict("m1", VERDICT_PASSED)
        mail.commit()

        with mock.patch("pipeline.sync.gmail_client") as gmail:
            gmail.get_message_body.return_value = {"body": "", "snippet": ""}
            asyncio.run(BodyFetcher(mail, executor=immediate,
                                    credential_loader=lambda: "c").run())

        self.assertEqual(mail.message("m1")["body_text"], "")
        self.assertEqual(len(mail.messages_awaiting_body()), 0)


class TestRouterParsing(unittest.TestCase):
    def test_valid_label(self):
        result = parse_route('{"label": "job_alert", "confidence": 0.9, '
                             '"reason": "digest"}')
        self.assertEqual(result["label"], CATEGORY_ALERT)
        self.assertEqual(result["confidence"], 0.9)

    def test_unknown_label_becomes_irrelevant(self):
        # The inert fallback is what stops a crafted email steering itself
        # into the lead list.
        result = parse_route('{"label": "delete_everything", "confidence": 1}')
        self.assertEqual(result["label"], CATEGORY_IRRELEVANT)

    def test_malformed_json_becomes_irrelevant(self):
        self.assertEqual(parse_route("nonsense")["label"], CATEGORY_IRRELEVANT)
        self.assertEqual(parse_route("[]")["label"], CATEGORY_IRRELEVANT)

    def test_confidence_is_clamped(self):
        self.assertEqual(parse_route('{"label": "job_update", "confidence": 9}')
                         ["confidence"], 1.0)
        self.assertEqual(parse_route('{"label": "job_update", "confidence": -1}')
                         ["confidence"], 0.0)

    def test_prompt_fences_the_email(self):
        messages = build_router_messages(
            {"sender": "a@b.com", "subject": "s", "body_text": "hello"})
        user = messages[1]["content"]
        self.assertIn("<email>", user)
        self.assertIn("</email>", user)
        self.assertIn("untrusted", messages[0]["content"])


class TestParserHelpers(unittest.TestCase):
    def test_split_company_location(self):
        self.assertEqual(split_company_location("Stripe - San Francisco, CA"),
                         ("Stripe", "San Francisco, CA"))

    def test_split_handles_reversed_order(self):
        company, location = split_company_location("San Francisco, CA - Stripe")
        self.assertEqual(company, "Stripe")
        self.assertEqual(location, "San Francisco, CA")

    def test_split_with_no_separator(self):
        self.assertEqual(split_company_location("Stripe"), ("Stripe", None))

    def test_strip_tags(self):
        self.assertEqual(strip_tags("<p>Hi</p><script>x</script>"), "Hi")


class TestBoardDetection(unittest.TestCase):
    def test_linkedin_claims_its_own(self):
        message = {"sender": "jobs-noreply@linkedin.com", "body_text": ""}
        self.assertIs(parser_for(message), linkedin)

    def test_indeed_claims_its_own(self):
        message = {"sender": "alert@indeed.com", "body_text": ""}
        self.assertIs(parser_for(message), indeed)

    def test_unknown_board_has_no_parser(self):
        message = {"sender": "jobs@someboard.io", "body_text": ""}
        self.assertIsNone(parser_for(message))


class TestIndeedParsing(unittest.TestCase):
    ALERT = """
    <a href="https://www.indeed.com/rc/clk?jk=abc123def456&from=ja">
      Data Engineer</a> Acme Corp - Austin, TX
    <a href="https://www.indeed.com/preferences">Unsubscribe</a>
    """

    def test_extracts_job_key_and_canonical_url(self):
        postings = indeed.parse({"sender": "alert@indeed.com",
                                 "body_text": self.ALERT})
        self.assertEqual(len(postings), 1)
        posting = postings[0]
        self.assertEqual(posting.board_job_id, "abc123def456")
        self.assertEqual(posting.apply_url,
                         "https://www.indeed.com/viewjob?jk=abc123def456")
        self.assertEqual(posting.title, "Data Engineer")

    def test_ignores_chrome_links(self):
        postings = indeed.parse({"sender": "alert@indeed.com",
                                 "body_text": self.ALERT})
        self.assertTrue(all("preferences" not in (p.apply_url or "")
                            for p in postings))

    def test_job_key_extraction(self):
        self.assertEqual(
            indeed.job_key("https://www.indeed.com/viewjob?jk=xyz"), "xyz")
        self.assertIsNone(indeed.job_key("https://example.com/?jk=xyz"))
        self.assertIsNone(indeed.job_key("https://www.indeed.com/viewjob"))


class TestParseAlertFallback(unittest.TestCase):
    def test_no_parser_and_no_client_yields_nothing(self):
        # Degrades to empty rather than guessing.
        postings = parse_alert({"sender": "jobs@unknownboard.io",
                                "body_text": "<a href='x'>Some Job</a>"}, None)
        self.assertEqual(postings, [])

    def test_incomplete_posting_is_dropped_without_a_company(self):
        # A lead keyed on a missing company never matches its acknowledgement.
        postings = parse_alert(
            {"sender": "jobs-noreply@linkedin.com",
             "body_text": '<a href="https://www.linkedin.com/comm/jobs/view/1/">'
                          'Engineer</a>'},
            None)
        self.assertEqual(postings, [])


if __name__ == "__main__":
    unittest.main()
