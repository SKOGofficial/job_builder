"""The rough filter.

Two directions matter, and they are not equally dangerous.

Dropping something we should have kept is invisible: the user never learns
about the interview invite they were not shown. Keeping something we could have
dropped costs a fraction of a cent. So the "must pass" cases below are the
load-bearing ones, and the filter is expected to be permissive.
"""

import unittest

from pipeline.rough_filter import (
    DROP_BULK,
    DROP_DENYLISTED,
    DROP_PERSONAL,
    DROP_SOCIAL,
    RoughFilter,
    domain_matches,
    load_rules,
)
from utilities.mailstore import VERDICT_PASSED


def header(sender, subject="", labels=(), snippet="", unsubscribe=""):
    return {
        "id": "m1",
        "sender": sender,
        "subject": subject,
        "snippet": snippet,
        "labels": list(labels),
        "list_unsubscribe": unsubscribe,
    }


class TestRuleData(unittest.TestCase):
    def test_shipped_rules_load(self):
        rules = load_rules()
        self.assertIn("linkedin.com", rules["job_board_domains"])
        self.assertIn("CATEGORY_SOCIAL", rules["drop_labels"])
        self.assertTrue(rules["job_keywords"])

    def test_promotions_and_updates_are_never_dropped(self):
        # The single most damaging misconfiguration: LinkedIn and Indeed job
        # alerts land in Promotions and Updates. Dropping those labels would
        # remove the main source of leads and look like the pipeline working.
        rules = load_rules()
        self.assertNotIn("CATEGORY_PROMOTIONS", rules["drop_labels"])
        self.assertNotIn("CATEGORY_UPDATES", rules["drop_labels"])

    def test_missing_file_falls_back_to_defaults(self):
        rules = load_rules("/nonexistent/filter_rules.json")
        self.assertTrue(rules["job_keywords"])
        self.assertIn("linkedin.com", rules["job_board_domains"])


class TestDomainMatching(unittest.TestCase):
    def test_exact(self):
        self.assertTrue(domain_matches("linkedin.com", ["linkedin.com"]))

    def test_subdomain(self):
        # Boards send from subdomains far more often than the apex.
        self.assertTrue(domain_matches("e.indeed.com", ["indeed.com"]))
        self.assertTrue(domain_matches("mail.greenhouse.io", ["greenhouse.io"]))

    def test_lookalike_is_not_a_match(self):
        self.assertFalse(domain_matches("notlinkedin.com", ["linkedin.com"]))
        self.assertFalse(domain_matches("linkedin.com.evil.co", ["linkedin.com"]))

    def test_empty(self):
        self.assertFalse(domain_matches("", ["linkedin.com"]))
        self.assertFalse(domain_matches("linkedin.com", []))


class TestMustPass(unittest.TestCase):
    """Cases where dropping would lose something the user needs."""

    def setUp(self):
        self.filter = RoughFilter(denied_domains={"netflix.com"},
                                  known_company_slugs={"acme", "stripe"})

    def test_recruiter_from_gmail_with_keyword(self):
        # The case a precision-tuned prefilter gets wrong. Independent
        # recruiters and small companies really do use free mail.
        verdict = self.filter.verdict(header(
            "Jane Recruiter <jane.recruiter@gmail.com>",
            subject="Following up on your application",
        ))
        self.assertEqual(verdict, VERDICT_PASSED)

    def test_keyword_only_in_snippet(self):
        # Subject says nothing; the first line of the body does.
        verdict = self.filter.verdict(header(
            "someone@gmail.com",
            subject="Following up",
            snippet="Hi - about the position you applied for last week",
        ))
        self.assertEqual(verdict, VERDICT_PASSED)

    def test_job_board_in_promotions(self):
        verdict = self.filter.verdict(header(
            "jobs-noreply@linkedin.com",
            subject="5 new jobs for you",
            labels=["CATEGORY_PROMOTIONS"],
            unsubscribe="<https://linkedin.com/unsub>",
        ))
        self.assertEqual(verdict, VERDICT_PASSED)

    def test_job_board_in_updates(self):
        verdict = self.filter.verdict(header(
            "alert@indeed.com",
            subject="New postings",
            labels=["CATEGORY_UPDATES"],
        ))
        self.assertEqual(verdict, VERDICT_PASSED)

    def test_known_company_beats_bulk_rule(self):
        # A company already in the applications list always passes, even with
        # no keyword and an unsubscribe header.
        verdict = self.filter.verdict(header(
            "careers@stripe.com",
            subject="An update for you",
            unsubscribe="<https://stripe.com/unsub>",
        ))
        self.assertEqual(verdict, VERDICT_PASSED)

    def test_unknown_company_with_no_signals_still_passes(self):
        # This is the permissive default: we do not know what it is, so the
        # model decides, not the filter.
        verdict = self.filter.verdict(header(
            "hello@someunknownstartup.io",
            subject="Quick question",
        ))
        self.assertEqual(verdict, VERDICT_PASSED)

    def test_ats_subdomain_passes(self):
        verdict = self.filter.verdict(header(
            "no-reply@mail.greenhouse.io",
            subject="Thanks for applying",
        ))
        self.assertEqual(verdict, VERDICT_PASSED)


class TestMustDrop(unittest.TestCase):
    def setUp(self):
        self.filter = RoughFilter(denied_domains={"netflix.com", "spotify.com"},
                                  known_company_slugs={"acme"})

    def test_denylisted_domain(self):
        verdict = self.filter.verdict(header(
            "info@netflix.com", subject="New arrivals this week"))
        self.assertEqual(verdict, DROP_DENYLISTED)

    def test_social_label(self):
        verdict = self.filter.verdict(header(
            "notify@somewhere.com", subject="You have 3 notifications",
            labels=["CATEGORY_SOCIAL"]))
        self.assertEqual(verdict, DROP_SOCIAL)

    def test_personal_mail_without_keyword(self):
        verdict = self.filter.verdict(header(
            "friend@gmail.com", subject="dinner on saturday?"))
        self.assertEqual(verdict, DROP_PERSONAL)

    def test_bulk_mail_without_keyword(self):
        verdict = self.filter.verdict(header(
            "newsletter@somesite.com",
            subject="Your weekly digest",
            unsubscribe="<https://somesite.com/unsub>"))
        self.assertEqual(verdict, DROP_BULK)

    def test_denylist_beats_other_rules(self):
        # Attribution should name the user's own explicit choice, so the
        # stats say something actionable.
        verdict = self.filter.verdict(header(
            "info@spotify.com", subject="x", labels=["CATEGORY_SOCIAL"]))
        self.assertEqual(verdict, DROP_DENYLISTED)

    def test_denylist_does_not_override_a_job_board(self):
        # If a board somehow lands on the denylist, treating it as a board is
        # still right - the user almost certainly denied a marketing subdomain.
        blocked = RoughFilter(denied_domains={"linkedin.com"})
        verdict = blocked.verdict(header(
            "jobs-noreply@linkedin.com", subject="5 new jobs"))
        self.assertEqual(verdict, VERDICT_PASSED)


class TestExplanations(unittest.TestCase):
    def test_every_drop_reason_has_prose(self):
        f = RoughFilter()
        for verdict in (DROP_DENYLISTED, DROP_SOCIAL, DROP_PERSONAL, DROP_BULK):
            self.assertTrue(f.explain(verdict))
            self.assertNotEqual(f.explain(verdict), "Passed to the classifier")

    def test_passed_is_explained_too(self):
        self.assertTrue(RoughFilter().explain(VERDICT_PASSED))


if __name__ == "__main__":
    unittest.main()


class TestSeedDenylist(unittest.TestCase):
    """The shipped starter list must actually reach the database.

    It did not, until a real run showed the filter passing 488 of 500
    messages: the denylist rule had nothing to match and the bulk-mail rule
    read a hardcoded empty header.
    """

    def setUp(self):
        from utilities.mailstore import MailStore
        from utilities.store import JobStore

        self.store = JobStore(":memory:")
        self.mail = MailStore(self.store.conn)
        self.addCleanup(self.store.close)

    def test_seeding_populates_the_denylist(self):
        from pipeline.rough_filter import seed_denylist

        self.assertEqual(self.mail.denied_domains(), set())
        added = seed_denylist(self.mail)
        self.assertGreater(added, 0)
        self.assertIn("netflix.com", self.mail.denied_domains())

    def test_addresses_are_reduced_to_domains(self):
        from pipeline.rough_filter import seed_denylist

        seed_denylist(self.mail)
        # filter_rules.json lists "noreply@github.com"; only the domain matches.
        self.assertIn("github.com", self.mail.denied_domains())

    def test_seeding_runs_only_once(self):
        from pipeline.rough_filter import seed_denylist

        seed_denylist(self.mail)
        self.assertEqual(seed_denylist(self.mail), 0)

    def test_a_removed_domain_does_not_come_back(self):
        from pipeline.rough_filter import seed_denylist

        seed_denylist(self.mail)
        self.mail.allow_sender("netflix.com")
        seed_denylist(self.mail)
        self.assertNotIn("netflix.com", self.mail.denied_domains())


class TestListUnsubscribeIsPersisted(unittest.TestCase):
    def test_header_survives_a_round_trip(self):
        # The bulk-mail rule runs in a separate pass from the fetch, so the
        # header has to be stored or the rule is dead code.
        from utilities.mailstore import MailStore
        from utilities.store import JobStore

        store = JobStore(":memory:")
        self.addCleanup(store.close)
        mail = MailStore(store.conn)
        mail.upsert_message({
            "id": "m1", "sender": "news@example.com", "subject": "Digest",
            "date": "", "labels": [], "snippet": "",
            "list_unsubscribe": "<https://example.com/unsub>",
        })
        mail.commit()
        self.assertIn("unsub", mail.message("m1")["list_unsubscribe"])
