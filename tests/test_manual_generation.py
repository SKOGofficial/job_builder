"""Nothing expensive happens until a person asks for it.

Documents used to be built unattended for any lead scoring above 0.45. A
relevance score is a reasonable way to sort a list and a poor way to authorise
a research call: the mailbox that prompted this holds 363 leads and eleven
documents, none of which were asked for.

So the split is now: scoring stays automatic, because it is free and it is what
makes the list rankable; research and letter-writing wait for a click. These
tests pin both halves - that a scheduled cycle writes nothing, and that
`prepare_now` still writes everything.
"""

import asyncio
import json
import tempfile
import unittest

from clients.research_client import ResearchClient
from pipeline.orchestrator import PipelineCycle
from pipeline.prepare import LeadPreparer
from utilities.mailstore import (
    LEAD_NEW,
    LEAD_READY,
    MailStore,
)
from utilities.store import JobStore


async def immediate(fn, *args):
    return fn(*args)


async def _resolved(value):
    return value


class FakeGroq:
    """A scorer that answers from a script."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.model = "fake"

    def complete_json(self, messages, parse, fallback, *args, **kwargs):
        self.calls += 1
        if not self.results:
            return fallback
        return parse(self.results.pop(0))


class CountingResearch:
    """A real `ResearchClient` whose network call is a counter.

    The real client is used rather than a hand-rolled stub because the shape it
    returns - `(payload, input_tokens, output_tokens)` - is exactly the
    coupling these tests need to stay honest about. What is faked is the
    network, and nothing else.
    """

    def __init__(self):
        self.calls = 0
        payload = json.dumps({"company_summary": "Acme builds things.",
                              "posting_keywords": ["python"]})

        def caller(_prompt):
            self.calls += 1
            return (payload, 10, 20)

        self.client = ResearchClient(key="x", caller=caller)


def add_experiences(mail):
    """
    Summary:
        Store the resume bullets the artifact builder selects from.

    Parameters:
        mail (MailStore): The store to write to.

    Note:
        A build with no experiences raises rather than producing an empty
        resume, which is the right behaviour and not what these tests are
        about.
    """
    for entry in (
        {"organisation": "Acme", "role": "Backend Engineer",
         "bullet": "Built a Python service handling 2k requests per second",
         "tags": "python,backend,api",
         "start_date": "2023-01", "end_date": "2025-06"},
        {"organisation": "Beta Co", "role": "Frontend Developer",
         "bullet": "Rebuilt the checkout flow in React",
         "tags": "react,frontend",
         "start_date": "2021-01", "end_date": "2022-12"},
    ):
        mail.add_experience(entry)


def add_lead(mail, identity="acme::engineer", title="Engineer",
             company="Acme"):
    mail.upsert_lead({
        "identity_key": identity,
        "identity_scheme": "title_company",
        "title": title,
        "company": company,
        "apply_url": "https://acme.test/1",
    })
    mail.commit()
    return mail.lead_by_identity(identity)


class ACycleScoresButDoesNotBuildTests(unittest.TestCase):
    def setUp(self):
        self.store = JobStore(":memory:")
        self.mail = MailStore(self.store.conn)

    def tearDown(self):
        self.store.close()

    def preparer(self, research=None):
        return LeadPreparer(
            self.store, self.mail,
            FakeGroq(['{"score": 0.95, "reason": "a squarely perfect fit"}']),
            (research or CountingResearch()).client,
            executor=immediate,
        )

    def test_a_default_pass_scores_and_stops(self):
        add_lead(self.mail)
        research = CountingResearch()

        result = asyncio.run(self.preparer(research).run())

        self.assertEqual(result["scored"], 1)
        self.assertEqual(result["prepared"], 0)
        self.assertEqual(research.calls, 0,
                         "a scheduled pass must not spend a research call")

    def test_a_high_score_is_not_permission_to_build(self):
        # The behaviour this whole change is about: 0.95 clears every threshold
        # the old gate had, and still buys nothing on its own.
        add_lead(self.mail)
        asyncio.run(self.preparer().run())

        lead = self.mail.lead_by_identity("acme::engineer")
        self.assertGreaterEqual(lead["relevance_score"], 0.9)
        self.assertEqual(lead["status"], LEAD_NEW)
        self.assertEqual(self.mail.selections_for("acme::engineer"), [])

    def test_the_score_is_still_written_so_the_list_can_be_ranked(self):
        add_lead(self.mail)
        asyncio.run(self.preparer().run())

        lead = self.mail.lead_by_identity("acme::engineer")
        self.assertIsNotNone(lead["relevance_score"])
        self.assertTrue(lead["relevance_reason"])

    def test_an_explicit_limit_still_builds(self):
        # The capability is not removed, only its default. A supervised
        # catch-up run can still ask for it.
        add_experiences(self.mail)
        add_lead(self.mail)
        research = CountingResearch()
        with tempfile.TemporaryDirectory() as directory:
            preparer = LeadPreparer(
                self.store, self.mail,
                FakeGroq(['{"score": 0.95, "reason": "fit"}']),
                research.client, output_dir=directory, executor=immediate)
            result = asyncio.run(preparer.run(prepare_limit=5))
        self.assertEqual(result["prepared"], 1)
        self.assertEqual(research.calls, 1)


class TheCycleDefaultsToPullTests(unittest.TestCase):
    def setUp(self):
        self.store = JobStore(":memory:")
        self.mail = MailStore(self.store.conn)

    def tearDown(self):
        self.store.close()

    def cycle(self, **kwargs):
        def no_pool():
            raise RuntimeError("no provider configured")

        cycle = PipelineCycle(self.store, self.mail, client_factory=no_pool,
                              **kwargs)
        cycle.sync.run = lambda limit: _resolved(0)
        cycle.bodies.run = lambda limit: _resolved(0)
        return cycle

    def test_auto_prepare_is_off_unless_asked_for(self):
        self.assertFalse(self.cycle().auto_prepare)

    def test_it_can_still_be_turned_on(self):
        self.assertTrue(self.cycle(auto_prepare=True).auto_prepare)

    def test_a_cycle_creates_no_artifacts(self):
        add_lead(self.mail)
        asyncio.run(self.cycle().run())
        self.assertEqual(
            self.store.conn.execute(
                "SELECT COUNT(*) c FROM job_artifacts").fetchone()["c"],
            0,
        )


class PrepareNowIgnoresTheThresholdTests(unittest.TestCase):
    """Asking for a role *is* the judgement the score was standing in for."""

    def setUp(self):
        self.store = JobStore(":memory:")
        self.mail = MailStore(self.store.conn)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def tearDown(self):
        self.store.close()

    def test_a_lead_scored_far_below_the_bar_can_still_be_generated(self):
        add_experiences(self.mail)
        lead = add_lead(self.mail)
        self.mail.set_lead_relevance(lead["id"], 0.01, "wrong field entirely")
        self.mail.commit()

        research = CountingResearch()
        preparer = LeadPreparer(
            self.store, self.mail, FakeGroq([]), research.client,
            output_dir=self.directory.name, executor=immediate)

        self.assertTrue(asyncio.run(preparer.prepare_now(lead["id"])))
        self.assertEqual(research.calls, 1)
        self.assertEqual(
            self.mail.lead(lead["id"])["status"], LEAD_READY)

    def test_a_rate_limit_leaves_the_lead_clickable_again(self):
        """The bug the first real click found.

        `prepare` sets `preparing` itself and restores the status the lead
        arrived with when a provider is busy. The Leads page was writing
        `preparing` *before* calling it, so "restore" meant "leave it at
        preparing" - and against a research provider failing 89% of the time,
        the first lead anyone generated stuck there permanently, showing a
        Generate button that could no longer be reached.
        """
        from clients.providers.base import ProviderRateLimited

        add_experiences(self.mail)
        lead = add_lead(self.mail)

        class Busy:
            def available_in(self):
                return 0.0

            def research(self, *args, **kwargs):
                raise ProviderRateLimited("busy", retry_after=60,
                                          provider="Claude")

        preparer = LeadPreparer(
            self.store, self.mail, FakeGroq([]), Busy(),
            output_dir=self.directory.name, executor=immediate)

        self.assertFalse(asyncio.run(preparer.prepare_now(lead["id"])))
        after = self.mail.lead(lead["id"])
        self.assertEqual(after["status"], LEAD_NEW)
        self.assertIn("Waiting for a model", after["prepare_error"])

    def test_an_unknown_lead_is_a_clean_false(self):
        preparer = LeadPreparer(
            self.store, self.mail, FakeGroq([]), CountingResearch().client,
            output_dir=self.directory.name, executor=immediate)
        self.assertFalse(asyncio.run(preparer.prepare_now(9999)))


class GeneratedLeadsSurviveTheFreshnessPurgeTests(unittest.TestCase):
    """Deleting a role you asked for documents about is losing your work."""

    def setUp(self):
        self.store = JobStore(":memory:")
        self.mail = MailStore(self.store.conn)

    def tearDown(self):
        self.store.close()

    def age(self, identity, days):
        import time

        self.mail.conn.execute(
            "UPDATE job_leads SET posted_ts = ? WHERE identity_key = ?",
            (int(time.time() - days * 86400), identity))
        self.mail.commit()

    def test_a_stale_lead_with_no_documents_is_still_purged(self):
        add_lead(self.mail, identity="old::role")
        self.age("old::role", 60)
        self.assertEqual(self.mail.purge_stale_leads(14), 1)

    def test_a_stale_lead_with_documents_is_kept(self):
        add_lead(self.mail, identity="chosen::role")
        self.age("chosen::role", 60)
        self.mail.save_selection("chosen::role", "resume", bullet_ids=[1, 2])
        self.mail.commit()

        self.assertEqual(self.mail.purge_stale_leads(14), 0)
        self.assertIsNotNone(self.mail.lead_by_identity("chosen::role"))


if __name__ == "__main__":
    unittest.main()
