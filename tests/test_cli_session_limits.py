"""A subscription limit is a limit, and it names when it lifts.

The Claude Code CLI exists in this project precisely so a Pro subscription can
take work the metered free tiers cannot. Its ceilings are therefore the *normal*
case, not an edge one - and `_LIMIT_PATTERN` did not recognise the way it
phrases them.

Observed live: `You've hit your session limit - resets 12:10pm
(America/New_York)`. That matched none of `rate limit|usage limit|limit
reached|quota|too many requests`, so it fell through to the catch-all, was
reported as a crash, and the reset time it named was discarded in favour of a
flat cooldown.
"""

import unittest
from datetime import datetime

from clients.providers import claude_cli as cc
from clients.providers.base import ProviderRateLimited, ProviderUnavailable


class TestLimitsAreRecognised(unittest.TestCase):
    def test_the_message_that_was_missed(self):
        self.assertTrue(cc._LIMIT_PATTERN.search(
            "You've hit your session limit - resets 12:10pm (America/New_York)"))

    def test_the_subscription_ceilings(self):
        for text in ["You've hit your session limit",
                     "You've hit your weekly limit",
                     "Usage limit reached",
                     "rate limit exceeded",
                     "too many requests"]:
            with self.subTest(text=text):
                self.assertTrue(cc._LIMIT_PATTERN.search(text))

    def test_an_ordinary_crash_is_still_not_a_limit(self):
        for text in ["Segmentation fault", "ENOENT: no such file",
                     "unexpected token in JSON"]:
            with self.subTest(text=text):
                self.assertFalse(cc._LIMIT_PATTERN.search(text))


class TestResetTimes(unittest.TestCase):
    NOW = datetime(2026, 8, 21, 11, 59, 55)

    def test_a_clock_time_becomes_a_wait(self):
        seconds = cc._seconds_until_reset(
            "resets 12:10pm (America/New_York)", now=self.NOW)
        self.assertEqual(seconds, 605.0)

    def test_a_duration_still_wins_over_a_clock_time(self):
        """Unambiguous beats "read this against a clock in some timezone"."""
        self.assertEqual(
            cc.parse_retry_after("Try again in 3 hours, resets 12:10pm",
                                 now=self.NOW),
            10800.0,
        )

    def test_midnight_and_noon_are_not_confused(self):
        self.assertEqual(
            cc._seconds_until_reset("resets 12am", now=datetime(2026, 8, 21, 23, 0)),
            3600.0)
        self.assertEqual(
            cc._seconds_until_reset("resets 12pm", now=datetime(2026, 8, 21, 11, 0)),
            3600.0)

    def test_a_time_already_past_rolls_to_tomorrow_and_is_then_rejected(self):
        """The cap is what stops a timezone misread parking a provider all day.

        The CLI names a timezone that need not be the machine's, so a reset that
        looks like it already happened rolls forward 24 hours - which is exactly
        the reading that must not be trusted.
        """
        self.assertIsNone(
            cc._seconds_until_reset("resets 9am", now=datetime(2026, 8, 21, 10, 0)))

    def test_nothing_parseable_falls_back(self):
        self.assertIsNone(cc._seconds_until_reset("no time here"))
        self.assertEqual(cc.parse_retry_after("no time here"),
                         cc.DEFAULT_LIMIT_COOLDOWN)

    def test_a_nonsense_clock_is_refused(self):
        self.assertIsNone(cc._seconds_until_reset("resets 99:99"))


class TestItReachesTheRightException(unittest.TestCase):
    """The point of all of it: the pool must be told "wait", not "broken"."""

    def test_a_session_limit_raises_a_rate_limit_carrying_its_wait(self):
        with self.assertRaises(ProviderRateLimited) as caught:
            cc._refuse("Claude Code CLI reported a failure",
                       "You've hit your session limit - resets 12:10pm")
        self.assertGreater(caught.exception.retry_after, 0)
        self.assertEqual(caught.exception.provider, cc.DISPLAY_NAME)

    def test_a_crash_is_still_unavailable_rather_than_a_limit(self):
        with self.assertRaises(ProviderUnavailable):
            cc._refuse("Claude Code CLI exited non-zero", "Segmentation fault")


if __name__ == "__main__":
    unittest.main()
