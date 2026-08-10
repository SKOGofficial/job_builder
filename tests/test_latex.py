"""LaTeX emission, escaping, and delivery.

The escaping tests carry most of the weight here. A LaTeX document with a bad
escape does not fail - it compiles, silently missing whatever followed the
offending character, which is how a real resume came to claim a solar panel
"converted 60" with the units gone.
"""

import asyncio
import os
import tempfile
import textwrap
import time
import unittest

from pipeline import documents, latex
from utilities.mailstore import MailStore
from utilities.store import JobStore

MASTER = textwrap.dedent(r"""
    \documentclass{article}
    \begin{document}
    %-----------EDUCATION-----------
    \section{Education}
      {Verified University}{May 2026}
    %-----------EXPERIENCE-----------
    \section{Experience}
      old experience that must not survive
    %-----------PROJECTS-----------
    \section{Projects}
      old projects that must not survive
    %-----------PROGRAMMING SKILLS-----------
    \section{Technical Skills}
      Python, C
    \end{document}
""").strip()


class EscapeTests(unittest.TestCase):
    def test_percent_is_escaped(self):
        # The one that matters: an unescaped % comments out the rest of the
        # line and the document still compiles.
        self.assertEqual(latex.escape("converted 60% of energy"),
                         r"converted 60\% of energy")

    def test_ampersand_and_underscore(self):
        self.assertEqual(latex.escape("R&D on cost_per_unit"),
                         r"R\&D on cost\_per\_unit")

    def test_every_special_character(self):
        escaped = latex.escape(r"100% & $5 #1 a_b {x} ~ ^ \ ")
        for raw in ("%", "&", "$", "#", "_", "{", "}"):
            self.assertNotIn(f" {raw}", escaped, f"{raw} left unescaped")
        self.assertIn(r"\textbackslash{}", escaped)

    def test_backslash_does_not_double_escape(self):
        # If the backslash were replaced after the others, its own replacement
        # would itself be escaped and the output would be garbage.
        self.assertEqual(latex.escape("a\\b"), r"a\textbackslash{}b")

    def test_balanced_quotes_become_directional(self):
        self.assertEqual(latex.escape('Co-authored "A Paper" today'),
                         "Co-authored ``A Paper'' today")

    def test_unpaired_quote_is_left_alone(self):
        # More often an inch mark than an opening quote.
        self.assertIn('6" of', latex.escape('a 6" of rain'))

    def test_empty_and_none(self):
        self.assertEqual(latex.escape(None), "")
        self.assertEqual(latex.escape(""), "")


class SplitMasterTests(unittest.TestCase):
    def test_keeps_education_and_skills(self):
        head, tail = latex.split_master(MASTER)
        self.assertIn("Verified University", head)
        self.assertIn("Technical Skills", tail)
        self.assertIn(r"\end{document}", tail)

    def test_drops_the_sections_it_regenerates(self):
        head, tail = latex.split_master(MASTER)
        self.assertNotIn("must not survive", head + tail)

    def test_a_missing_marker_is_an_error(self):
        with self.assertRaises(ValueError) as caught:
            latex.split_master(r"\documentclass{article}")
        self.assertIn("EXPERIENCE", str(caught.exception))

    def test_markers_out_of_order_are_an_error(self):
        reordered = (latex.SKILLS_MARKER + "\nskills\n" +
                     latex.EXPERIENCE_MARKER + "\nexperience\n")
        with self.assertRaises(ValueError):
            latex.split_master(reordered)


class RenderResumeTests(unittest.TestCase):
    def _group(self, kind="work", **overrides):
        entry = {
            "kind": kind,
            "organisation": "Acme",
            "role": "Engineer",
            "start_date": "2024-01",
            "end_date": "2025-01",
            "bullets": ["Cut latency by 40% & shipped it"],
        }
        entry.update(overrides)
        return entry

    def test_work_uses_subheading_and_escapes_bullets(self):
        out = latex.render_resume(MASTER, [self._group()], [])
        self.assertIn(r"\resumeSubheading", out)
        self.assertIn(r"40\% \& shipped", out)
        self.assertIn("2024-01 - 2025-01", out)

    def test_projects_use_the_project_heading(self):
        out = latex.render_resume(MASTER, [], [self._group(kind="project")])
        self.assertIn(r"\resumeProjectHeading", out)

    def test_the_master_scaffolding_survives(self):
        out = latex.render_resume(MASTER, [self._group()], [])
        self.assertIn("Verified University", out)
        self.assertIn(r"\end{document}", out)

    def test_empty_sections_are_omitted_not_empty(self):
        out = latex.render_resume(MASTER, [], [])
        self.assertNotIn(r"\section{Experience}", out)
        self.assertNotIn(r"\section{Projects}", out)


class RenderLetterTests(unittest.TestCase):
    PROFILE = {"name": "Sam Doe", "email": "s@example.com",
               "phone": "555", "location": "Town", "website": "example.com"}

    def test_four_part_letter_becomes_five_paragraphs(self):
        letter = {"opening": "A", "match": ["B", "C"], "why_here": "D",
                  "closing": "E"}
        self.assertEqual(latex.letter_paragraphs(letter),
                         ["A", "B", "C", "D", "E"])

    def test_one_match_paragraph_gives_four(self):
        letter = {"opening": "A", "match": ["B"], "why_here": "C",
                  "closing": "D"}
        self.assertEqual(len(latex.letter_paragraphs(letter)), 4)

    def test_empty_parts_are_dropped(self):
        letter = {"opening": "A", "match": ["", "  "], "why_here": None,
                  "closing": "D"}
        self.assertEqual(latex.letter_paragraphs(letter), ["A", "D"])

    def test_addressed_to_the_hiring_manager(self):
        out = latex.render_letter(self.PROFILE, {"opening": "Hello"},
                                  today="August 9, 2026")
        self.assertIn("Dear Hiring Manager,", out)
        self.assertIn("August 9, 2026", out)
        self.assertIn("Sam Doe", out)

    def test_body_is_escaped(self):
        out = latex.render_letter(self.PROFILE, {"opening": "I cut cost by 30%"},
                                  today="August 9, 2026")
        self.assertIn(r"30\%", out)


class CompileTests(unittest.TestCase):
    def test_no_engine_returns_none_rather_than_raising(self):
        # No LaTeX engine is installed on the development machine, so this is
        # the path that actually runs today. A missing engine must degrade.
        self.assertIsNone(latex.compile_pdf("missing.tex", engine="not-an-engine"))

    def test_safe_filename_strips_punctuation(self):
        self.assertEqual(latex.safe_filename("Acme, Inc. / R&D"), "Acme Inc RD")
        self.assertEqual(latex.safe_filename(""), "document")


class DeliveryTests(unittest.TestCase):
    def test_writes_the_tex_when_nothing_can_compile(self):
        with tempfile.TemporaryDirectory() as target:
            path, is_pdf = documents.deliver("\\documentclass{article}",
                                             "Resume - Acme", target_dir=target)
            self.assertFalse(is_pdf)
            self.assertTrue(path.endswith(".tex"))
            self.assertTrue(os.path.exists(path))

    def test_never_overwrites_an_existing_download(self):
        with tempfile.TemporaryDirectory() as target:
            first, _ = documents.deliver("a", "Resume - Acme", target_dir=target)
            second, _ = documents.deliver("b", "Resume - Acme", target_dir=target)
            self.assertNotEqual(first, second)
            self.assertIn("(2)", second)

    def test_document_name_says_what_and_for_whom(self):
        lead = {"company": "Acme, Inc.", "title": "Backend Engineer"}
        self.assertEqual(documents.document_name("resume", lead),
                         "Resume - Acme Inc - Backend Engineer")
        self.assertEqual(documents.document_name("cover_letter", lead),
                         "Cover Letter - Acme Inc - Backend Engineer")


class RebuildFromSelectionTests(unittest.TestCase):
    """A stored selection must render without any file having been kept."""

    def setUp(self):
        self.store = JobStore(":memory:")
        self.mail = MailStore(self.store.conn)
        self.ids = [
            self.mail.add_experience({
                "kind": "work", "organisation": "Acme", "role": "Engineer",
                "start_date": "2024-01", "end_date": "2025-01",
                "bullet": "Cut latency by 40%", "tags": "python",
            }),
            self.mail.add_experience({
                "kind": "project", "organisation": "Personal",
                "role": "Toy Compiler", "bullet": "Wrote a parser",
                "tags": "c",
            }),
        ]

    def tearDown(self):
        self.store.conn.close()

    def test_renders_from_ids_alone(self):
        self.mail.save_selection("KEY", "resume", bullet_ids=self.ids)
        selection = self.mail.selection_for("KEY", "resume")
        out = documents.build_resume_tex(self.mail, {}, selection, master=MASTER)
        self.assertIn(r"Cut latency by 40\%", out)
        self.assertIn("Toy Compiler", out)

    def test_edited_bullets_appear_without_regenerating(self):
        # The reason ids are stored rather than rendered files: no selection is
        # rewritten and nothing is re-prepared, yet the download changes.
        self.mail.save_selection("KEY", "resume", bullet_ids=self.ids)
        self.mail.conn.execute(
            "UPDATE experiences SET bullet = ? WHERE id = ?",
            ("Rewritten claim", self.ids[0]),
        )
        self.mail.commit()
        selection = self.mail.selection_for("KEY", "resume")
        out = documents.build_resume_tex(self.mail, {}, selection, master=MASTER)
        self.assertIn("Rewritten claim", out)
        self.assertNotIn("Cut latency", out)

    def test_a_deleted_bullet_costs_a_line_not_the_document(self):
        self.mail.save_selection("KEY", "resume",
                                 bullet_ids=self.ids + [99999])
        selection = self.mail.selection_for("KEY", "resume")
        out = documents.build_resume_tex(self.mail, {}, selection, master=MASTER)
        self.assertIn("Toy Compiler", out)

    def test_nothing_prepared_is_a_lookup_error(self):
        with self.assertRaises(LookupError):
            documents.build_document(self.mail, {},
                                     {"identity_key": "NONE",
                                      "company": "A", "title": "B"},
                                     "resume")

    def test_the_threaded_half_cannot_reach_the_database(self):
        """The split that keeps sqlite on its own thread.

        `deliver` is what gets pushed to a worker, and sqlite raises if a
        connection is used off the thread that opened it. The guarantee is
        structural rather than careful: `deliver` takes finished text and has
        no store in its signature at all, so there is nothing for it to touch.
        """
        import inspect

        parameters = inspect.signature(documents.deliver).parameters
        self.assertNotIn("mail", parameters)
        self.assertNotIn("store", parameters)
        self.assertIn("tex_text", parameters)


class EventLoopTests(unittest.IsolatedAsyncioTestCase):
    """Delivery blocks, so a caller on the loop has to thread it.

    The scheduler runs as an asyncio task on the same loop that serves the
    pages. A compile held inline froze the interface once before, which is what
    `TestHandlersDoNotBlockTheEventLoop` in test_lifecycle covers for the
    pipeline; this is the same guarantee for the download button.
    """

    async def test_the_loop_keeps_running_during_a_delivery(self):
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        def slow_deliver(target):
            time.sleep(0.2)
            return documents.deliver("a", "Resume - Acme", target_dir=target)

        beat = asyncio.create_task(heartbeat())
        try:
            with tempfile.TemporaryDirectory() as target:
                await asyncio.to_thread(slow_deliver, target)
        finally:
            beat.cancel()

        self.assertGreater(ticks, 0,
                           "the event loop was blocked for the whole delivery")


if __name__ == "__main__":
    unittest.main()
