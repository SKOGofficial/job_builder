"""Contacts, matching, the careers check, and the referral draft.

Three things here are worth testing hardest, because each one fails quietly:

- **The slug is the join.** A lead says "Stripe" and the user typed
  "Stripe, Inc.". If those stop reducing to the same token the page shows an
  empty list and looks like a feature nobody uses, rather than a broken one.
- **"New" has to mean new.** The badge is the entire reason to open the page in
  the morning. A count that never clears is noise; one that clears too eagerly
  hides the posting the whole feature exists to surface.
- **Openings without a link are dropped.** These postings come from a model
  reading the web, not from mail the user received. A hallucinated role costs a
  real favour from a real person.
"""

import asyncio
import json
import time
import unittest

from clients.research_client import build_openings_prompt, parse_openings
from pipeline.referral_email import (
    build_prompt,
    parse_draft,
    supporting_bullets,
)
from pipeline.referrals import (
    BOARD_CAREERS_CHECK,
    OpeningsChecker,
    is_new_for,
    leads_by_company,
    matches_for,
    new_match_count,
)
from utilities.identity import identity_key
from utilities.mailstore import (
    LEAD_APPLIED,
    LEAD_DISMISSED,
    LEAD_NEW,
    MailStore,
)
from utilities.store import JobStore

DAY = 86400


def make_store():
    """A fresh in-memory store and mail store sharing one connection."""
    store = JobStore(":memory:")
    return store, MailStore(store.conn)


def hours_ago(hours):
    return int(time.time()) - hours * 3600


def add_lead(mail, title, company, posted_ts=None, status=LEAD_NEW,
             location=None):
    """Create a lead directly, bypassing the alert parser."""
    key = identity_key(title, company, location)
    mail.upsert_lead({
        "identity_key": key,
        "title": title,
        "company": company,
        "location": location,
        "posted_ts": posted_ts,
        "status": status,
    })
    mail.commit()
    return key


class ContactStorageTests(unittest.TestCase):
    def setUp(self):
        self.store, self.mail = make_store()

    def tearDown(self):
        self.store.conn.close()

    def test_slug_is_derived_from_the_company(self):
        contact_id = self.mail.save_contact({
            "name": "Priya Raman", "company": "Stripe, Inc.",
        })
        self.assertEqual(self.mail.contact(contact_id)["company_slug"], "stripe")

    def test_editing_keeps_the_check_stamp(self):
        """An edit must not repopulate the morning list."""
        contact_id = self.mail.save_contact({"name": "Priya", "company": "Stripe"})
        self.mail.mark_contact_checked(contact_id, hours_ago(1))
        self.mail.save_contact({
            "id": contact_id, "name": "Priya Raman", "company": "Stripe",
        })
        contact = self.mail.contact(contact_id)
        self.assertEqual(contact["name"], "Priya Raman")
        self.assertIsNotNone(contact["last_checked_ts"])

    def test_a_company_that_reduces_to_nothing_is_refused(self):
        """An empty slug would match every lead with no company, not none."""
        with self.assertRaises(ValueError):
            self.mail.save_contact({"name": "Nobody", "company": "!!!"})

    def test_archived_contacts_are_out_of_the_default_list(self):
        contact_id = self.mail.save_contact({"name": "Tom", "company": "Datadog"})
        self.mail.set_contact_archived(contact_id)
        self.assertEqual(self.mail.list_contacts(), [])
        self.assertEqual(len(self.mail.list_contacts(include_archived=True)), 1)

    def test_redrafting_does_not_forget_that_an_email_was_sent(self):
        """The guard against messaging a real person twice."""
        contact_id = self.mail.save_contact({"name": "Priya", "company": "Stripe"})
        outreach_id = self.mail.record_outreach(contact_id, "k", "S", "B", "m1")
        self.mail.set_outreach_status(outreach_id, MailStore.OUTREACH_SENT)

        self.mail.record_outreach(contact_id, "k", "S2", "B2", "m2")

        row = self.mail.outreach_for_contact(contact_id)["k"]
        self.assertEqual(row["subject"], "S2")
        self.assertEqual(row["status"], MailStore.OUTREACH_SENT)
        self.assertTrue(row["sent_at"])

    def test_unsending_clears_the_stamp(self):
        contact_id = self.mail.save_contact({"name": "Priya", "company": "Stripe"})
        outreach_id = self.mail.record_outreach(contact_id, "k", "S", "B")
        self.mail.set_outreach_status(outreach_id, MailStore.OUTREACH_SENT)
        self.mail.set_outreach_status(outreach_id, MailStore.OUTREACH_DRAFTED)
        self.assertIsNone(self.mail.outreach_for_contact(contact_id)["k"]["sent_at"])

    def test_unknown_status_is_refused(self):
        contact_id = self.mail.save_contact({"name": "Priya", "company": "Stripe"})
        outreach_id = self.mail.record_outreach(contact_id, "k", "S", "B")
        with self.assertRaises(ValueError):
            self.mail.set_outreach_status(outreach_id, "posted")

    def test_deleting_a_contact_takes_their_drafts(self):
        contact_id = self.mail.save_contact({"name": "Priya", "company": "Stripe"})
        self.mail.record_outreach(contact_id, "k", "S", "B")
        self.assertEqual(self.mail.delete_contact(contact_id), 1)
        self.assertEqual(self.mail.outreach_for_contact(contact_id), {})


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.store, self.mail = make_store()
        self.contact_id = self.mail.save_contact({
            "name": "Priya", "company": "Stripe, Inc.",
        })

    def tearDown(self):
        self.store.conn.close()

    def test_differently_written_companies_still_match(self):
        add_lead(self.mail, "Backend Engineer", "Stripe", hours_ago(1))
        add_lead(self.mail, "Infra Engineer", "stripe inc.", hours_ago(2))
        entry = matches_for(self.mail)[0]
        self.assertEqual(len(entry["leads"]), 2)

    def test_leads_at_other_companies_are_not_matched(self):
        add_lead(self.mail, "Backend Engineer", "Datadog", hours_ago(1))
        self.assertEqual(matches_for(self.mail)[0]["leads"], [])

    def test_a_lead_with_no_company_matches_nobody(self):
        """It must not fall into a shared empty bucket every contact reads."""
        add_lead(self.mail, "Mystery Role", None, hours_ago(1))
        self.assertEqual(leads_by_company(self.mail), {})

    def test_dismissed_and_applied_leads_are_left_out(self):
        add_lead(self.mail, "Nope", "Stripe", hours_ago(1), status=LEAD_DISMISSED)
        add_lead(self.mail, "Done", "Stripe", hours_ago(1), status=LEAD_APPLIED)
        add_lead(self.mail, "Backend Engineer", "Stripe", hours_ago(1))
        entry = matches_for(self.mail)[0]
        self.assertEqual([lead["title"] for lead in entry["leads"]],
                         ["Backend Engineer"])

    def test_a_contact_with_nothing_open_is_still_listed(self):
        """"Datadog: nothing new" is the answer most mornings."""
        self.mail.save_contact({"name": "Tom", "company": "Datadog"})
        entries = matches_for(self.mail)
        self.assertEqual(len(entries), 2)
        self.assertEqual([e["new_count"] for e in entries], [0, 0])

    def test_everything_is_new_until_the_contact_is_checked(self):
        add_lead(self.mail, "Backend Engineer", "Stripe", hours_ago(50))
        self.assertEqual(new_match_count(self.mail), 1)

    def test_checking_clears_only_what_was_already_posted(self):
        add_lead(self.mail, "Old Role", "Stripe", hours_ago(48))
        self.mail.mark_contact_checked(self.contact_id, hours_ago(24))
        self.assertEqual(new_match_count(self.mail), 0)

        add_lead(self.mail, "Fresh Role", "Stripe", hours_ago(1))
        self.assertEqual(new_match_count(self.mail), 1)

    def test_an_undated_lead_falls_back_to_when_it_was_recorded(self):
        """A badge that cannot be cleared stops being read, and then hides everything.

        `posted_ts` is absent on leads written before it existed and on any
        path with no alert email to date them from. Keying only on it would
        make every one of those permanently new.
        """
        self.mail.mark_contact_checked(self.contact_id, hours_ago(1))
        add_lead(self.mail, "Undated Role", "Stripe", None)
        contact = self.mail.contact(self.contact_id)
        lead = self.mail.lead_by_identity(identity_key("Undated Role", "Stripe", None))

        # Recorded just now, after the check, so it is new.
        self.assertTrue(is_new_for(contact, lead))

        self.mail.mark_contact_checked(self.contact_id)
        self.assertFalse(is_new_for(self.mail.contact(self.contact_id), lead))

    def test_a_lead_with_no_date_at_all_counts_as_new(self):
        """Last resort. Hiding a posting is worse than showing one twice."""
        self.mail.mark_contact_checked(self.contact_id)
        contact = self.mail.contact(self.contact_id)
        self.assertTrue(is_new_for(contact, {"posted_ts": None, "created_at": None}))
        self.assertTrue(is_new_for(contact, {"posted_ts": None, "created_at": "not a date"}))

    def test_two_contacts_at_one_company_are_two_things_to_do(self):
        self.mail.save_contact({"name": "Sam", "company": "Stripe"})
        add_lead(self.mail, "Backend Engineer", "Stripe", hours_ago(1))
        self.assertEqual(new_match_count(self.mail), 2)

    def test_the_badge_is_cheap_when_nobody_is_tracked(self):
        self.mail.delete_contact(self.contact_id)
        add_lead(self.mail, "Backend Engineer", "Stripe", hours_ago(1))
        self.assertEqual(new_match_count(self.mail), 0)


class OpeningsParsingTests(unittest.TestCase):
    def payload(self, openings):
        return json.dumps({"openings": openings})

    def test_a_well_formed_reply_is_read(self):
        openings = parse_openings(self.payload([{
            "title": "Backend Engineer", "location": "Remote",
            "url": "https://stripe.com/jobs/1", "posted": "2026-08-18",
        }]))
        self.assertEqual(len(openings), 1)
        self.assertEqual(openings[0]["title"], "Backend Engineer")
        self.assertTrue(openings[0]["posted_ts"])

    def test_a_fenced_reply_is_read(self):
        """Grounded search cannot ask for a JSON response type - see the module."""
        raw = self.payload([{"title": "Engineer", "url": "https://x.com/1"}])
        self.assertEqual(len(parse_openings("Here:\n```json\n" + raw + "\n```")), 1)

    def test_prose_around_the_object_is_tolerated(self):
        raw = self.payload([{"title": "Engineer", "url": "https://x.com/1"}])
        self.assertEqual(len(parse_openings("I found one. " + raw + " Hope that helps.")), 1)

    def test_an_opening_with_no_link_is_dropped(self):
        openings = parse_openings(self.payload([
            {"title": "Real Role", "url": "https://x.com/1"},
            {"title": "Unverifiable Role"},
            {"title": "Bad Link", "url": "see careers page"},
            {"title": "", "url": "https://x.com/2"},
        ]))
        self.assertEqual([o["title"] for o in openings], ["Real Role"])

    def test_an_unusable_reply_is_an_empty_list(self):
        for reply in ("", None, "sorry, I could not find anything", "[1, 2]",
                      '{"openings": "none"}'):
            self.assertEqual(parse_openings(reply), [])

    def test_an_undated_posting_has_no_timestamp(self):
        openings = parse_openings(self.payload([
            {"title": "Engineer", "url": "https://x.com/1", "posted": "recently"},
        ]))
        self.assertIsNone(openings[0]["posted_ts"])

    def test_the_stored_careers_url_reaches_the_prompt(self):
        prompt = build_openings_prompt({
            "company": "Stripe", "careers_url": "https://stripe.com/jobs",
        })
        self.assertIn("https://stripe.com/jobs", prompt)
        self.assertNotIn("Careers page", build_openings_prompt({"company": "Stripe"}))


class StubOpeningsClient:
    """Stands in for a research task client. Never reaches the network."""

    def __init__(self, openings):
        self.openings = openings
        self.calls = 0

    def find_openings(self, contact):
        self.calls += 1
        return list(self.openings), 100, 200


class OpeningsCheckerTests(unittest.TestCase):
    def setUp(self):
        self.store, self.mail = make_store()
        self.contact_id = self.mail.save_contact({
            "name": "Priya", "company": "Stripe",
        })
        self.contact = dict(self.mail.contact(self.contact_id))
        self.opening = {"title": "Staff Engineer", "location": "NYC",
                        "url": "https://stripe.com/jobs/9", "posted_ts": None}

    def tearDown(self):
        self.store.conn.close()

    def check(self, client):
        return asyncio.run(
            OpeningsChecker(self.store, self.mail, client).check(self.contact)
        )

    def test_a_found_opening_becomes_a_lead(self):
        result = self.check(StubOpeningsClient([self.opening]))
        self.assertEqual(result["created"], 1)
        lead = self.mail.lead_by_identity(
            identity_key("Staff Engineer", "Stripe", "NYC")
        )
        self.assertEqual(lead["apply_url"], "https://stripe.com/jobs/9")
        self.assertEqual(lead["board"], BOARD_CAREERS_CHECK)

    def test_an_undated_opening_is_dated_from_the_check(self):
        """Otherwise it sorts to the bottom of a list ordered by posting date."""
        self.check(StubOpeningsClient([self.opening]))
        lead = self.mail.lead_by_identity(
            identity_key("Staff Engineer", "Stripe", "NYC")
        )
        self.assertTrue(lead["posted_ts"])

    def test_checking_twice_does_not_duplicate(self):
        self.check(StubOpeningsClient([self.opening]))
        result = self.check(StubOpeningsClient([self.opening]))
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["known"], 1)
        self.assertEqual(len(self.mail.list_leads(None)), 1)

    def test_a_role_already_applied_to_is_not_relisted(self):
        self.store.create_job({
            "position_title": "Staff Engineer", "company": "Stripe",
            "location": "NYC", "job_type": "Full-time", "status": "Applied",
            "application_date": "2026-08-01",
            "posting_url": "https://stripe.com/jobs/9",
        })
        result = self.check(StubOpeningsClient([self.opening]))
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["created"], 0)

    def test_the_contact_is_stamped_even_when_nothing_is_found(self):
        """The user has looked; the badge should say so."""
        self.check(StubOpeningsClient([]))
        self.assertTrue(self.mail.contact(self.contact_id)["last_checked_ts"])

    def test_no_provider_is_a_no_op_rather_than_an_error(self):
        result = self.check(None)
        self.assertEqual(result["found"], 0)
        self.assertIsNone(self.mail.contact(self.contact_id)["last_checked_ts"])


class ReferralDraftTests(unittest.TestCase):
    def setUp(self):
        self.contact = {"name": "Priya", "company": "Stripe",
                        "role": "Staff Engineer", "notes": "worked together at Acme"}
        self.lead = {"title": "Backend Engineer", "location": "Remote",
                     "apply_url": "https://stripe.com/jobs/1",
                     "tracking_url": None, "board": "linkedin",
                     "identity_key": "k"}

    def test_a_well_formed_reply_is_read(self):
        raw = json.dumps({"subject": "Backend Engineer at Stripe",
                          "body": "Hi Priya,\n\nStripe posted a role.\n\nThanks"})
        draft = parse_draft(raw)
        self.assertEqual(draft["subject"], "Backend Engineer at Stripe")
        self.assertIn("Stripe posted", draft["body"])

    def test_an_unusable_reply_is_an_empty_draft(self):
        for reply in ("", None, "I cannot help with that", "[1, 2]"):
            self.assertEqual(parse_draft(reply), {"subject": "", "body": ""})

    def test_a_runaway_body_is_trimmed(self):
        raw = json.dumps({"subject": "x", "body": " ".join(["word"] * 900)})
        self.assertLess(len(parse_draft(raw)["body"].split()), 400)

    def test_an_unrecorded_relationship_is_stated_as_such(self):
        """An absent field invites the model to invent a shared history."""
        prompt = build_prompt("", dict(self.contact, notes=None), self.lead, [])
        self.assertIn("not recorded", prompt)
        self.assertIn("Do not describe the relationship", prompt)

    def test_unmatched_experience_asks_for_a_shorter_email(self):
        prompt = build_prompt("", self.contact, self.lead, [])
        self.assertIn("do not claim any", prompt)

    def test_the_posting_and_the_relationship_reach_the_prompt(self):
        prompt = build_prompt("I build pipelines.", self.contact, self.lead, [])
        self.assertIn("https://stripe.com/jobs/1", prompt)
        self.assertIn("worked together at Acme", prompt)
        self.assertIn("I build pipelines.", prompt)

    def test_bullets_are_ranked_against_the_role(self):
        store, mail = make_store()
        try:
            for bullet, tags in (("Built an ingest pipeline in Python", "python, backend"),
                                 ("Designed a poster for the society", "design")):
                mail.add_experience({"kind": "work", "organisation": "Acme",
                                     "role": "Engineer", "bullet": bullet,
                                     "tags": tags})
            chosen = supporting_bullets(mail.list_experiences(), self.lead,
                                        {"posting_keywords": ["python", "backend"]})
            # Both share the "Engineer" of the role title, so both score. What
            # matters is which one an email would quote first.
            self.assertIn("ingest pipeline", chosen[0]["bullet"])
        finally:
            store.conn.close()


if __name__ == "__main__":
    unittest.main()
