"""Message-to-job resolution.

The critical test in this file is `test_ambiguous_company_refuses_to_guess`.
Everything else is a convenience; that one is a correctness guarantee. Two open
applications at one company and an update email naming neither must produce no
link at all, because attaching a rejection to the wrong role marks the wrong
job dead and nothing surfaces the error.
"""

import unittest

from pipeline.resolver import (
    RESOLVED_BOARD_ID,
    RESOLVED_DOMAIN_ONLY,
    RESOLVED_DOMAIN_TITLE,
    RESOLVED_IDENTITY,
    JobResolver,
    title_similarity,
)
from utilities.identity import identity_key
from utilities.mailstore import CATEGORY_UPDATE, MailStore
from utilities.store import JobStore


def make_stores():
    store = JobStore(":memory:")
    return store, MailStore(store.conn)


def add_job(store, title, company, location=None, status="Applied", url=None):
    return store.create_job({
        "posting_url": url or "",
        "position_title": title,
        "company": company,
        "location": location or "",
        "job_type": "Full time",
        "status": status,
        "application_date": "2026-01-15",
    })


class TestTitleSimilarity(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(title_similarity("Software Engineer", "Software Engineer"), 1.0)

    def test_spelling_variants_score_high(self):
        self.assertGreaterEqual(
            title_similarity("Sr. Software Engineer", "Senior Software Engineer"), 0.9)

    def test_different_disciplines_score_low(self):
        # Shares "engineer" but must not clear the threshold.
        self.assertLess(title_similarity("Backend Engineer", "Frontend Engineer"), 0.6)

    def test_empty(self):
        self.assertEqual(title_similarity("", "Engineer"), 0.0)
        self.assertEqual(title_similarity("Engineer", ""), 0.0)


class TestBoardReference(unittest.TestCase):
    def test_board_id_resolves_exactly(self):
        store, mail = make_stores()
        job_id = add_job(store, "Backend Engineer", "Stripe", "Remote",
                         url="https://linkedin.com/jobs/view/12345")
        store.add_job_source(job_id, "https://linkedin.com/jobs/view/12345",
                             "linkedin", "12345")
        resolver = JobResolver(store, mail)

        result = resolver.resolve(
            {"sender": "anything@nowhere.com", "subject": ""},
            {"board": "linkedin", "board_job_id": "12345"},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.resolved_by, RESOLVED_BOARD_ID)
        self.assertEqual(result.confidence, 1.0)

    def test_unknown_board_id_falls_through(self):
        store, mail = make_stores()
        add_job(store, "Backend Engineer", "Stripe")
        resolver = JobResolver(store, mail)
        result = resolver.resolve(
            {"sender": "x@unrelated.com", "subject": ""},
            {"board": "linkedin", "board_job_id": "does-not-exist"},
        )
        self.assertFalse(result.resolved)


class TestIdentityMatch(unittest.TestCase):
    def test_exact_identity_resolves(self):
        store, mail = make_stores()
        add_job(store, "Backend Engineer", "Stripe", "Remote")
        resolver = JobResolver(store, mail)

        result = resolver.resolve(
            {"sender": "noreply@stripe.com", "subject": "Update"},
            {"title": "Backend Engineer", "company": "Stripe", "location": "Remote"},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.resolved_by, RESOLVED_IDENTITY)

    def test_spelling_variants_still_resolve(self):
        store, mail = make_stores()
        add_job(store, "Senior Backend Engineer", "Stripe Inc.", "Remote")
        resolver = JobResolver(store, mail)
        result = resolver.resolve(
            {"sender": "noreply@stripe.com", "subject": ""},
            {"title": "Sr. Backend Engineer", "company": "Stripe", "location": "Fully Remote"},
        )
        self.assertTrue(result.resolved)

    def test_falls_back_to_bare_key_for_legacy_rows(self):
        # A job stored with no location must be reachable from an email that
        # does mention one.
        store, mail = make_stores()
        add_job(store, "Backend Engineer", "Stripe", location=None)
        resolver = JobResolver(store, mail)
        result = resolver.resolve(
            {"sender": "noreply@stripe.com", "subject": ""},
            {"title": "Backend Engineer", "company": "Stripe", "location": "Remote"},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.identity_key,
                         identity_key("Backend Engineer", "Stripe", None))

    def test_lead_identity_resolves_too(self):
        store, mail = make_stores()
        key = identity_key("Backend Engineer", "Stripe", "Remote")
        mail.upsert_lead({"identity_key": key, "title": "Backend Engineer",
                          "company": "Stripe", "location": "Remote"})
        mail.commit()
        resolver = JobResolver(store, mail)
        result = resolver.resolve(
            {"sender": "noreply@stripe.com", "subject": ""},
            {"title": "Backend Engineer", "company": "Stripe", "location": "Remote"},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.identity_key, key)


class TestDomainTiers(unittest.TestCase):
    def test_sole_open_application_links_at_low_confidence(self):
        store, mail = make_stores()
        add_job(store, "Backend Engineer", "Stripe")
        resolver = JobResolver(store, mail)

        result = resolver.resolve(
            {"sender": "careers@stripe.com", "subject": "An update on your application"},
            {},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.resolved_by, RESOLVED_DOMAIN_ONLY)
        self.assertLessEqual(result.confidence, 0.5)

    def test_title_match_picks_one_of_several(self):
        store, mail = make_stores()
        add_job(store, "Backend Engineer", "Stripe")
        add_job(store, "Product Designer", "Stripe")
        resolver = JobResolver(store, mail)

        result = resolver.resolve(
            {"sender": "careers@stripe.com",
             "subject": "Your Backend Engineer application"},
            {},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.resolved_by, RESOLVED_DOMAIN_TITLE)
        self.assertLess(result.confidence, 0.95, "domain+title is weaker evidence")

    def test_ambiguous_company_refuses_to_guess(self):
        # THE case. Two open applications at one company, an email that names
        # neither. Linking either one would be a coin flip that silently marks
        # the wrong role dead.
        store, mail = make_stores()
        add_job(store, "Backend Engineer", "Stripe")
        add_job(store, "Product Designer", "Stripe")
        resolver = JobResolver(store, mail)

        result = resolver.resolve(
            {"sender": "careers@stripe.com", "subject": "An update on your application"},
            {},
        )
        self.assertFalse(result.resolved, "must not guess between two candidates")
        self.assertTrue(result.ambiguous)
        self.assertEqual(len(result.candidates), 2)
        self.assertIn("distinguishes", result.reason)

    def test_several_similar_titles_also_refuse(self):
        store, mail = make_stores()
        add_job(store, "Software Engineer", "Stripe")
        add_job(store, "Software Engineer II", "Stripe")
        resolver = JobResolver(store, mail)
        result = resolver.resolve(
            {"sender": "careers@stripe.com", "subject": "Software Engineer"},
            {},
        )
        self.assertFalse(result.resolved)
        self.assertTrue(result.ambiguous)

    def test_free_mail_domain_resolves_nothing(self):
        store, mail = make_stores()
        add_job(store, "Backend Engineer", "Stripe")
        resolver = JobResolver(store, mail)
        result = resolver.resolve(
            {"sender": "recruiter@gmail.com", "subject": "An update"}, {})
        self.assertFalse(result.resolved)

    def test_unknown_domain_resolves_nothing(self):
        store, mail = make_stores()
        add_job(store, "Backend Engineer", "Stripe")
        resolver = JobResolver(store, mail)
        result = resolver.resolve(
            {"sender": "hello@someoneelse.com", "subject": "An update"}, {})
        self.assertFalse(result.resolved)
        self.assertFalse(result.ambiguous)


class TestLinking(unittest.TestCase):
    def test_link_writes_a_row(self):
        store, mail = make_stores()
        add_job(store, "Backend Engineer", "Stripe")
        mail.upsert_message({"id": "m1", "sender": "careers@stripe.com",
                             "subject": "Update", "date": ""})
        resolver = JobResolver(store, mail)
        result = resolver.resolve({"sender": "careers@stripe.com", "subject": "x"}, {})

        self.assertTrue(resolver.link("m1", result, CATEGORY_UPDATE))
        mail.commit()
        timeline = mail.messages_for_identity(result.identity_key)
        self.assertEqual(len(timeline), 1)

    def test_link_is_a_no_op_when_unresolved(self):
        store, mail = make_stores()
        add_job(store, "Backend Engineer", "Stripe")
        add_job(store, "Product Designer", "Stripe")
        mail.upsert_message({"id": "m1", "sender": "careers@stripe.com",
                             "subject": "Update", "date": ""})
        resolver = JobResolver(store, mail)
        result = resolver.resolve({"sender": "careers@stripe.com", "subject": "x"}, {})

        self.assertFalse(resolver.link("m1", result, CATEGORY_UPDATE))

    def test_an_ambiguous_message_writes_no_link_at_all(self):
        """Refusing has to mean refusing.

        Two open roles at one company and an email that names neither is the
        case the resolver exists to decline. Linking it anywhere would put a
        status update on a job it may not be about, so the check is that
        *nothing* was written - not that something plausible was.
        """
        store, mail = make_stores()
        add_job(store, "Backend Engineer", "Stripe")
        add_job(store, "Product Designer", "Stripe")
        mail.upsert_message({"id": "m1", "sender": "careers@stripe.com",
                             "subject": "An update", "date": ""})
        mail.record_category("m1", CATEGORY_UPDATE, 0.9, "status change")
        mail.commit()

        resolver = JobResolver(store, mail)
        result = resolver.resolve({"sender": "careers@stripe.com",
                                   "subject": "An update"}, {})
        linked = resolver.link("m1", result, CATEGORY_UPDATE)
        mail.commit()

        self.assertFalse(result.resolved)
        self.assertTrue(result.ambiguous)
        self.assertFalse(linked)
        self.assertEqual(mail.links_for_message("m1"), [])


if __name__ == "__main__":
    unittest.main()
