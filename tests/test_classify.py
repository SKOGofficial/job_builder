"""The rule classifier.

Every sender and subject below is taken from the real mailbox, because the
whole value of this module is that it matches what the boards actually send
rather than what they plausibly might. The awkward cases are the point: three
LinkedIn addresses that each mean something different, one Glassdoor address
that carries both job alerts and a forum digest, and an ATS relay that sends
receipts, rejections, and its own marketing.

The declines matter as much as the matches. A rule that guesses wrong writes a
wrong label with high confidence and no model ever sees the message again, so
anything genuinely ambiguous - "thank you for your interest in X", which opens
acknowledgements and rejections in equal measure - must come back None.
"""

import unittest

from pipeline.classify import RULE_MODEL, classify, classify_message
from utilities.mailstore import (
    CATEGORY_ACKNOWLEDGEMENT,
    CATEGORY_ALERT,
    CATEGORY_IRRELEVANT,
    CATEGORY_UPDATE,
)


def label(sender, subject):
    """The label a rule assigns, or None when none fires."""
    result = classify(sender, subject)
    return None if result is None else result["label"]


class TestJobBoardAlerts(unittest.TestCase):
    """The bulk of the mailbox, and the whole reason this module exists."""

    def test_the_five_boards_that_dominate_the_mailbox(self):
        for sender, subject in [
            ("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
             "Software Engineer, Platform at Doppel"),
            ("Indeed <donotreply@match.indeed.com>",
             "Software Engineer I (Remote Eligible) @ Smartsheet"),
            ("ZipRecruiter <alerts@ziprecruiter.com>",
             "$157K/yr Mid-Level Test Engineer job in Chantilly, VA"),
            ("Glassdoor Jobs <noreply@glassdoor.com>",
             "AI Engineer at Global KTech and 8 more jobs in Fairfax, VA"),
            ("Jobright <noreply@jobright.ai>",
             "Enigma Technologies just posted a 69% match Data Operations role"),
        ]:
            with self.subTest(sender=sender):
                self.assertEqual(label(sender, subject), CATEGORY_ALERT)

    def test_a_digest_from_an_unknown_sender_is_caught_by_its_subject(self):
        """The long tail: no list of board domains can ever be complete."""
        self.assertEqual(
            label("Careers <hello@somestartup.example>",
                  "7 new roles for you this week"),
            CATEGORY_ALERT,
        )

    def test_recruiter_phrasing_that_names_no_count(self):
        for subject in [
            "Soham, I think this job might be right for you!",
            "Leidos is hiring now. August 6, 2026",
            "Junior Full Stack Developer role at Assyst: you would be a great fit!",
            "Soham, Leidos has an open position",
        ]:
            with self.subTest(subject=subject):
                self.assertEqual(
                    label("Recruiting <careers@example.test>", subject),
                    CATEGORY_ALERT,
                )


class TestAcknowledgements(unittest.TestCase):
    def test_the_common_receipt_phrasings(self):
        for subject in [
            "Thank you for applying to Coinbase",
            "Thanks for applying to Google",
            "Soham, we have received your application",
            "We received your application",
            "Thank you for your application to Notion, Soham!",
            "Thanks for submitting your application",
            "Your recent job application for Junior AI Applications Engineer",
            "Keep track of your application",
        ]:
            with self.subTest(subject=subject):
                self.assertEqual(
                    label("Careers <no-reply@example.test>", subject),
                    CATEGORY_ACKNOWLEDGEMENT,
                )

    def test_a_quoted_role_between_your_and_application(self):
        """ZipRecruiter puts the role in the middle of the phrase.

        `Your "Automated QA Engineer" application is complete` breaks any
        pattern anchored on `your application`, so the rule anchors on the tail.
        """
        self.assertEqual(
            label('"Phil @ ZipRecruiter" <phil@ziprecruiter.com>',
                  'Your "Automated QA Engineer - AI Trainer" application is complete'),
            CATEGORY_ACKNOWLEDGEMENT,
        )


class TestOneSenderThreeMeanings(unittest.TestCase):
    """`jobs-noreply@linkedin.com` sends all three job categories.

    This is the case that forces subject intent to be settled before sender
    reputation. Reverse the two and every Easy Apply receipt becomes an advert.
    """

    SENDER = "LinkedIn <jobs-noreply@linkedin.com>"

    def test_easy_apply_receipt_is_an_acknowledgement(self):
        self.assertEqual(
            label(self.SENDER, "Soham , your application was sent to BeaconFire Inc."),
            CATEGORY_ACKNOWLEDGEMENT,
        )

    def test_status_mail_is_an_update(self):
        self.assertEqual(
            label(self.SENDER,
                  "Your application to Mazak Lathe Programmer at Sustainable Staffing"),
            CATEGORY_UPDATE,
        )

    def test_everything_else_from_it_is_an_advert(self):
        self.assertEqual(
            label(self.SENDER, "CACI International Inc is hiring for a Remote role"),
            CATEGORY_ALERT,
        )


class TestBoardNoise(unittest.TestCase):
    """Board mail advertising no role at all.

    This is what filled the old review queue: a digest of ten unrelated roles,
    or a "welcome to our job board" mail, shown next to a picker asking which
    of the user's applications it belonged to.
    """

    def test_marketing_from_a_board(self):
        for sender, subject in [
            ("MyGreenhouse <notifications@us.greenhouse-jobs.com>",
             "Show recruiters you're really interested with Dream Job"),
            ("MyGreenhouse <no-reply@us.greenhouse-jobs.com>",
             "Welcome to MyGreenhouse. Start your search."),
            ("LinkedIn <messages-noreply@linkedin.com>",
             "Get noticed by recruiters for 30+ jobs at companies like Esri"),
            ("LinkedIn <billing-noreply@linkedin.com>",
             "Thank you for purchasing Premium Career"),
            ("Indeed <no-reply@indeed.com>", "Terms of Service Updates"),
        ]:
            with self.subTest(subject=subject):
                self.assertEqual(label(sender, subject), CATEGORY_IRRELEVANT)

    def test_the_forum_digest_shares_an_address_with_the_job_alerts(self):
        """Only the display name separates these two Glassdoor messages."""
        self.assertEqual(
            label("Glassdoor Community <noreply@glassdoor.com>",
                  "Is NYC still worth it if you're single and mid-30s?"),
            CATEGORY_IRRELEVANT,
        )
        self.assertEqual(
            label("Glassdoor Jobs <noreply@glassdoor.com>",
                  "Machine Learning Engineer at MORSE Corp and 12 more jobs"),
            CATEGORY_ALERT,
        )


class TestDeclining(unittest.TestCase):
    """What the rules must refuse to answer."""

    def test_thank_you_for_your_interest_is_left_to_the_model(self):
        """The phrase opens receipts and rejections in equal measure.

        In the stored mailbox it introduces an acknowledgement from TCOM and a
        rejection from CACI, and only the body tells them apart. Guessing here
        would silently mark live applications dead.
        """
        for sender, subject in [
            ("no-reply@tcomcareers.com", "Thank you for your interest in TCOM, L.P."),
            ("caci@myworkday.com",
             "Thank you for your interest in the Junior Software Engineer position"),
            ("no-reply@us.greenhouse-mail.io", "Thank you for your interest in CLEAR"),
        ]:
            with self.subTest(subject=subject):
                self.assertIsNone(classify(sender, subject))

    def test_a_recruiter_inmail_goes_to_the_model(self):
        """`inmail-hit-reply` looks like noise and is not.

        It is how a human recruiter's approach arrives, carrying real roles, so
        it is deliberately absent from the noise list.
        """
        self.assertIsNone(classify(
            "Saad Afzal <inmail-hit-reply@linkedin.com>",
            "Open Job For Associate, Software Engineer, at Herndon, Virginia",
        ))

    def test_a_bare_role_title_from_an_unknown_sender(self):
        self.assertIsNone(classify(
            "Matroid Recruitment <no-reply@matroid.breezy-mail.com>",
            "Matroid: Deep Learning Field Engineer",
        ))

    def test_nothing_at_all(self):
        self.assertIsNone(classify("", ""))
        self.assertIsNone(classify(None, None))


class TestUpdateRulesAreNarrow(unittest.TestCase):
    """Update words are common in adverts, so the patterns are multi-word.

    A bare `interview` or `offer` matches "Interview Coach at Google" and
    "Offer Management Analyst", and labelling those updates would attach an
    advert to an application and mark the wrong job dead.
    """

    def test_a_role_title_containing_an_update_word_is_not_an_update(self):
        for subject in [
            "Interview Coach at Google",
            "Offer Management Analyst at Capital One",
            "Assessment Specialist role in Reston, VA",
        ]:
            with self.subTest(subject=subject):
                self.assertNotEqual(
                    label("Careers <careers@example.test>", subject),
                    CATEGORY_UPDATE,
                )

    def test_real_update_phrasings_still_match(self):
        for subject in [
            "Update on your application to Genius AI",
            "Amazon application: Status update",
            "Interview confirmation - Backend Engineer",
            "Your memoryBlue Application & Next Steps",
            "Security code for your application to Rackner",
            "[IMPORTANT] Applications of AI Engineering - Withdrawal Notice",
        ]:
            with self.subTest(subject=subject):
                self.assertEqual(
                    label("Careers <careers@example.test>", subject),
                    CATEGORY_UPDATE,
                )

    def test_next_steps_alone_is_not_enough(self):
        """Course reminders and marketing say it too."""
        self.assertIsNone(classify(
            "CodePath <support@codepath.org>",
            "[Missing] For your next step, Complete CodePath AI201 Early Course Survey",
        ))


class TestResultShape(unittest.TestCase):
    def test_a_match_carries_a_confidence_and_a_reason(self):
        result = classify("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
                          "Software Engineer at Doppel")
        self.assertEqual(result["label"], CATEGORY_ALERT)
        self.assertGreaterEqual(result["confidence"], 0.9)
        self.assertTrue(result["reason"])

    def test_classify_message_reads_a_row(self):
        self.assertEqual(
            classify_message({"sender": "Indeed <donotreply@match.indeed.com>",
                              "subject": "Software Engineer @ AHEAD"})["label"],
            CATEGORY_ALERT,
        )

    def test_the_rule_tier_is_attributable(self):
        """Stored in `category_model`, so a bad label can be traced here."""
        self.assertEqual(RULE_MODEL, "rules")


if __name__ == "__main__":
    unittest.main()
