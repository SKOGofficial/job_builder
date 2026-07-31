"""Smoke tests that every page renders and that the module split holds together.

These need a display. On a headless Linux runner, wrap the run with xvfb-run.

Isolation: JobStore resolves DB_PATH when it is called rather than binding it as
a default argument, so pointing store.DB_PATH at a temporary file is enough to
keep these tests away from the real job_applications.sqlite3.
"""

import os
import tempfile
import unittest

import app
import utilities.store as store
from pages import DRAWER_ENTRIES, NAV_TABS, PAGE_CLASSES
from pages.email_matches import NO_BODY_HINT
from utilities.theme import TIME_RANGES


def descendants(widget):
    """Every widget under this one, depth first."""
    found = []
    for child in widget.winfo_children():
        found.append(child)
        found.extend(descendants(child))
    return found


def widgets_of_class(widget, class_name):
    return [w for w in descendants(widget) if w.winfo_class() == class_name]


def display_available():
    try:
        import tkinter

        root = tkinter.Tk()
    except Exception:
        return False
    root.destroy()
    return True


HAVE_DISPLAY = display_available()

PAGE_NAMES = [cls.name for cls in PAGE_CLASSES]


class PageRegistryTests(unittest.TestCase):
    """These need no display, so they run everywhere."""

    def test_every_page_has_a_unique_name(self):
        names = [cls.name for cls in PAGE_CLASSES]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(names))

    def test_every_page_implements_render(self):
        for cls in PAGE_CLASSES:
            with self.subTest(page=cls.name):
                self.assertIn("render", dir(cls))
                self.assertIsNot(cls.render, app.JobTrackerApp.__dict__.get("render"))

    def test_nav_and_drawer_only_reference_real_pages(self):
        for label, name in NAV_TABS + DRAWER_ENTRIES:
            with self.subTest(entry=label):
                self.assertIn(name, PAGE_NAMES)

    def test_email_matches_is_reachable_from_the_drawer(self):
        self.assertIn("email_matches", [name for _label, name in DRAWER_ENTRIES])


@unittest.skipUnless(HAVE_DISPLAY, "no display available for tkinter")
class PageRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handle, cls.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        cls.original_db_path = store.DB_PATH
        store.DB_PATH = cls.db_path
        cls.gui = app.JobTrackerApp()
        cls.gui.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.gui.store.conn.close()
        cls.gui.destroy()
        store.DB_PATH = cls.original_db_path
        os.unlink(cls.db_path)

    def test_uses_the_temporary_database(self):
        # Guards the isolation described in the module docstring. If this fails,
        # the other tests in this class are touching the real database.
        (path,) = self.gui.store.conn.execute(
            "SELECT file FROM pragma_database_list WHERE name = 'main'"
        ).fetchone()
        self.assertEqual(os.path.normcase(path), os.path.normcase(self.db_path))

    def test_every_page_renders(self):
        for name in PAGE_NAMES:
            with self.subTest(page=name):
                self.gui.show_page(name)
                self.gui.update_idletasks()

    def test_unknown_page_is_rejected(self):
        with self.assertRaises(KeyError):
            self.gui.show_page("does_not_exist")

    def test_pages_render_in_both_themes(self):
        for _ in range(2):
            self.gui.toggle_theme()
            self.gui.update_idletasks()
            for name in PAGE_NAMES:
                with self.subTest(theme=self.gui.theme_name, page=name):
                    self.gui.show_page(name)
                    self.gui.update_idletasks()

    def test_drawer_lists_every_configured_entry(self):
        self.gui.toggle_drawer()
        self.gui.update_idletasks()
        labels = [
            child.cget("text")
            for child in self.gui.drawer.winfo_children()
            if child.winfo_class() == "TButton"
        ]
        for expected, _name in DRAWER_ENTRIES:
            self.assertIn(expected, labels)
        self.gui.toggle_drawer()

    def test_dashboard_survives_every_time_range(self):
        dashboard = self.gui.pages["dashboard"]
        for label, days in TIME_RANGES:
            with self.subTest(range=label):
                dashboard.set_range(days)
                self.gui.update_idletasks()
                self.assertEqual(dashboard.range_days, days)

    # Email match cards ----------------------------------------------------

    def seed_match(self, body="Thanks for applying. Can we talk Tuesday?"):
        """Put one pending match on the page and clean it up afterwards."""
        job_id = self.gui.store.create_job(
            {
                "posting_url": "https://acme.com/jobs/render",
                "position_title": "Engineer",
                "company": "Acme",
                "job_type": "Internship",
                "requires_oa": False,
                "completed_oa": False,
                "received_references": False,
                "payment_amount": "",
                "payment_period": "Unspecified",
                "status": "Applied",
                "application_date": "2026-07-28",
                "response_date": None,
                "notes": "",
            }
        )
        self.gui.store.record_email_match(
            job_id,
            {
                "id": "msg-render",
                "sender": "Careers <careers@acme.com>",
                "subject": "Interview?",
                "date": "Tue, 28 Jul 2026 10:00:00 -0400",
                "body": body,
                "snippet": body[:40],
            },
        )
        match_id = self.gui.store.pending_email_matches()[0]["id"]

        def cleanup():
            self.gui.store.conn.execute("DELETE FROM email_matches")
            self.gui.store.conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            self.gui.store.conn.commit()
            self.gui.pages["email_matches"].expanded.clear()

        self.addCleanup(cleanup)
        return match_id

    def toggle_button(self):
        buttons = widgets_of_class(self.gui.content, "TButton")
        return next(b for b in buttons if b.cget("text") in ("▶", "▼"))

    def test_email_match_card_starts_collapsed(self):
        self.seed_match()
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()
        (body,) = widgets_of_class(self.gui.content, "Text")
        # The pane is built but never handed to the geometry manager, so an
        # unmanaged parent is what "collapsed" looks like.
        self.assertEqual(body.master.winfo_manager(), "")
        self.assertEqual(self.toggle_button().cget("text"), "▶")

    def test_email_match_card_expands_to_show_the_message(self):
        match_id = self.seed_match()
        page = self.gui.pages["email_matches"]
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()

        self.toggle_button().invoke()
        self.gui.update_idletasks()

        self.assertIn(match_id, page.expanded)
        (body,) = widgets_of_class(self.gui.content, "Text")
        self.assertEqual(body.master.winfo_manager(), "pack")
        self.assertIn("Can we talk Tuesday?", body.get("1.0", "end"))
        self.assertEqual(self.toggle_button().cget("text"), "▼")

    def test_expanded_message_is_read_only(self):
        self.seed_match()
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()
        self.toggle_button().invoke()
        (body,) = widgets_of_class(self.gui.content, "Text")
        self.assertEqual(str(body.cget("state")), "disabled")

    def test_expanded_state_survives_navigation(self):
        match_id = self.seed_match()
        page = self.gui.pages["email_matches"]
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()
        self.toggle_button().invoke()

        self.gui.show_page("jobs")
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()

        self.assertIn(match_id, page.expanded)
        (body,) = widgets_of_class(self.gui.content, "Text")
        self.assertEqual(body.master.winfo_manager(), "pack")

    def test_collapsing_hides_the_message_again(self):
        match_id = self.seed_match()
        page = self.gui.pages["email_matches"]
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()
        self.toggle_button().invoke()
        self.toggle_button().invoke()
        self.gui.update_idletasks()

        self.assertNotIn(match_id, page.expanded)
        (body,) = widgets_of_class(self.gui.content, "Text")
        self.assertEqual(body.master.winfo_manager(), "")

    def test_match_without_body_shows_a_placeholder(self):
        # Rows recorded before bodies were stored must still render.
        self.seed_match(body="")
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()
        self.toggle_button().invoke()
        self.gui.update_idletasks()

        self.assertEqual(widgets_of_class(self.gui.content, "Text"), [])
        labels = [w.cget("text") for w in widgets_of_class(self.gui.content, "TLabel")]
        self.assertIn(NO_BODY_HINT, labels)

    def test_dismissing_clears_the_card(self):
        match_id = self.seed_match()
        self.gui.show_page("email_matches")
        self.gui.pages["email_matches"].dismiss(match_id)
        self.gui.update_idletasks()
        self.assertEqual(self.gui.store.pending_email_matches(), [])
        self.assertEqual(widgets_of_class(self.gui.content, "Text"), [])

    def test_page_state_survives_navigation(self):
        # Page objects are reused, so per-page state must not reset on return.
        dashboard = self.gui.pages["dashboard"]
        dashboard.set_range(90)
        self.gui.show_page("jobs")
        self.gui.show_page("dashboard")
        self.assertEqual(dashboard.range_days, 90)


class GmailOptionalImportTests(unittest.TestCase):
    def test_gmail_flag_matches_import_state(self):
        # The tracker must stay usable as a local-only tool, so a missing Gmail
        # dependency has to degrade rather than break the app.
        if app.GMAIL_AVAILABLE:
            self.assertIsNotNone(app.gmail_client)
        else:
            self.assertIsNone(app.gmail_client)
            self.assertTrue(app.GMAIL_IMPORT_ERROR)


if __name__ == "__main__":
    unittest.main()
