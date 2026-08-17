"""Posting dates, ordering, and the two-week window on the to-apply list.

The whole feature rests on one distinction: `created_at` is when this pipeline
read the alert email, `posted_ts` is when the role was advertised. A backfill
collapses the first and preserves the second, which is why ordering and expiry
both key on `posted_ts` and neither can key on `created_at`.

Deletion is the part worth testing hardest. Purging an `applied` lead would
break the record of an application, and purging a `dismissed` one would let the
next alert recreate a role the user has already said no to.
"""

import time
import unittest

from utilities.mailstore import (
    LEAD_APPLIED,
    LEAD_DISMISSED,
    LEAD_FRESHNESS_DAYS,
    LEAD_NEW,
    LEAD_READY,
    MailStore,
)
from utilities.store import JobStore

DAY = 86400


def make_mail():
    return MailStore(JobStore(":memory:").conn)


def days_ago(days):
    return int(time.time()) - days * DAY


def add_lead(mail, key, title="Engineer", posted_ts=None, status=LEAD_NEW,
             score=None):
    """Create a lead directly, bypassing the alert parser."""
    mail.upsert_lead({
        "identity_key": key,
        "title": title,
        "company": "Acme",
        "posted_ts": posted_ts,
        "status": status,
    })
    if score is not None:
        lead = mail.lead_by_identity(key)
        mail.set_lead_relevance(lead["id"], score, "test")
    mail.commit()
    return mail.lead_by_identity(key)


class TestOrdering(unittest.TestCase):
    def test_newest_posting_comes_first(self):
        mail = make_mail()
        add_lead(mail, "old", "Old role", posted_ts=days_ago(10))
        add_lead(mail, "new", "New role", posted_ts=days_ago(1))
        add_lead(mail, "mid", "Mid role", posted_ts=days_ago(5))

        self.assertEqual([row["title"] for row in mail.list_leads()],
                         ["New role", "Mid role", "Old role"])

    def test_posting_date_outranks_relevance(self):
        """Applying early beats every other signal the list used to sort on.

        A role scored 95% but advertised a week ago is a worse use of the next
        ten minutes than a 60% match posted this morning, because the second is
        still open.
        """
        mail = make_mail()
        add_lead(mail, "stale-great", "Stale but relevant",
                 posted_ts=days_ago(7), score=0.95)
        add_lead(mail, "fresh-ok", "Fresh and adequate",
                 posted_ts=days_ago(0), score=0.6)

        self.assertEqual(mail.list_leads()[0]["title"], "Fresh and adequate")

    def test_relevance_breaks_ties_within_a_posting_date(self):
        mail = make_mail()
        same_day = days_ago(2)
        add_lead(mail, "weak", "Weak match", posted_ts=same_day, score=0.3)
        add_lead(mail, "strong", "Strong match", posted_ts=same_day, score=0.9)

        self.assertEqual([row["title"] for row in mail.list_leads()],
                         ["Strong match", "Weak match"])

    def test_the_unfiltered_view_is_ordered_the_same_way(self):
        mail = make_mail()
        add_lead(mail, "old", "Old role", posted_ts=days_ago(9),
                 status=LEAD_DISMISSED)
        add_lead(mail, "new", "New role", posted_ts=days_ago(2),
                 status=LEAD_APPLIED)

        self.assertEqual([row["title"] for row in mail.list_leads(None)],
                         ["New role", "Old role"])


class TestPurging(unittest.TestCase):
    def test_open_leads_past_the_window_are_deleted(self):
        mail = make_mail()
        add_lead(mail, "stale", posted_ts=days_ago(LEAD_FRESHNESS_DAYS + 1))
        add_lead(mail, "fresh", posted_ts=days_ago(LEAD_FRESHNESS_DAYS - 1))

        self.assertEqual(mail.purge_stale_leads(), 1)
        self.assertEqual([row["identity_key"] for row in mail.list_leads()],
                         ["fresh"])

    def test_every_open_status_is_eligible(self):
        mail = make_mail()
        for status in (LEAD_NEW, LEAD_READY):
            add_lead(mail, f"stale-{status}", posted_ts=days_ago(30),
                     status=status)

        self.assertEqual(mail.purge_stale_leads(), 2)

    def test_applied_leads_survive(self):
        """They are the record that the user applied to the role."""
        mail = make_mail()
        add_lead(mail, "applied", posted_ts=days_ago(90), status=LEAD_APPLIED)

        self.assertEqual(mail.purge_stale_leads(), 0)
        self.assertIsNotNone(mail.lead_by_identity("applied"))

    def test_dismissed_leads_survive(self):
        """Deleting one lets the next alert re-suggest a rejected role.

        The row is the memory of a decision. Without it `upsert_lead` inserts a
        fresh `new` lead the moment the board re-advertises, and a list that
        keeps returning roles the user said no to stops being trusted.
        """
        mail = make_mail()
        add_lead(mail, "dismissed", posted_ts=days_ago(90),
                 status=LEAD_DISMISSED)

        self.assertEqual(mail.purge_stale_leads(), 0)
        self.assertIsNotNone(mail.lead_by_identity("dismissed"))

    def test_the_window_is_configurable(self):
        mail = make_mail()
        add_lead(mail, "eight-days", posted_ts=days_ago(8))

        self.assertEqual(mail.purge_stale_leads(older_than_days=30), 0)
        self.assertEqual(mail.purge_stale_leads(older_than_days=7), 1)

    def test_a_lead_with_no_posting_date_falls_back_to_created_at(self):
        """Never immortal. A dateless row must still be able to expire."""
        mail = make_mail()
        add_lead(mail, "dateless", posted_ts=None)
        mail.conn.execute(
            "UPDATE job_leads SET created_at = '2020-01-01T00:00:00' "
            "WHERE identity_key = 'dateless'"
        )
        mail.commit()

        self.assertEqual(mail.purge_stale_leads(), 1)


class TestPostingDateOnUpsert(unittest.TestCase):
    def test_re_advertising_a_role_resets_its_clock(self):
        """A board still pushing a posting is evidence it is still open."""
        mail = make_mail()
        add_lead(mail, "role", posted_ts=days_ago(13))

        mail.upsert_lead({"identity_key": "role", "title": "Engineer",
                          "posted_ts": days_ago(1)})
        mail.commit()

        self.assertEqual(mail.lead_by_identity("role")["posted_ts"],
                         days_ago(1))
        self.assertEqual(mail.purge_stale_leads(), 0)

    def test_an_older_sighting_never_drags_the_date_backwards(self):
        mail = make_mail()
        recent = days_ago(1)
        add_lead(mail, "role", posted_ts=recent)

        mail.upsert_lead({"identity_key": "role", "title": "Engineer",
                          "posted_ts": days_ago(20)})
        mail.commit()

        self.assertEqual(mail.lead_by_identity("role")["posted_ts"], recent)

    def test_a_sighting_with_no_date_does_not_erase_the_stored_one(self):
        mail = make_mail()
        stored = days_ago(3)
        add_lead(mail, "role", posted_ts=stored)

        mail.upsert_lead({"identity_key": "role", "title": "Engineer"})
        mail.commit()

        self.assertEqual(mail.lead_by_identity("role")["posted_ts"], stored)

    def test_two_unknowns_stay_unknown_rather_than_becoming_the_epoch(self):
        """`MAX(COALESCE(...))` would collapse both NULLs to 0.

        Epoch 0 reads as fifty years stale, so the lead would be purged on the
        very next cycle. `NULLIF` is what puts the NULL back.
        """
        mail = make_mail()
        add_lead(mail, "role", posted_ts=None)

        mail.upsert_lead({"identity_key": "role", "title": "Engineer"})
        mail.commit()

        self.assertIsNone(mail.lead_by_identity("role")["posted_ts"])


if __name__ == "__main__":
    unittest.main()
