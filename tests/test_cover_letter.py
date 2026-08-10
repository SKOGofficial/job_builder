"""The covering letter: what the model is given, and what it may not invent.

The load-bearing property is that the letter argues only from experience the
user actually stored. A fluent letter naming a project they never worked on is
worse than no letter, because they find out in the interview.
"""

import json
import unittest

from pipeline import cover_letter
from utilities.mailstore import MailStore
from utilities.store import JobStore

LEAD = {"title": "Backend Engineer", "company": "Acme",
        "location": "Remote", "identity_key": "KEY"}

RESEARCH = {
    "mission": "Make payments boring.",
    "company_summary": "Acme processes payments.",
    "products": ["Acme Pay"],
    "recent_news": ["Series B, March 2026"],
    "culture_notes": ["Ships on Fridays"],
    "requirements": ["Strong Python", "Experience with distributed systems"],
    "nice_to_haves": ["Rust"],
}


class ScriptedClient:
    """Returns a queued reply, like the doubles in the other suites."""

    def __init__(self, reply):
        self.reply = reply
        self.messages = None

    def complete_json(self, messages, parser, fallback, max_tokens=200):
        self.messages = messages
        return parser(self.reply)


def bullets():
    return [
        {"id": 1, "organisation": "Acme", "role": "Engineer",
         "bullet": "Built a Python service handling 2k requests per second",
         "tags": "python,backend"},
        {"id": 2, "organisation": "Beta", "role": "Engineer",
         "bullet": "Sharded a distributed queue across ten nodes",
         "tags": "distributed systems,scale"},
        {"id": 3, "organisation": "Gamma", "role": "Designer",
         "bullet": "Redrew the checkout illustrations",
         "tags": "illustration,design"},
    ]


class MappingTests(unittest.TestCase):
    def test_each_requirement_gets_its_matching_bullet(self):
        mapping = cover_letter.build_mapping(RESEARCH["requirements"], bullets())
        pairs = {p["requirement"]: [b["id"] for b in p["bullets"]] for p in mapping}
        self.assertIn(1, pairs["Strong Python"])
        self.assertIn(2, pairs["Experience with distributed systems"])

    def test_an_unanswered_requirement_is_dropped(self):
        # Better silence than a paragraph drawing attention to the gap.
        mapping = cover_letter.build_mapping(
            ["Underwater basket weaving certification"], bullets())
        self.assertEqual(mapping, [])

    def test_irrelevant_bullets_stay_out(self):
        mapping = cover_letter.build_mapping(["Strong Python"], bullets())
        matched = [b["id"] for b in mapping[0]["bullets"]]
        self.assertNotIn(3, matched, "the illustration bullet is not evidence "
                                     "of Python")

    def test_requirements_are_capped(self):
        many = [f"Requirement {n} python" for n in range(20)]
        mapping = cover_letter.build_mapping(many, bullets())
        self.assertLessEqual(len(mapping), cover_letter.MAX_REQUIREMENTS)

    def test_bullets_per_requirement_are_capped(self):
        mapping = cover_letter.build_mapping(["python backend distributed"],
                                             bullets())
        self.assertLessEqual(len(mapping[0]["bullets"]),
                             cover_letter.BULLETS_PER_REQUIREMENT)


class PromptTests(unittest.TestCase):
    def test_the_prompt_carries_only_real_bullets(self):
        """The guardrail: nothing reaches the model that is not stored.

        If a future prompt change let the model supply its own evidence, this
        is what would catch it.
        """
        store = JobStore(":memory:")
        mail = MailStore(store.conn)
        for row in bullets():
            mail.add_experience({"kind": "work", "bullet": row["bullet"],
                                 "tags": row["tags"],
                                 "organisation": row["organisation"],
                                 "role": row["role"]})
        stored = {r["bullet"] for r in mail.list_experiences()}

        rows = [dict(r) for r in mail.list_experiences()]
        mapping = cover_letter.build_mapping(RESEARCH["requirements"], rows)
        prompt = cover_letter.build_prompt("I am an engineer.", LEAD, RESEARCH,
                                           mapping)

        for pair in mapping:
            for row in pair["bullets"]:
                self.assertIn(row["bullet"], stored)
                self.assertIn(row["bullet"], prompt)
        store.conn.close()

    def test_the_prompt_carries_the_company_research(self):
        mapping = cover_letter.build_mapping(RESEARCH["requirements"], bullets())
        prompt = cover_letter.build_prompt("", LEAD, RESEARCH, mapping)
        self.assertIn("Make payments boring.", prompt)
        self.assertIn("Series B, March 2026", prompt)
        self.assertIn("Backend Engineer", prompt)

    def test_the_prompt_carries_the_users_positioning(self):
        prompt = cover_letter.build_prompt("I build autonomous systems.", LEAD,
                                           RESEARCH, [])
        self.assertIn("I build autonomous systems.", prompt)

    def test_missing_research_fields_are_simply_absent(self):
        prompt = cover_letter.build_prompt("", LEAD, {}, [])
        self.assertIn("Backend Engineer", prompt)
        self.assertNotIn("None", prompt)


class ParseTests(unittest.TestCase):
    def test_a_full_letter_round_trips(self):
        letter = cover_letter.parse_letter(json.dumps({
            "opening": "A", "match": ["B", "C"], "why_here": "D", "closing": "E",
        }))
        self.assertEqual(letter["match"], ["B", "C"])
        self.assertEqual(letter["opening"], "A")

    def test_match_is_capped_at_two_paragraphs(self):
        # Four or five paragraphs by construction, not by asking politely.
        letter = cover_letter.parse_letter(json.dumps({
            "opening": "A", "match": ["B", "C", "D", "E"], "why_here": "F",
            "closing": "G",
        }))
        self.assertEqual(len(letter["match"]), 2)

    def test_a_single_string_match_is_accepted(self):
        letter = cover_letter.parse_letter(json.dumps({"match": "just one"}))
        self.assertEqual(letter["match"], ["just one"])

    def test_garbage_degrades_to_an_empty_letter(self):
        for bad in ("not json", "[]", '"a string"', ""):
            letter = cover_letter.parse_letter(bad)
            self.assertEqual(letter["opening"], "")
            self.assertEqual(letter["match"], [])


class WriteTests(unittest.TestCase):
    def _reply(self):
        return json.dumps({
            "opening": "I am applying for the Backend Engineer role.",
            "match": ["I have built Python services.",
                      "I have sharded distributed queues."],
            "why_here": "Acme makes payments boring, which I like.",
            "closing": "I would welcome a conversation.",
        })

    def test_produces_four_or_five_paragraphs(self):
        from pipeline.latex import letter_paragraphs

        client = ScriptedClient(self._reply())
        mapping = cover_letter.build_mapping(RESEARCH["requirements"], bullets())
        letter = cover_letter.write_letter(client, "", LEAD, RESEARCH, mapping)
        self.assertIn(len(letter_paragraphs(letter)), (4, 5))

    def test_the_system_prompt_forbids_invention(self):
        client = ScriptedClient(self._reply())
        cover_letter.write_letter(client, "", LEAD, RESEARCH, [])
        system = client.messages[0]["content"]
        self.assertIn("Use only the experience given to you", system)

    def test_a_model_returning_nothing_gives_an_empty_letter(self):
        client = ScriptedClient("not json at all")
        letter = cover_letter.write_letter(client, "", LEAD, RESEARCH, [])
        self.assertEqual(letter["opening"], "")


if __name__ == "__main__":
    unittest.main()
