"""Argument parsing and the CLI's shape.

Thin, but it caught a real one: `--host` referenced a `DEFAULT_HOST` constant
that was never defined, so every invocation died at startup with a NameError.
Nothing else in the suite calls `parse_args`, because every other test
constructs its objects directly - so the entry point had no coverage at all
while looking perfectly well tested.
"""

import unittest

import app
import cli


class TestParseArgs(unittest.TestCase):
    def test_defaults(self):
        args = app.parse_args([])
        self.assertEqual(args.host, app.DEFAULT_HOST)
        self.assertEqual(args.port, app.DEFAULT_PORT)
        self.assertFalse(args.headless)
        self.assertFalse(args.browser)
        self.assertFalse(args.no_poll)

    def test_default_host_is_loopback(self):
        # The entire access control model. If this ever changes, the change
        # should be deliberate enough to update a test.
        self.assertEqual(app.DEFAULT_HOST, "127.0.0.1")

    def test_headless_flags(self):
        args = app.parse_args(["--headless", "--no-poll", "--port", "9000"])
        self.assertTrue(args.headless)
        self.assertTrue(args.no_poll)
        self.assertEqual(args.port, 9000)

    def test_host_override(self):
        args = app.parse_args(["--host", "0.0.0.0"])
        self.assertEqual(args.host, "0.0.0.0")

    def test_browser_flag(self):
        self.assertTrue(app.parse_args(["--browser"]).browser)


class TestCliParser(unittest.TestCase):
    def test_every_subcommand_resolves_to_a_handler(self):
        parser = cli.build_parser()
        for command in ("status", "sync", "backfill", "filter-stats", "prune",
                        "prepare", "deny"):
            args = parser.parse_args(
                [command] + (["example.com"] if command == "deny" else []))
            self.assertTrue(callable(getattr(args, "func", None)), command)

    def test_backfill_defaults_are_bounded(self):
        # An unbounded first walk over a decade-old mailbox is not something
        # to trigger by accident.
        args = cli.build_parser().parse_args(["backfill"])
        self.assertGreater(args.days, 0)
        self.assertGreater(args.max, 0)

    def test_command_is_required(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args([])


if __name__ == "__main__":
    unittest.main()
