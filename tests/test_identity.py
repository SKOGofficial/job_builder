"""Identity normalization.

The highest-value tests in the project. Normalization is the load-bearing
assumption behind the whole ingest pipeline: if two spellings of one job do not
collapse, the same role shows up twice and its email history splits across the
rows. If two different jobs do collapse, one of them silently loses its history
and nothing surfaces the loss.

So the suite is organised as two opposing pressures - `MustCollapse` for pairs
that have to agree, `MustNotCollapse` for pairs that must stay apart.
"""

import unittest

from utilities.identity import (
    SCHEME_TITLE_COMPANY,
    SCHEME_TITLE_COMPANY_LOCATION,
    candidate_keys,
    company_slug,
    identity_key,
    identity_scheme,
    normalize_company,
    normalize_location,
    normalize_title,
)


class TestNormalizeTitle(unittest.TestCase):
    def test_lowercases_and_strips_punctuation(self):
        self.assertEqual(normalize_title("Software Engineer!"), "software engineer")

    def test_expands_seniority_abbreviations(self):
        self.assertEqual(normalize_title("Sr. Engineer"), "senior engineer")
        self.assertEqual(normalize_title("Jr Developer"), "junior developer")

    def test_expands_role_abbreviations(self):
        self.assertEqual(normalize_title("SWE"), "software engineer")

    def test_normalizes_roman_levels_to_digits(self):
        self.assertEqual(normalize_title("Engineer II"), "engineer 2")
        self.assertEqual(normalize_title("Engineer 2"), "engineer 2")

    def test_strips_gender_markers(self):
        self.assertEqual(normalize_title("Developer (m/f/d)"), "developer")

    def test_strips_requisition_ids(self):
        self.assertEqual(normalize_title("Analyst REQ-12345"), "analyst")
        self.assertEqual(normalize_title("Analyst #98765"), "analyst")

    def test_strips_remote_marker_from_title(self):
        # Remote belongs in the location field, not the title.
        self.assertEqual(normalize_title("Designer (Remote)"), "designer")

    def test_empty_input(self):
        self.assertEqual(normalize_title(""), "")
        self.assertEqual(normalize_title(None), "")


class TestNormalizeCompany(unittest.TestCase):
    def test_drops_legal_suffixes(self):
        self.assertEqual(normalize_company("Google LLC"), "google")
        self.assertEqual(normalize_company("Acme Inc."), "acme")

    def test_joins_words(self):
        # Joined rather than spaced so the Gmail matcher can test the slug
        # against a sender domain like "acmecorp.com".
        self.assertEqual(normalize_company("Acme Corp Holdings"), "acme")
        self.assertEqual(normalize_company("General Motors"), "generalmotors")

    def test_company_slug_alias_is_the_same_function(self):
        self.assertEqual(company_slug("Google LLC"), normalize_company("Google LLC"))


class TestNormalizeLocation(unittest.TestCase):
    def test_remote_family_agrees(self):
        for spelling in ("Remote", "remote", "Fully Remote", "100% Remote",
                         "Work From Home", "WFH"):
            self.assertEqual(normalize_location(spelling), "remote", spelling)

    def test_spelled_out_state_becomes_code(self):
        self.assertEqual(
            normalize_location("San Francisco, California"),
            normalize_location("San Francisco, CA"),
        )

    def test_country_aliases(self):
        self.assertEqual(normalize_location("United States"), "us")
        self.assertEqual(normalize_location("USA"), "us")

    def test_hybrid_keeps_both_parts(self):
        # A hybrid posting is a different arrangement from either a pure
        # office role or a pure remote one, so it must match neither.
        hybrid = normalize_location("San Francisco, CA (Remote)")
        self.assertIn("remote", hybrid)
        self.assertIn("san francisco", hybrid)
        self.assertNotEqual(hybrid, normalize_location("Remote"))
        self.assertNotEqual(hybrid, normalize_location("San Francisco, CA"))

    def test_remote_with_region(self):
        self.assertEqual(normalize_location("Remote - US"), "us|remote")

    def test_empty_input(self):
        self.assertEqual(normalize_location(""), "")
        self.assertEqual(normalize_location(None), "")


class TestMustCollapse(unittest.TestCase):
    """Pairs that describe one job and must produce one key."""

    def assert_same(self, first, second):
        self.assertEqual(identity_key(*first), identity_key(*second),
                         f"{first} should match {second}")

    def test_seniority_spelling(self):
        self.assert_same(
            ("Senior Software Engineer", "Google", "Remote"),
            ("Sr. Software Engineer", "Google", "Remote"),
        )

    def test_legal_suffix(self):
        self.assert_same(
            ("Software Engineer", "Google", "Remote"),
            ("Software Engineer", "Google LLC", "Remote"),
        )

    def test_state_spelling(self):
        self.assert_same(
            ("Data Analyst", "Acme", "Austin, TX"),
            ("Data Analyst", "Acme", "Austin, Texas"),
        )

    def test_board_noise_in_title(self):
        self.assert_same(
            ("Backend Engineer", "Stripe", "Remote"),
            ("Backend Engineer (m/f/d) REQ-4471", "Stripe", "Fully Remote"),
        )

    def test_case_and_whitespace(self):
        self.assert_same(
            ("  PRODUCT   manager ", "Figma", "New York, NY"),
            ("Product Manager", "figma", "New York, New York"),
        )


class TestMustNotCollapse(unittest.TestCase):
    """Pairs that describe different jobs and must stay apart.

    Failures here are the dangerous direction: a false merge destroys the
    application history of whichever row loses.
    """

    def assert_different(self, first, second):
        self.assertNotEqual(identity_key(*first), identity_key(*second),
                            f"{first} should NOT match {second}")

    def test_seniority_is_meaningful(self):
        self.assert_different(
            ("Software Engineer", "Google", "Remote"),
            ("Senior Software Engineer", "Google", "Remote"),
        )

    def test_level_is_meaningful(self):
        self.assert_different(
            ("Engineer II", "Acme", "Remote"),
            ("Engineer III", "Acme", "Remote"),
        )

    def test_different_company(self):
        self.assert_different(
            ("Software Engineer", "Google", "Remote"),
            ("Software Engineer", "Meta", "Remote"),
        )

    def test_different_location(self):
        self.assert_different(
            ("Software Engineer", "Google", "Austin, TX"),
            ("Software Engineer", "Google", "Seattle, WA"),
        )

    def test_remote_is_not_hybrid(self):
        self.assert_different(
            ("Software Engineer", "Google", "Remote"),
            ("Software Engineer", "Google", "Austin, TX (Remote)"),
        )

    def test_different_discipline(self):
        self.assert_different(
            ("Backend Engineer", "Stripe", "Remote"),
            ("Frontend Engineer", "Stripe", "Remote"),
        )


class TestSchemeAndCandidates(unittest.TestCase):
    def test_scheme_reflects_location_presence(self):
        self.assertEqual(identity_scheme("Remote"), SCHEME_TITLE_COMPANY_LOCATION)
        self.assertEqual(identity_scheme(""), SCHEME_TITLE_COMPANY)
        self.assertEqual(identity_scheme(None), SCHEME_TITLE_COMPANY)

    def test_key_without_location_differs_from_key_with_one(self):
        self.assertNotEqual(
            identity_key("Software Engineer", "Google", None),
            identity_key("Software Engineer", "Google", "Remote"),
        )

    def test_candidate_keys_orders_specific_first(self):
        keys = candidate_keys("Software Engineer", "Google", "Remote")
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], identity_key("Software Engineer", "Google", "Remote"))
        self.assertEqual(keys[1], identity_key("Software Engineer", "Google", None))

    def test_candidate_keys_without_location_yields_one(self):
        keys = candidate_keys("Software Engineer", "Google", None)
        self.assertEqual(keys, [identity_key("Software Engineer", "Google", None)])

    def test_candidate_keys_reach_pre_migration_rows(self):
        # A row stored before the location column existed only has the bare
        # key. A lead that knows its location must still find it.
        legacy = identity_key("Software Engineer", "Google", None)
        self.assertIn(legacy, candidate_keys("Software Engineer", "Google", "Remote"))


class TestKeyShape(unittest.TestCase):
    def test_twelve_uppercase_hex(self):
        key = identity_key("Software Engineer", "Google", "Remote")
        self.assertEqual(len(key), 12)
        self.assertTrue(all(c in "0123456789ABCDEF" for c in key), key)

    def test_deterministic(self):
        args = ("Software Engineer", "Google", "Remote")
        self.assertEqual(identity_key(*args), identity_key(*args))


if __name__ == "__main__":
    unittest.main()
