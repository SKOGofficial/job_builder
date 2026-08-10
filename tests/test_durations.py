"""Spelling a wait.

Small, but it guards a real regression: a daily ceiling reported as "86400s"
or "1440m" is the kind of message that reads as a bug in the thing reporting
it, which is exactly the impression the rate-limit logs used to give.
"""

import unittest

from utilities.durations import spell_duration
from utilities.mailstore import PREPARE_WAITING_PREFIX, waiting_note


class SpellDurationTests(unittest.TestCase):
    def test_seconds_below_the_minute_threshold(self):
        self.assertEqual(spell_duration(45), "45s")
        self.assertEqual(spell_duration(89), "89s")

    def test_minutes(self):
        self.assertEqual(spell_duration(90), "2m")
        self.assertEqual(spell_duration(240), "4m")

    def test_a_daily_ceiling_reads_in_hours(self):
        """The case that prompted the module."""
        self.assertEqual(spell_duration(86400), "24h")
        self.assertEqual(spell_duration(5400), "2h")

    def test_no_wait_is_an_empty_phrase_not_zero(self):
        """So a caller can drop the clause rather than promise "about 0s"."""
        for value in (0, -5, None, "", "nonsense"):
            self.assertEqual(spell_duration(value), "", repr(value))


class WaitingNoteTests(unittest.TestCase):
    def test_the_note_carries_the_prefix_the_leads_page_looks_for(self):
        note = waiting_note(240)
        self.assertTrue(note.startswith(PREPARE_WAITING_PREFIX))
        self.assertIn("4m", note)

    def test_an_unknown_wait_still_says_something_useful(self):
        self.assertEqual(waiting_note(None),
                         f"{PREPARE_WAITING_PREFIX}. Retrying next cycle.")

    def test_a_day_scoped_limit_is_not_spelled_in_seconds(self):
        self.assertIn("24h", waiting_note(86400))


if __name__ == "__main__":
    unittest.main()
