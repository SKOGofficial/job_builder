"""Research, relevance scoring, and artifact generation.

No network anywhere: the research client takes an injectable `caller` and the
Groq client is a scripted fake, the same discipline
`tests/test_llm_classification.py` already uses.

The behaviours worth guarding here are the ones that cost money or produce a
broken to-apply list: the spend ceiling actually stops calls, a lead whose
generation failed never reaches `ready`, and bullet selection degrades to
something sensible when research produced no keywords.
"""

import json
import os
import tempfile
import unittest

from clients.research_client import (
    ResearchClient,
    SpendCeilingReached,
    SpendLimiter,
    parse_research,
)
from pipeline.generate import (
    ArtifactBuilder,
    render_html,
    render_markdown,
    score_bullet,
    select_bullets,
)
from pipeline.prepare import LeadPreparer
from pipeline.relevance import RelevanceScorer, parse_score
from utilities.identity import identity_key
from utilities.mailstore import LEAD_NEW, LEAD_READY, MailStore
from utilities.store import JobStore


class FakeGroq:
    def __init__(self, replies=()):
        self.replies = list(replies)

    def complete_json(self, messages, parser, fallback, max_tokens=200):
        if not self.replies:
            return fallback
        return parser(self.replies.pop(0))


async def immediate(func, *args):
    """Executor stand-in that calls straight through, no thread involved."""
    return func(*args)


def make_app():
    store = JobStore(":memory:")
    return store, MailStore(store.conn)


def add_lead(mail, title="Backend Engineer", company="Stripe",
             location="Remote", status=LEAD_NEW):
    key = identity_key(title, company, location)
    mail.upsert_lead({"identity_key": key, "title": title, "company": company,
                      "location": location, "status": status,
                      "apply_url": "https://example.com/job/1"})
    mail.commit()
    return mail.lead_by_identity(key)


def add_experiences(mail):
    for entry in (
        {"organisation": "Acme", "role": "Backend Engineer",
         "bullet": "Built a Python service handling 2k requests per second",
         "tags": "python,backend,api", "start_date": "2023-01", "end_date": "2025-06"},
        {"organisation": "Acme", "role": "Backend Engineer",
         "bullet": "Migrated a Postgres cluster with no downtime",
         "tags": "postgres,databases", "start_date": "2023-01", "end_date": "2025-06"},
        {"organisation": "Beta Co", "role": "Frontend Developer",
         "bullet": "Rebuilt the checkout flow in React",
         "tags": "react,frontend", "start_date": "2021-01", "end_date": "2022-12"},
    ):
        mail.add_experience(entry)


class TestResearchParsing(unittest.TestCase):
    def test_valid_payload(self):
        payload = parse_research(json.dumps({
            "company_summary": "Payments infrastructure.",
            "products": ["Payments API"],
            "tech_stack": ["Ruby", "Go"],
            "recent_news": ["Raised a round"],
            "posting_keywords": ["python", "distributed systems"],
            "culture_notes": ["Writes a lot of docs"],
            "tailoring_advice": "Emphasise scale.",
        }))
        self.assertEqual(payload["tech_stack"], ["Ruby", "Go"])
        self.assertEqual(payload["posting_keywords"], ["python", "distributed systems"])

    def test_fenced_json_is_tolerated(self):
        payload = parse_research('```json\n{"company_summary": "Hi"}\n```')
        self.assertEqual(payload["company_summary"], "Hi")

    def test_garbage_yields_empty(self):
        self.assertEqual(parse_research("not json"), {})
        self.assertEqual(parse_research(""), {})
        self.assertEqual(parse_research("[1,2,3]"), {})

    def test_non_string_list_items_are_dropped(self):
        payload = parse_research(json.dumps({"products": ["ok", 42, None]}))
        self.assertEqual(payload["products"], ["ok"])


class TestSpendLimiter(unittest.TestCase):
    def test_allows_when_under_budget(self):
        _, mail = make_app()
        SpendLimiter(mail, ceiling=1000).check()  # must not raise

    def test_blocks_once_spent(self):
        _, mail = make_app()
        mail.save_research("KEY1", "s", {}, model="m",
                           input_tokens=5000, output_tokens=1200)
        limiter = SpendLimiter(mail, ceiling=1000)
        self.assertEqual(limiter.remaining(), 0)
        with self.assertRaises(SpendCeilingReached):
            limiter.check()

    def test_research_client_respects_the_ceiling(self):
        # The guard that stops a parser bug turning into a month's budget.
        _, mail = make_app()
        mail.save_research("KEY1", "s", {}, output_tokens=99999, input_tokens=1)
        called = []

        def caller(prompt):
            called.append(prompt)
            return "{}", 10, 10

        client = ResearchClient(key="x", caller=caller,
                                limiter=SpendLimiter(mail, ceiling=1000))
        with self.assertRaises(SpendCeilingReached):
            client.research({"title": "t", "company": "c", "location": None,
                             "apply_url": None})
        self.assertEqual(called, [], "no call may be made once the budget is gone")

    def test_spend_is_read_back_from_the_database(self):
        # Survives a restart, unlike an in-memory counter - which matters
        # because a restart is exactly when a runaway loop reappears.
        _, mail = make_app()
        mail.save_research("A", "s", {}, input_tokens=1, output_tokens=400)
        mail.save_research("B", "s", {}, input_tokens=1, output_tokens=300)
        self.assertEqual(SpendLimiter(mail, ceiling=1000).remaining(), 300)


class TestRelevance(unittest.IsolatedAsyncioTestCase):
    def test_parse_score_clamps(self):
        self.assertEqual(parse_score('{"score": 5, "reason": "x"}')["score"], 1.0)
        self.assertEqual(parse_score('{"score": -2, "reason": "x"}')["score"], 0.0)

    def test_parse_score_rejects_garbage(self):
        self.assertIsNone(parse_score("not json")["score"])
        self.assertIsNone(parse_score('{"score": "high"}')["score"])

    async def test_scoring_stores_score_and_reason(self):
        store, mail = make_app()
        lead = add_lead(mail)
        scorer = RelevanceScorer(store, mail,
                                 FakeGroq(['{"score": 0.82, "reason": "python backend"}']),
                                 executor=immediate)
        self.assertEqual(await scorer.score_lead(lead), 0.82)

        refreshed = mail.lead(lead["id"])
        self.assertAlmostEqual(refreshed["relevance_score"], 0.82)
        self.assertEqual(refreshed["relevance_reason"], "python backend")

    async def test_unscorable_lead_is_left_for_a_retry(self):
        # Baking in a wrong score would suppress the lead forever.
        store, mail = make_app()
        lead = add_lead(mail)
        scorer = RelevanceScorer(store, mail, FakeGroq(["not json"]),
                                 executor=immediate)
        self.assertIsNone(await scorer.score_lead(lead))
        self.assertIsNone(mail.lead(lead["id"])["relevance_score"])

    def test_gate_selects_only_leads_above_the_bar(self):
        store, mail = make_app()
        good = add_lead(mail, "Backend Engineer", "Stripe")
        poor = add_lead(mail, "Warehouse Operative", "LogisticsCo")
        mail.set_lead_relevance(good["id"], 0.8, "fit")
        mail.set_lead_relevance(poor["id"], 0.1, "wrong field")

        scorer = RelevanceScorer(store, mail, None, threshold=0.45)
        selected = [row["title"] for row in scorer.worth_preparing()]
        self.assertEqual(selected, ["Backend Engineer"])

    async def test_no_client_scores_nothing_rather_than_failing(self):
        store, mail = make_app()
        add_lead(mail)
        self.assertEqual(await RelevanceScorer(store, mail, None).run(), 0)


class TestBulletSelection(unittest.TestCase):
    def test_tags_outweigh_prose(self):
        _, mail = make_app()
        add_experiences(mail)
        rows = mail.list_experiences()
        tagged = next(r for r in rows if "python" in (r["tags"] or ""))
        untagged = next(r for r in rows if "react" in (r["tags"] or ""))
        self.assertGreater(score_bullet(tagged, ["python"]),
                           score_bullet(untagged, ["python"]))

    def test_relevant_bullets_come_first(self):
        _, mail = make_app()
        add_experiences(mail)
        chosen = select_bullets(mail.list_experiences(), ["python", "api"], limit=2)
        self.assertIn("Python service", chosen[0]["bullet"])

    def test_no_keywords_falls_back_to_recency(self):
        # Research can fail or a posting can be vague. A reasonable resume
        # beats an empty one.
        _, mail = make_app()
        add_experiences(mail)
        chosen = select_bullets(mail.list_experiences(), [], limit=3)
        self.assertEqual(len(chosen), 3)
        self.assertEqual(chosen[0]["organisation"], "Acme")

    def test_limit_is_respected(self):
        _, mail = make_app()
        add_experiences(mail)
        self.assertEqual(len(select_bullets(mail.list_experiences(), ["python"], 1)), 1)


class TestRendering(unittest.TestCase):
    def setUp(self):
        self.store, self.mail = make_app()
        add_experiences(self.mail)
        self.lead = add_lead(self.mail)
        self.bullets = self.mail.list_experiences()
        self.profile = {"name": "Sam Doe", "email": "sam@example.com"}

    def test_markdown_contains_the_essentials(self):
        text = render_markdown(self.profile, self.lead, self.bullets,
                               {"posting_keywords": ["python"],
                                "tailoring_advice": "Emphasise scale."})
        self.assertIn("Sam Doe", text)
        self.assertIn("Backend Engineer", text)
        self.assertIn("Emphasise scale.", text)
        self.assertIn("Python service", text)

    def test_html_escapes_untrusted_text(self):
        # Company and title come from an email, so they are untrusted.
        lead = add_lead(self.mail, title="Engineer <script>alert(1)</script>",
                        company="Evil & Co")
        page = render_html(self.profile, lead, self.bullets)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("Evil &amp; Co", page)

    def test_renders_without_research(self):
        text = render_markdown(self.profile, self.lead, self.bullets, None)
        self.assertIn("Experience", text)


class TestArtifactBuilder(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store, self.mail = make_app()
        add_experiences(self.mail)
        self.store.save_profile_value("name", "Sam Doe")
        self.lead = add_lead(self.mail)
        self.directory = tempfile.mkdtemp()

    def _client(self):
        payload = json.dumps({
            "company_summary": "Payments.",
            "posting_keywords": ["python", "api"],
            "tailoring_advice": "Lead with scale.",
        })
        return ResearchClient(key="x", model="claude-opus-5",
                              caller=lambda prompt: (payload, 100, 200))

    async def test_records_a_bullet_selection(self):
        builder = ArtifactBuilder(self.store, self.mail, self._client(),
                                  output_dir=self.directory, executor=immediate)
        result = await builder.build(self.lead)

        self.assertTrue(result["bullet_ids"])
        stored = self.mail.selection_for(self.lead["identity_key"], "resume")
        self.assertEqual(stored["bullet_ids"], result["bullet_ids"])
        # Every id must resolve; a selection naming a row that is not there
        # would render a resume quietly missing a bullet.
        known = {row["id"] for row in self.mail.list_experiences()}
        self.assertTrue(set(stored["bullet_ids"]) <= known)

    async def test_writes_no_files(self):
        # The whole point of storing a selection: nothing on disk to go stale
        # when an experience bullet is edited.
        builder = ArtifactBuilder(self.store, self.mail, self._client(),
                                  output_dir=self.directory, executor=immediate)
        await builder.build(self.lead)
        self.assertEqual(os.listdir(self.directory), [])

    async def test_selections_are_keyed_on_identity(self):
        # Not on job_id: a lead has none until promotion, and keying on the
        # identity means nothing moves when it is promoted.
        builder = ArtifactBuilder(self.store, self.mail, self._client(),
                                  output_dir=self.directory, executor=immediate)
        await builder.build(self.lead)
        rows = self.mail.selections_for(self.lead["identity_key"])
        self.assertEqual({row["kind"] for row in rows}, {"resume"})

    async def test_research_is_cached_not_repeated(self):
        calls = []

        def caller(prompt):
            calls.append(prompt)
            return json.dumps({"posting_keywords": ["python"]}), 10, 20

        client = ResearchClient(key="x", caller=caller)
        builder = ArtifactBuilder(self.store, self.mail, client,
                                  output_dir=self.directory, executor=immediate)
        await builder.build(self.lead)
        await builder.build(self.lead)
        self.assertEqual(len(calls), 1, "second build must reuse stored research")

    async def test_no_experiences_is_a_clear_error(self):
        store, mail = make_app()
        lead = add_lead(mail)
        builder = ArtifactBuilder(store, mail, self._client(),
                                  output_dir=self.directory, executor=immediate)
        with self.assertRaises(ValueError) as caught:
            await builder.build(lead)
        self.assertIn("experience", str(caught.exception).lower())


class TestLeadPreparer(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store, self.mail = make_app()
        add_experiences(self.mail)
        self.directory = tempfile.mkdtemp()

    def _research(self):
        payload = json.dumps({"posting_keywords": ["python"]})
        return ResearchClient(key="x", caller=lambda p: (payload, 10, 20))

    async def test_scores_then_prepares(self):
        add_lead(self.mail)
        preparer = LeadPreparer(
            self.store, self.mail,
            FakeGroq(['{"score": 0.9, "reason": "great fit"}']),
            self._research(), output_dir=self.directory, executor=immediate)
        result = await preparer.run()
        self.assertEqual(result["scored"], 1)
        self.assertEqual(result["prepared"], 1)

        leads = self.mail.list_leads()
        self.assertEqual(leads[0]["status"], LEAD_READY)

    async def test_low_scoring_lead_is_not_prepared(self):
        add_lead(self.mail)
        preparer = LeadPreparer(
            self.store, self.mail,
            FakeGroq(['{"score": 0.05, "reason": "wrong field"}']),
            self._research(), output_dir=self.directory, executor=immediate)
        result = await preparer.run()
        self.assertEqual(result["prepared"], 0)
        self.assertEqual(self.mail.list_leads()[0]["status"], LEAD_NEW)

    async def test_failed_generation_never_reaches_ready(self):
        # A ready lead whose resume link is dead is worse than no lead.
        store, mail = make_app()  # no experiences -> build raises
        add_lead(mail)
        preparer = LeadPreparer(
            store, mail, FakeGroq(['{"score": 0.9, "reason": "fit"}']),
            self._research(), output_dir=self.directory, executor=immediate)
        result = await preparer.run()

        self.assertEqual(result["failed"], 1)
        lead = mail.list_leads()[0]
        self.assertNotEqual(lead["status"], LEAD_READY)
        self.assertTrue(lead["prepare_error"])

    async def test_prepare_now_bypasses_the_gate(self):
        # The escape hatch for a threshold set slightly wrong.
        lead = add_lead(self.mail)
        self.mail.set_lead_relevance(lead["id"], 0.01, "scored too low")
        preparer = LeadPreparer(self.store, self.mail, None, self._research(),
                                output_dir=self.directory, executor=immediate)
        self.assertTrue(await preparer.prepare_now(lead["id"]))
        self.assertEqual(self.mail.lead(lead["id"])["status"], LEAD_READY)

    async def test_budget_exhaustion_stops_cleanly(self):
        add_lead(self.mail, "Backend Engineer", "Stripe")
        add_lead(self.mail, "Platform Engineer", "Stripe")
        self.mail.save_research("SPENT", "s", {}, input_tokens=1,
                                output_tokens=999999)
        research = ResearchClient(key="x", caller=lambda p: ("{}", 1, 1),
                                  limiter=SpendLimiter(self.mail, ceiling=1000))
        preparer = LeadPreparer(
            self.store, self.mail,
            FakeGroq(['{"score": 0.9, "reason": "fit"}',
                      '{"score": 0.9, "reason": "fit"}']),
            research, output_dir=self.directory, executor=immediate)
        result = await preparer.run()

        self.assertEqual(result["prepared"], 0)
        self.assertEqual(result["failed"], 0, "budget stop is not a lead failure")


if __name__ == "__main__":
    unittest.main()
