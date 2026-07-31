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
import clients.llm_client as llm
import utilities.store as store
from utilities import credentials
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


def button_texts(widget):
    return [w.cget("text") for w in widgets_of_class(widget, "TButton")]


def label_texts(widget):
    return [w.cget("text") for w in widgets_of_class(widget, "TLabel")]


class FailingKeyring:
    """A machine where keyring is installed but no backend answers.

    Defined here rather than imported so this module stays runnable on its own.
    """

    def get_password(self, service, username):
        raise credentials.KeyringError("No recommended backend was available")

    def set_password(self, service, username, value):
        raise credentials.KeyringError("No recommended backend was available")

    def delete_password(self, service, username):
        raise credentials.KeyringError("No recommended backend was available")


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

    def test_every_page_renders_without_a_credential_store(self):
        # The Linux and CI case: keyring is installed but no backend answers.
        # Rendering must not depend on a credential store being present, and a
        # missing one must read as "no stored secret" rather than an error.
        saved = credentials.keyring
        credentials.keyring = FailingKeyring()
        self.addCleanup(setattr, credentials, "keyring", saved)
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

    # AI classification UI -------------------------------------------------

    def configured_classifier(self, state=llm.IDLE, **attributes):
        """Present a classifier in a chosen state, without needing a real key."""
        runner = self.gui.classifier
        runner.is_configured = lambda: True
        runner.state = state
        for name, value in attributes.items():
            setattr(runner, name, value)
        self.addCleanup(runner.__dict__.pop, "is_configured", None)
        self.addCleanup(setattr, runner, "state", llm.IDLE)
        self.addCleanup(setattr, runner, "message", "")
        return runner

    def test_classifier_offers_a_run_when_idle(self):
        self.seed_match()
        self.configured_classifier(llm.IDLE)
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()
        self.assertTrue(any("Classify" in text for text in button_texts(self.gui.content)))
        self.assertEqual(widgets_of_class(self.gui.content, "TProgressbar"), [])

    def test_running_state_shows_a_progress_bar_and_stop(self):
        self.seed_match()
        self.configured_classifier(
            llm.RUNNING, total=4, processed=1, current="Acme", message=""
        )
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()

        (bar,) = widgets_of_class(self.gui.content, "TProgressbar")
        self.assertEqual(int(bar.cget("maximum")), 4)
        self.assertEqual(int(bar.cget("value")), 1)
        self.assertIn("Stop", button_texts(self.gui.content))
        self.assertTrue(
            any("Classifying 2 of 4" in text for text in label_texts(self.gui.content))
        )

    def test_rate_limited_state_offers_resume(self):
        self.seed_match()
        self.configured_classifier(
            llm.RATE_LIMITED,
            total=4,
            processed=2,
            retry_after=42,
            message="Groq rate limit reached after 2 of 4. Try again in about 42s.",
        )
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()

        self.assertIn("Resume classification", button_texts(self.gui.content))
        (bar,) = widgets_of_class(self.gui.content, "TProgressbar")
        # Paused styling makes the stall visible rather than looking idle.
        self.assertEqual(str(bar.cget("style")), "Paused.Horizontal.TProgressbar")
        self.assertTrue(any("rate limit" in text for text in label_texts(self.gui.content)))

    def test_stopped_and_error_states_offer_resume(self):
        for state in (llm.STOPPED, llm.ERROR):
            with self.subTest(state=state):
                self.configured_classifier(state, message="stopped early")
                self.gui.show_page("email_matches")
                self.gui.update_idletasks()
                self.assertIn("Resume classification", button_texts(self.gui.content))

    def test_unconfigured_classifier_points_at_settings(self):
        runner = self.gui.classifier
        runner.is_configured = lambda: False
        self.addCleanup(runner.__dict__.pop, "is_configured", None)
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()
        self.assertIn("Open Settings", button_texts(self.gui.content))

    def test_mid_cycle_update_does_not_rebuild_the_page(self):
        # Rebuilding on every classified message would be wasteful and would
        # reset the scroll position, so only the two progress widgets change.
        self.seed_match()
        runner = self.configured_classifier(llm.RUNNING, total=4, processed=1, current="Acme")
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()
        page = self.gui.pages["email_matches"]
        original_label = page.progress_label
        original_bar = page.progress_bar

        runner.processed = 3
        runner.current = "Globex"
        page.on_classification_update(final=False)
        self.gui.update_idletasks()

        self.assertIs(page.progress_label, original_label)
        self.assertIs(page.progress_bar, original_bar)
        self.assertIn("Classifying 4 of 4", original_label.cget("text"))
        self.assertEqual(int(original_bar.cget("value")), 3)

    def test_final_update_redraws_the_page(self):
        self.seed_match()
        runner = self.configured_classifier(llm.RUNNING, total=1, processed=1)
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()
        page = self.gui.pages["email_matches"]
        original_label = page.progress_label

        runner.state = llm.DONE
        runner.message = "Classified 1 message(s); 1 status(es) applied automatically."
        page.on_classification_update(final=True)
        self.gui.update_idletasks()

        self.assertIsNot(page.progress_label, original_label)
        self.assertTrue(
            any("applied automatically" in text for text in label_texts(self.gui.content))
        )

    def test_applied_classification_shows_a_badge_and_undo(self):
        match_id = self.seed_match()
        self.configured_classifier(llm.DONE)
        self.gui.store.record_classification(match_id, "Rejected", 0.94, "They declined.")
        self.gui.store.apply_ai_status(match_id, "Rejected")
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()

        labels = label_texts(self.gui.content)
        self.assertTrue(any("AI: Rejected · 94%" in text for text in labels))
        self.assertTrue(any("They declined." in text for text in labels))
        self.assertIn("Undo", button_texts(self.gui.content))

    def test_undo_button_restores_the_previous_status(self):
        match_id = self.seed_match()
        self.configured_classifier(llm.DONE)
        self.gui.store.record_classification(match_id, "Rejected", 0.94, "They declined.")
        self.gui.store.apply_ai_status(match_id, "Rejected")
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()

        undo = next(
            w for w in widgets_of_class(self.gui.content, "TButton")
            if w.cget("text") == "Undo"
        )
        undo.invoke()
        self.gui.update_idletasks()

        match = self.gui.store.pending_email_matches()[0]
        self.assertEqual(match["job_status"], "Applied")
        self.assertEqual(match["ai_applied"], 0)
        self.assertNotIn("Undo", button_texts(self.gui.content))

    def test_low_confidence_classification_only_preselects(self):
        match_id = self.seed_match()
        self.configured_classifier(llm.DONE)
        self.gui.store.record_classification(match_id, "Offer", 0.40, "Maybe an offer.")
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()

        self.assertNotIn("Undo", button_texts(self.gui.content))
        (combo,) = widgets_of_class(self.gui.content, "TCombobox")
        self.assertEqual(combo.get(), "Offer")

    def test_inert_label_does_not_preselect_a_status(self):
        match_id = self.seed_match()
        self.configured_classifier(llm.DONE)
        self.gui.store.record_classification(match_id, "Acknowledgement", 0.99, "Routine.")
        self.gui.show_page("email_matches")
        self.gui.update_idletasks()

        (combo,) = widgets_of_class(self.gui.content, "TCombobox")
        self.assertEqual(combo.get(), "Interview")

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
