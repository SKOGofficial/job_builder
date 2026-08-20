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

from clients.gmail_client import (
    GmailHistoryExpired,
    GmailMessageGone,
    list_history,
)
from clients.llm_client import GroqRateLimited
from pipeline.parsers import indeed, linkedin, parse_alert, parser_for
from pipeline.parsers.base import split_company_location, strip_tags
from pipeline.router import MessageRouter, build_router_messages, parse_route
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

    def test_a_deleted_message_is_skipped_without_a_traceback(self):
        """Mail deleted between the history page and the fetch is routine.

        Spam auto-purges and people clear a morning of digests by hand, so this
        is an expected outcome rather than a failure, and it must not abort the
        pass or fill the log with tracebacks.
        """
        store, mail = make_mail()
        mail.set_cursor(CURSOR_HISTORY_ID, "1000")

        def flaky(message_id, creds):
            if message_id == "deleted":
                raise GmailMessageGone("gone")
            return header(message_id)

        with mock.patch("pipeline.sync.gmail_client") as gmail:
            gmail.list_history.return_value = (["deleted", "good"], "1100")
            gmail.get_message_headers.side_effect = flaky
            sync = MailboxSync(mail, executor=immediate,
                               credential_loader=lambda: "creds")
            stored = asyncio.run(sync.run())

        self.assertEqual(stored, 1)
        self.assertTrue(mail.has_message("good"))
        self.assertFalse(mail.has_message("deleted"))
        # The cursor still advances, so the deleted id is never asked for again.
        self.assertEqual(mail.get_cursor(CURSOR_HISTORY_ID), "1100")

    def test_a_capped_pass_holds_the_cursor(self):
        """Advancing past unfetched ids loses them permanently.

        The window is re-listed next pass, so holding the cursor is what makes
        the cap a pause rather than a deletion.
        """
        store, mail = make_mail()
        mail.set_cursor(CURSOR_HISTORY_ID, "1000")

        with mock.patch("pipeline.sync.gmail_client") as gmail:
            gmail.list_history.return_value = (["m1", "m2", "m3"], "1100")
            gmail.get_message_headers.side_effect = lambda mid, creds: header(mid)
            sync = MailboxSync(mail, executor=immediate,
                               credential_loader=lambda: "creds")
            stored = asyncio.run(sync.run(max_messages=2))

        self.assertEqual(stored, 2)
        self.assertEqual(mail.get_cursor(CURSOR_HISTORY_ID), "1000",
                         "cursor must not move past the message left behind")

    def test_the_next_pass_picks_up_the_remainder(self):
        store, mail = make_mail()
        mail.set_cursor(CURSOR_HISTORY_ID, "1000")

        with mock.patch("pipeline.sync.gmail_client") as gmail:
            gmail.list_history.return_value = (["m1", "m2", "m3"], "1100")
            gmail.get_message_headers.side_effect = lambda mid, creds: header(mid)
            sync = MailboxSync(mail, executor=immediate,
                               credential_loader=lambda: "creds")
            asyncio.run(sync.run(max_messages=2))
            stored = asyncio.run(sync.run(max_messages=2))

        self.assertEqual(stored, 1)
        for message_id in ("m1", "m2", "m3"):
            self.assertTrue(mail.has_message(message_id))
        # Everything covered, so the cursor is free to move.
        self.assertEqual(mail.get_cursor(CURSOR_HISTORY_ID), "1100")

    def test_an_uncapped_pass_advances_as_before(self):
        store, mail = make_mail()
        mail.set_cursor(CURSOR_HISTORY_ID, "1000")

        with mock.patch("pipeline.sync.gmail_client") as gmail:
            gmail.list_history.return_value = (["m1", "m2"], "1100")
            gmail.get_message_headers.side_effect = lambda mid, creds: header(mid)
            sync = MailboxSync(mail, executor=immediate,
                               credential_loader=lambda: "creds")
            asyncio.run(sync.run(max_messages=50))

        self.assertEqual(mail.get_cursor(CURSOR_HISTORY_ID), "1100")

    def test_deleted_ids_cannot_stall_a_held_cursor(self):
        """The trap in the obvious version of the fix.

        A deleted message is never stored, so it stays unseen for ever. Without
        remembering it, a capped batch would refill with the same dead ids
        every pass and never reach the live mail behind them.
        """
        store, mail = make_mail()
        mail.set_cursor(CURSOR_HISTORY_ID, "1000")

        def flaky(message_id, creds):
            if message_id in ("d1", "d2"):
                raise GmailMessageGone("gone")
            return header(message_id)

        with mock.patch("pipeline.sync.gmail_client") as gmail:
            gmail.list_history.return_value = (["d1", "d2", "live"], "1100")
            gmail.get_message_headers.side_effect = flaky
            sync = MailboxSync(mail, executor=immediate,
                               credential_loader=lambda: "creds")
            # Pass one: the cap is spent entirely on the two dead ids.
            asyncio.run(sync.run(max_messages=2))
            self.assertEqual(mail.get_cursor(CURSOR_HISTORY_ID), "1000")
            # Pass two: they are remembered, so the live one is reached.
            stored = asyncio.run(sync.run(max_messages=2))

        self.assertEqual(stored, 1)
        self.assertTrue(mail.has_message("live"))
        self.assertEqual(mail.get_cursor(CURSOR_HISTORY_ID), "1100")

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

    def test_a_deleted_message_leaves_the_queue(self):
        """The loop this closes: `messages_awaiting_body` selects on
        `body_text IS NULL` alone, so a row skipped on a fetch failure comes
        back every cycle for ever, spending an API call each time to be told
        again that the message is gone.
        """
        store, mail = make_mail()
        mail.upsert_message(header("m1"))
        mail.set_filter_verdict("m1", VERDICT_PASSED)
        mail.commit()

        with mock.patch("pipeline.sync.gmail_client") as gmail:
            gmail.get_message_body.side_effect = GmailMessageGone("gone")
            fetched = asyncio.run(BodyFetcher(
                mail, executor=immediate, credential_loader=lambda: "c").run())

        self.assertEqual(fetched, 0)
        self.assertEqual(mail.message("m1")["body_text"], "")
        self.assertEqual(len(mail.messages_awaiting_body()), 0)

    def test_an_unexpected_failure_is_still_retried(self):
        """Only a confirmed deletion is written off; anything else comes back."""
        store, mail = make_mail()
        mail.upsert_message(header("m1"))
        mail.set_filter_verdict("m1", VERDICT_PASSED)
        mail.commit()

        with mock.patch("pipeline.sync.gmail_client") as gmail:
            gmail.get_message_body.side_effect = RuntimeError("Gmail is down")
            asyncio.run(BodyFetcher(mail, executor=immediate,
                                    credential_loader=lambda: "c").run())

        self.assertIsNone(mail.message("m1")["body_text"])
        self.assertEqual(len(mail.messages_awaiting_body()), 1)


class TestHistoryDeletions(unittest.TestCase):
    """`list_history` has to subtract what it is told was deleted.

    Without this the caller is handed ids Gmail has already reported gone, and
    every one becomes a failed fetch.
    """

    def build(self, pages):
        """A fake Gmail service returning canned history pages."""
        calls = {"n": 0}

        class Execute:
            def execute(self_inner):
                page = pages[calls["n"]]
                calls["n"] += 1
                return page

        class History:
            def list(self_inner, **kwargs):
                return Execute()

        class Users:
            def history(self_inner):
                return History()

        service = mock.Mock()
        service.users.return_value = Users()
        return service

    def run_history(self, pages):
        with mock.patch("clients.gmail_client._service",
                        return_value=self.build(pages)):
            return list_history("1000", creds="c")

    def test_a_message_added_then_deleted_is_dropped(self):
        ids, cursor = self.run_history([{
            "history": [
                {"messagesAdded": [{"message": {"id": "kept"}}]},
                {"messagesAdded": [{"message": {"id": "purged"}}]},
                {"messagesDeleted": [{"message": {"id": "purged"}}]},
            ],
            "historyId": "1100",
        }])
        self.assertEqual(ids, ["kept"])
        self.assertEqual(cursor, "1100")

    def test_a_deletion_on_a_later_page_still_counts(self):
        ids, _ = self.run_history([
            {"history": [{"messagesAdded": [{"message": {"id": "purged"}}]},
                         {"messagesAdded": [{"message": {"id": "kept"}}]}],
             "historyId": "1050", "nextPageToken": "p2"},
            {"history": [{"messagesDeleted": [{"message": {"id": "purged"}}]}],
             "historyId": "1100"},
        ])
        self.assertEqual(ids, ["kept"])

    def test_duplicates_across_pages_are_collapsed(self):
        ids, _ = self.run_history([
            {"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
             "historyId": "1050", "nextPageToken": "p2"},
            {"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
             "historyId": "1100"},
        ])
        self.assertEqual(ids, ["m1"])

    def test_a_deletion_for_something_never_added_is_harmless(self):
        ids, _ = self.run_history([{
            "history": [{"messagesDeleted": [{"message": {"id": "old"}}]},
                        {"messagesAdded": [{"message": {"id": "new"}}]}],
            "historyId": "1100",
        }])
        self.assertEqual(ids, ["new"])


class TestRouterKeepsGoing(unittest.TestCase):
    """One unclassifiable message must not stop the queue behind it.

    This was real: a single June email whose payload no provider would take sat
    at the head of the queue and left 187 messages behind it unclassified,
    every cycle, for weeks. A rate limit still stops the pass - that one really
    does apply to everything behind it - but nothing else should.
    """

    def prepare(self, mail, count):
        for index in range(count):
            message_id = "m%d" % index
            mail.upsert_message(header(message_id, subject="s%d" % index))
            mail.set_filter_verdict(message_id, VERDICT_PASSED)
            mail.store_body(message_id, "body %d" % index)
        mail.commit()

    def test_a_failing_message_is_skipped_not_fatal(self):
        store, mail = make_mail()
        self.prepare(mail, 3)

        class Breaks:
            """Fails on the first message it is given, then behaves."""

            last_model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_json(self, messages, parser, fallback, max_tokens=200):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("Groq returned HTTP 413")
                return {"label": CATEGORY_IRRELEVANT, "confidence": 0.9,
                        "reason": "not job related"}

        client = Breaks()
        router = MessageRouter(mail, client_factory=lambda: client)
        asyncio.run(router.run(limit=10))

        self.assertEqual(client.calls, 3, "every message must be attempted")
        classified = [mail.message("m%d" % i)["category"] for i in range(3)]
        self.assertEqual(classified.count(None), 1, "only the bad one is left")
        self.assertEqual(classified.count(CATEGORY_IRRELEVANT), 2)

    def test_a_rate_limit_still_stops_the_pass(self):
        """The one failure that genuinely applies to every message behind it."""
        store, mail = make_mail()
        self.prepare(mail, 3)

        class Limited:
            last_model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_json(self, messages, parser, fallback, max_tokens=200):
                self.calls += 1
                raise GroqRateLimited("slow down", retry_after=30)

        client = Limited()
        router = MessageRouter(mail, client_factory=lambda: client)
        asyncio.run(router.run(limit=10))

        self.assertEqual(client.calls, 1, "a rate limit ends the pass")


class TestPrunedBodiesStayOut(unittest.TestCase):
    """`prune_bodies` sets `body_text` back to NULL on purpose.

    A fetch queue defined by that column would hand every pruned message
    straight back to the fetcher, to be downloaded and pruned again for ever -
    undoing the retention pass and paying Gmail for the privilege.
    """

    def test_a_pruned_message_is_not_queued_again(self):
        store, mail = make_mail()
        mail.upsert_message(header("m1"))
        mail.set_filter_verdict("m1", VERDICT_PASSED)
        mail.store_body("m1", "some body text")
        mail.record_category("m1", CATEGORY_IRRELEVANT, 0.9, "not job related")
        mail.commit()
        self.assertEqual(len(mail.messages_awaiting_body()), 0)

        cleared = mail.prune_bodies(older_than_days=0)

        self.assertEqual(cleared, 1)
        self.assertIsNone(mail.message("m1")["body_text"])
        self.assertEqual(len(mail.messages_awaiting_body()), 0,
                         "a pruned body must not look like an unfetched one")

    def test_a_genuinely_unfetched_message_is_still_queued(self):
        store, mail = make_mail()
        mail.upsert_message(header("m1"))
        mail.set_filter_verdict("m1", VERDICT_PASSED)
        mail.commit()
        self.assertEqual(len(mail.messages_awaiting_body()), 1)


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
