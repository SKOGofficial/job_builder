"""Tests for Groq configuration, prompting, pacing, and the classification cycle.

Nothing here reaches the network. The HTTP call is injected as `poster`, the
pacer's clock and sleep are injected, and the runner is driven by a stub client,
so the whole cycle including its worker thread runs offline and instantly.

Store tests use a temporary SQLite database, so the real job_applications.sqlite3
is never touched.
"""

import json
import os
import tempfile
import unittest

import app
import clients.llm_client as llm
from utilities import credentials


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


def completion(content):
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"total_tokens": 850},
    }


class EnvIsolationMixin:
    """Keeps tests off the developer's real .env and keyring."""

    GROQ_VARS = (
        "GROQ_API_KEY",
        "GROQ_MODEL",
        "GROQ_REQUESTS_PER_MINUTE",
        "GROQ_CONFIDENCE_THRESHOLD",
    )

    def isolate_env(self):
        self.saved_env = {name: os.environ.get(name) for name in self.GROQ_VARS}
        for name in self.GROQ_VARS:
            os.environ.pop(name, None)
        # _load_env would otherwise pull in the real .env.
        self.saved_loader = llm.load_dotenv
        llm.load_dotenv = None
        # Keep the developer's real credential store out of the tests.
        self.saved_keyring = credentials.keyring
        credentials.keyring = None
        self.addCleanup(self.restore_env)

    def restore_env(self):
        for name, value in self.saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        llm.load_dotenv = self.saved_loader
        credentials.keyring = self.saved_keyring


class FakeKeyring:
    def __init__(self, value=None):
        self.value = value

    def get_password(self, service, username):
        return self.value

    def set_password(self, service, username, value):
        self.value = value

    def delete_password(self, service, username):
        self.value = None


class FailingKeyring:
    """A machine where keyring is installed but no backend answers."""

    def get_password(self, service, username):
        raise credentials.KeyringError("No recommended backend was available")

    def set_password(self, service, username, value):
        raise credentials.KeyringError("No recommended backend was available")

    def delete_password(self, service, username):
        raise credentials.KeyringError("No recommended backend was available")


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, credentials, "keyring", credentials.keyring)

    def test_read_reports_nothing_when_no_backend_answers(self):
        credentials.keyring = FailingKeyring()
        self.assertIsNone(credentials.read_secret("service", "user"))

    def test_read_reports_nothing_when_the_package_is_missing(self):
        credentials.keyring = None
        self.assertIsNone(credentials.read_secret("service", "user"))

    def test_backend_available_is_false_without_a_backend(self):
        credentials.keyring = FailingKeyring()
        self.assertFalse(credentials.backend_available())
        credentials.keyring = None
        self.assertFalse(credentials.backend_available())

    def test_write_raises_so_a_lost_secret_is_never_silent(self):
        credentials.keyring = FailingKeyring()
        with self.assertRaises(credentials.CredentialStoreUnavailable):
            credentials.write_secret("service", "user", "value")
        credentials.keyring = None
        with self.assertRaises(credentials.CredentialStoreUnavailable):
            credentials.write_secret("service", "user", "value")

    def test_delete_reports_false_without_a_backend(self):
        credentials.keyring = FailingKeyring()
        self.assertFalse(credentials.delete_secret("service", "user"))

    def test_round_trip_with_a_working_backend(self):
        credentials.keyring = FakeKeyring()
        credentials.write_secret("service", "user", "secret")
        self.assertEqual(credentials.read_secret("service", "user"), "secret")
        self.assertTrue(credentials.delete_secret("service", "user"))
        self.assertIsNone(credentials.read_secret("service", "user"))
        self.assertFalse(credentials.delete_secret("service", "user"))


class ConfigTests(EnvIsolationMixin, unittest.TestCase):
    def setUp(self):
        self.isolate_env()

    def test_key_comes_from_env_when_keyring_is_empty(self):
        os.environ["GROQ_API_KEY"] = "env-key"
        self.assertEqual(llm.api_key(), "env-key")

    def test_keyring_wins_over_env(self):
        # The stored key is the real credential, so it takes precedence.
        credentials.keyring = FakeKeyring("keyring-key")
        os.environ["GROQ_API_KEY"] = "env-key"
        self.assertEqual(llm.api_key(), "keyring-key")

    def test_env_is_used_when_the_credential_store_is_unusable(self):
        # The CI case: keyring installed, nothing behind it. Reading the key
        # must fall through to .env instead of taking the page down.
        credentials.keyring = FailingKeyring()
        os.environ["GROQ_API_KEY"] = "env-key"
        self.assertEqual(llm.api_key(), "env-key")
        self.assertTrue(llm.is_configured())

    def test_unconfigured_without_keyring_or_env_does_not_raise_upward(self):
        credentials.keyring = FailingKeyring()
        self.assertFalse(llm.is_configured())

    def test_placeholder_key_counts_as_unconfigured(self):
        # Copying .env.example without editing it must not send a junk key.
        os.environ["GROQ_API_KEY"] = llm.PLACEHOLDER_KEY
        with self.assertRaises(llm.GroqNotConfigured):
            llm.api_key()

    def test_missing_key_raises(self):
        with self.assertRaises(llm.GroqNotConfigured):
            llm.api_key()

    def test_is_configured_reflects_key_presence(self):
        self.assertFalse(llm.is_configured())
        os.environ["GROQ_API_KEY"] = "env-key"
        self.assertTrue(llm.is_configured())

    def test_defaults_apply_when_env_is_absent_or_junk(self):
        self.assertEqual(llm.model_name(), llm.DEFAULT_MODEL)
        self.assertEqual(llm.requests_per_minute(), llm.DEFAULT_REQUESTS_PER_MINUTE)
        os.environ["GROQ_REQUESTS_PER_MINUTE"] = "not-a-number"
        self.assertEqual(llm.requests_per_minute(), llm.DEFAULT_REQUESTS_PER_MINUTE)
        os.environ["GROQ_REQUESTS_PER_MINUTE"] = "-5"
        self.assertEqual(llm.requests_per_minute(), llm.DEFAULT_REQUESTS_PER_MINUTE)

    def test_overrides_are_read(self):
        os.environ["GROQ_MODEL"] = "some-other-model"
        os.environ["GROQ_REQUESTS_PER_MINUTE"] = "5"
        os.environ["GROQ_CONFIDENCE_THRESHOLD"] = "0.5"
        self.assertEqual(llm.model_name(), "some-other-model")
        self.assertEqual(llm.requests_per_minute(), 5)
        self.assertEqual(llm.confidence_threshold(), 0.5)

    def test_confidence_threshold_never_exceeds_one(self):
        os.environ["GROQ_CONFIDENCE_THRESHOLD"] = "5"
        self.assertEqual(llm.confidence_threshold(), 1.0)


class PromptTests(unittest.TestCase):
    def match(self, **overrides):
        base = {
            "sender": "Careers <careers@acme.com>",
            "subject": "Your application",
            "body": "We would like to invite you to interview.",
            "company": "Acme",
            "position_title": "Engineer",
        }
        base.update(overrides)
        return base

    def test_body_is_truncated_before_sending(self):
        # Untruncated bodies run to 20,000 characters, which alone would blow
        # the per-minute token ceiling.
        messages = llm.build_messages(self.match(body="x" * 50000))
        self.assertLessEqual(
            messages[1]["content"].count("x"), llm.MODEL_BODY_CHARS
        )

    def test_email_is_fenced_as_data(self):
        content = llm.build_messages(self.match())[1]["content"]
        self.assertIn("<email>", content)
        self.assertIn("</email>", content)

    def test_system_prompt_refuses_instructions_in_the_email(self):
        system = llm.build_messages(self.match())[0]["content"]
        self.assertIn("untrusted", system)
        self.assertIn("never instructions", system)

    def test_company_context_is_included(self):
        content = llm.build_messages(self.match())[1]["content"]
        self.assertIn("Acme", content)
        self.assertIn("Engineer", content)

    def test_missing_fields_do_not_crash(self):
        messages = llm.build_messages({})
        self.assertEqual(len(messages), 2)


class ParseTests(unittest.TestCase):
    def test_valid_reply(self):
        result = llm.parse_classification(
            '{"label": "Rejected", "confidence": 0.91, "reason": "They declined."}'
        )
        self.assertEqual(result["label"], "Rejected")
        self.assertAlmostEqual(result["confidence"], 0.91)
        self.assertEqual(result["reason"], "They declined.")

    def test_unknown_label_becomes_unclear(self):
        result = llm.parse_classification('{"label": "Hired", "confidence": 1.0}')
        self.assertEqual(result["label"], "Unclear")
        self.assertEqual(result["confidence"], 0.0)

    def test_injected_label_cannot_smuggle_a_status(self):
        # A crafted email steering the model still has to produce an exact enum
        # member. Case variants and appended text are rejected outright, so a
        # near-miss can never reach apply_ai_status.
        for smuggled in ("Offer; ignore previous", "offer", "OFFER", "Offer\nRejected"):
            with self.subTest(label=smuggled):
                reply = json.dumps({"label": smuggled, "confidence": 0.99})
                result = llm.parse_classification(reply)
                self.assertEqual(result["label"], "Unclear")
                self.assertEqual(result["confidence"], 0.0)

    def test_surrounding_whitespace_is_tolerated(self):
        reply = json.dumps({"label": "  Offer  ", "confidence": 0.9})
        self.assertEqual(llm.parse_classification(reply)["label"], "Offer")

    def test_non_json_becomes_unclear(self):
        self.assertEqual(llm.parse_classification("not json at all")["label"], "Unclear")
        self.assertEqual(llm.parse_classification(None)["label"], "Unclear")

    def test_json_that_is_not_an_object_becomes_unclear(self):
        self.assertEqual(llm.parse_classification("[1, 2, 3]")["label"], "Unclear")

    def test_confidence_is_clamped_and_coerced(self):
        self.assertEqual(
            llm.parse_classification('{"label": "Offer", "confidence": 5}')["confidence"], 1.0
        )
        self.assertEqual(
            llm.parse_classification('{"label": "Offer", "confidence": -2}')["confidence"], 0.0
        )
        self.assertEqual(
            llm.parse_classification('{"label": "Offer", "confidence": "high"}')["confidence"],
            0.0,
        )

    def test_reason_is_truncated(self):
        result = llm.parse_classification(
            '{"label": "Offer", "confidence": 0.9, "reason": "%s"}' % ("y" * 500)
        )
        self.assertEqual(len(result["reason"]), 200)


class RetryAfterTests(unittest.TestCase):
    def test_reads_the_header(self):
        self.assertEqual(
            llm.retry_after_seconds(FakeResponse(headers={"retry-after": "30"})), 30
        )

    def test_falls_back_when_absent_or_junk(self):
        self.assertEqual(llm.retry_after_seconds(FakeResponse()), 60)
        self.assertEqual(
            llm.retry_after_seconds(FakeResponse(headers={"retry-after": "soon"})), 60
        )


class PacerTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def pacer(self, per_minute=12, tokens_per_minute=12000):
        return llm.Pacer(
            per_minute=per_minute,
            tokens_per_minute=tokens_per_minute,
            sleep=self.sleep,
            clock=self.clock,
        )

    def test_first_call_does_not_wait(self):
        self.pacer().wait()
        self.assertEqual(self.slept, [])

    def test_second_call_is_spaced_by_the_interval(self):
        pacer = self.pacer(per_minute=12)
        pacer.wait()
        pacer.wait()
        self.assertEqual(self.slept, [5.0])

    def test_no_wait_once_the_interval_has_already_passed(self):
        pacer = self.pacer(per_minute=12)
        pacer.wait()
        self.now += 10
        pacer.wait()
        self.assertEqual(self.slept, [])

    def test_token_ceiling_delays_beyond_the_request_interval(self):
        # The token limit is what actually binds on the free tier.
        pacer = self.pacer(per_minute=60, tokens_per_minute=1000)
        pacer.wait()
        pacer.record(900)
        self.now += 1
        pacer.wait(projected_tokens=900)
        self.assertTrue(self.slept)
        self.assertGreater(sum(self.slept), 1.0)

    def test_spent_tokens_leave_the_window_after_a_minute(self):
        pacer = self.pacer(per_minute=60, tokens_per_minute=1000)
        pacer.record(900)
        self.now += 61
        self.assertEqual(pacer.token_delay(self.now, 900), 0.0)


class ClientTests(unittest.TestCase):
    def client(self, response, pacer=None):
        calls = []

        def poster(url, **kwargs):
            calls.append((url, kwargs))
            return response

        client = llm.GroqClient(key="k", pacer=pacer or llm.Pacer(sleep=lambda _s: None))
        client.poster = poster
        return client, calls

    def payload(self):
        return {"sender": "a@b.com", "subject": "Hi", "body": "We are moving forward."}

    def test_successful_classification(self):
        response = FakeResponse(
            payload=completion('{"label": "Interview", "confidence": 0.93, "reason": "Invite."}')
        )
        client, calls = self.client(response)
        result = client.classify(self.payload())
        self.assertEqual(result["label"], "Interview")
        self.assertEqual(calls[0][0], llm.API_URL)
        self.assertEqual(
            calls[0][1]["headers"]["Authorization"], "Bearer k"
        )
        self.assertEqual(
            calls[0][1]["json"]["response_format"], {"type": "json_object"}
        )

    def test_rate_limit_raises_with_retry_after(self):
        response = FakeResponse(status_code=429, headers={"retry-after": "42"})
        client, _calls = self.client(response)
        with self.assertRaises(llm.GroqRateLimited) as caught:
            client.classify(self.payload())
        self.assertEqual(caught.exception.retry_after, 42)

    def test_server_error_raises(self):
        client, _calls = self.client(FakeResponse(status_code=500, text="boom"))
        with self.assertRaises(RuntimeError):
            client.classify(self.payload())

    def test_empty_choices_becomes_unclear(self):
        client, _calls = self.client(FakeResponse(payload={"choices": []}))
        self.assertEqual(client.classify(self.payload())["label"], "Unclear")

    def test_reported_token_usage_feeds_the_pacer(self):
        pacer = llm.Pacer(sleep=lambda _s: None)
        response = FakeResponse(payload=completion('{"label": "Offer", "confidence": 0.9}'))
        client, _calls = self.client(response, pacer=pacer)
        client.classify(self.payload())
        self.assertEqual(sum(n for _at, n in pacer._spent), 850)


class StubClient:
    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.seen = []

    def classify(self, payload):
        self.seen.append(payload)
        if self.error is not None:
            raise self.error
        return self.results.pop(0)


class ListenerSpy:
    """Records the states the runner published, in order."""

    def __init__(self):
        self.states = []

    def __call__(self, runner):
        self.states.append((runner.state, runner.processed))


async def immediate(func, *args):
    """Executor stand-in that calls straight through, no thread involved."""
    return func(*args)


class RunnerTests(EnvIsolationMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.isolate_env()
        handle, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.store = app.JobStore(self.db_path)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.store.conn.close()
        os.unlink(self.db_path)

    def add_match(self, company="Acme", status="Applied", message_id="msg-1", body="Hello."):
        job_id = self.store.create_job(
            {
                "posting_url": f"https://{company.lower()}.com/jobs/{message_id}",
                "position_title": "Engineer",
                "company": company,
                "job_type": "Internship",
                "requires_oa": False,
                "completed_oa": False,
                "received_references": False,
                "payment_amount": "",
                "payment_period": "Unspecified",
                "status": status,
                "application_date": "2026-07-28",
                "response_date": None,
                "notes": "",
            }
        )
        self.store.record_email_match(
            job_id,
            {
                "id": message_id,
                "sender": "careers@acme.com",
                "subject": "Update",
                "date": "Tue, 28 Jul 2026 10:00:00 -0400",
                "body": body,
                "snippet": body[:40],
            },
        )
        return job_id

    def runner(self, results=None, error=None, client_factory=None):
        stub = StubClient(results=results, error=error)
        runner = llm.ClassificationRunner(
            self.store,
            client_factory=client_factory or (lambda: stub),
            executor=immediate,
        )
        return runner, stub

    def job_row(self, job_id):
        return self.store.conn.execute(
            "SELECT status, response_date FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()

    def match_row(self):
        return self.store.conn.execute("SELECT * FROM email_matches").fetchone()

    # Applying -------------------------------------------------------------

    async def test_confident_label_applies_the_status(self):
        job_id = self.add_match()
        runner, _stub = self.runner(
            [{"label": "Rejected", "confidence": 0.95, "reason": "Turned down."}]
        )
        await runner.run()

        self.assertEqual(runner.state, llm.DONE)
        self.assertEqual(runner.applied, 1)
        self.assertEqual(self.job_row(job_id)["status"], "Rejected")
        match = self.match_row()
        self.assertEqual(match["ai_status"], "Rejected")
        self.assertEqual(match["ai_applied"], 1)
        self.assertEqual(match["ai_previous_status"], "Applied")

    async def test_low_confidence_records_without_applying(self):
        job_id = self.add_match()
        runner, _stub = self.runner(
            [{"label": "Rejected", "confidence": 0.4, "reason": "Might be."}]
        )
        await runner.run()

        self.assertEqual(runner.applied, 0)
        self.assertEqual(self.job_row(job_id)["status"], "Applied")
        match = self.match_row()
        self.assertEqual(match["ai_status"], "Rejected")
        self.assertEqual(match["ai_applied"], 0)
        self.assertIsNotNone(match["ai_classified_at"])

    async def test_inert_labels_never_apply_however_confident(self):
        for label in llm.INERT_LABELS:
            with self.subTest(label=label):
                job_id = self.add_match(message_id=f"msg-{label}")
                runner, _stub = self.runner(
                    [{"label": label, "confidence": 1.0, "reason": "Routine."}]
                )
                await runner.run()
                self.assertEqual(runner.applied, 0)
                self.assertEqual(self.job_row(job_id)["status"], "Applied")

    async def test_undo_restores_status_and_response_date(self):
        job_id = self.add_match()
        runner, _stub = self.runner(
            [{"label": "Rejected", "confidence": 0.99, "reason": "No."}]
        )
        await runner.run()

        before = self.job_row(job_id)
        self.assertEqual(before["status"], "Rejected")
        self.assertTrue(before["response_date"])

        self.assertTrue(self.store.undo_ai_status(self.match_row()["id"]))
        after = self.job_row(job_id)
        self.assertEqual(after["status"], "Applied")
        self.assertIn(after["response_date"], (None, ""))
        # The job is back in the pool future Gmail scans look at.
        self.assertIn(job_id, [row["job_id"] for row in self.store.jobs_awaiting_response()])

    async def test_undo_keeps_the_classification_so_it_is_not_redone(self):
        self.add_match()
        runner, _stub = self.runner(
            [{"label": "Rejected", "confidence": 0.99, "reason": "No."}]
        )
        await runner.run()
        self.store.undo_ai_status(self.match_row()["id"])
        self.assertEqual(self.store.unclassified_email_matches(), [])

    async def test_undo_is_a_no_op_when_nothing_was_applied(self):
        self.add_match()
        runner, _stub = self.runner([{"label": "Unclear", "confidence": 0.2, "reason": "?"}])
        await runner.run()
        self.assertFalse(self.store.undo_ai_status(self.match_row()["id"]))

    # Cycle behaviour ------------------------------------------------------

    async def test_rate_limit_stops_the_cycle_and_keeps_earlier_work(self):
        self.add_match(company="Acme", message_id="msg-1")
        self.add_match(company="Globex", message_id="msg-2")

        class FlakyClient:
            def __init__(self):
                self.calls = 0

            def classify(self, payload):
                self.calls += 1
                if self.calls > 1:
                    raise llm.GroqRateLimited("limit", retry_after=42)
                return {"label": "Rejected", "confidence": 0.99, "reason": "No."}

        runner, _stub = self.runner(client_factory=FlakyClient)
        await runner.run()

        self.assertEqual(runner.state, llm.RATE_LIMITED)
        self.assertEqual(runner.retry_after, 42)
        self.assertEqual(runner.processed, 1)
        classified = self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM email_matches WHERE ai_classified_at IS NOT NULL"
        ).fetchone()["n"]
        self.assertEqual(classified, 1)

    async def test_resume_picks_up_only_the_unclassified_remainder(self):
        self.add_match(company="Acme", message_id="msg-1")
        self.add_match(company="Globex", message_id="msg-2")
        runner, _stub = self.runner(
            [
                {"label": "Unclear", "confidence": 0.1, "reason": "?"},
                {"label": "Unclear", "confidence": 0.1, "reason": "?"},
            ]
        )
        await runner.run()
        self.assertEqual(self.store.unclassified_email_matches(), [])

        second, stub = self.runner([])
        await second.run()
        self.assertEqual(second.state, llm.DONE)
        self.assertEqual(stub.seen, [])

    async def test_matches_without_a_body_are_skipped(self):
        self.add_match(body="")
        runner, stub = self.runner([])
        await runner.run()
        self.assertEqual(stub.seen, [])
        self.assertEqual(runner.state, llm.DONE)

    async def test_executor_receives_plain_dicts_not_database_rows(self):
        # Whatever runs the blocking call must not hold anything tied to the
        # sqlite connection this thread owns.
        self.add_match()
        runner, stub = self.runner([{"label": "Unclear", "confidence": 0.1, "reason": "?"}])
        await runner.run()
        self.assertEqual(len(stub.seen), 1)
        self.assertIs(type(stub.seen[0]), dict)

    async def test_client_failure_surfaces_as_an_error_state(self):
        self.add_match()
        runner, _stub = self.runner(error=RuntimeError("network down"))
        await runner.run()
        self.assertEqual(runner.state, llm.ERROR)
        self.assertIn("network down", runner.message)

    async def test_missing_configuration_reports_instead_of_running(self):
        self.add_match()

        def factory():
            raise llm.GroqNotConfigured("no key")

        runner = llm.ClassificationRunner(
            self.store, client_factory=factory, executor=immediate
        )
        await runner.run()
        self.assertEqual(runner.state, llm.ERROR)
        self.assertEqual(runner.processed, 0)

    async def test_nothing_to_do_finishes_immediately(self):
        runner, stub = self.runner([])
        await runner.run()
        self.assertEqual(runner.state, llm.DONE)
        self.assertEqual(stub.seen, [])
        self.assertIn("Nothing new", runner.message)

    async def test_stop_ends_the_cycle_early(self):
        for index in range(3):
            self.add_match(company=f"Co{index}", message_id=f"msg-{index}")

        holder = {}

        class StoppingClient:
            def classify(self, payload):
                # Ask to stop while the first message is in flight. The loop
                # checks the flag before it takes the next one.
                holder["runner"].stop()
                return {"label": "Unclear", "confidence": 0.1, "reason": "?"}

        runner, _stub = self.runner(client_factory=StoppingClient)
        holder["runner"] = runner
        await runner.run()

        self.assertEqual(runner.state, llm.STOPPED)
        self.assertEqual(runner.processed, 1)
        self.assertIn("1 of 3", runner.message)

    async def test_running_again_clears_a_stale_stop_flag(self):
        # A previous stop must not cancel the next cycle before it begins.
        self.add_match()
        runner, _stub = self.runner([{"label": "Unclear", "confidence": 0.1, "reason": "?"}])
        runner.stop()
        await runner.run()
        self.assertEqual(runner.state, llm.DONE)
        self.assertEqual(runner.processed, 1)

    # Subscribers ----------------------------------------------------------

    async def test_subscribers_see_the_cycle_progress(self):
        self.add_match(company="Acme", message_id="msg-1")
        self.add_match(company="Globex", message_id="msg-2")
        runner, _stub = self.runner(
            [
                {"label": "Unclear", "confidence": 0.1, "reason": "?"},
                {"label": "Unclear", "confidence": 0.1, "reason": "?"},
            ]
        )
        spy = ListenerSpy()
        runner.subscribe(spy)
        await runner.run()

        self.assertEqual(spy.states[0], (llm.RUNNING, 0))
        self.assertEqual(spy.states[-1], (llm.DONE, 2))
        # Progress is published as each message completes, which is what keeps
        # the bar moving rather than jumping at the end.
        self.assertIn((llm.RUNNING, 1), spy.states)

    async def test_unsubscribe_stops_the_updates(self):
        self.add_match()
        runner, _stub = self.runner([{"label": "Unclear", "confidence": 0.1, "reason": "?"}])
        spy = ListenerSpy()
        runner.subscribe(spy)
        runner.unsubscribe(spy)
        await runner.run()
        self.assertEqual(spy.states, [])

    async def test_progress_text_describes_the_running_cycle(self):
        runner, _stub = self.runner([])
        runner.state = llm.RUNNING
        runner.total = 4
        runner.processed = 1
        runner.current = "Acme"
        self.assertEqual(runner.progress_text(), "Classifying 2 of 4 — Acme")



class AiColumnMigrationTests(unittest.TestCase):
    OLD_SCHEMA = """
        CREATE TABLE email_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            gmail_message_id TEXT NOT NULL,
            sender TEXT,
            subject TEXT,
            received_date TEXT,
            reviewed INTEGER NOT NULL DEFAULT 0,
            dismissed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(job_id, gmail_message_id)
        );
    """

    def setUp(self):
        import sqlite3

        handle, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(self.OLD_SCHEMA)
        conn.execute(
            """
            INSERT INTO email_matches (
                job_id, gmail_message_id, sender, subject, received_date, created_at
            )
            VALUES ('JOB1', 'msg-old', 'a@b.com', 'Old', '', '2026-07-01')
            """
        )
        conn.commit()
        conn.close()
        self.addCleanup(os.unlink, self.db_path)

    def test_ai_columns_are_added_to_an_old_database(self):
        store = app.JobStore(self.db_path)
        self.addCleanup(store.conn.close)
        columns = {
            row["name"] for row in store.conn.execute("PRAGMA table_info(email_matches)")
        }
        for expected in (
            "ai_status",
            "ai_confidence",
            "ai_reason",
            "ai_classified_at",
            "ai_applied",
            "ai_previous_status",
            "ai_previous_response_date",
        ):
            self.assertIn(expected, columns)

    def test_existing_rows_default_to_not_applied(self):
        store = app.JobStore(self.db_path)
        self.addCleanup(store.conn.close)
        row = store.conn.execute("SELECT * FROM email_matches").fetchone()
        self.assertEqual(row["ai_applied"], 0)
        self.assertIsNone(row["ai_status"])


class TokenEstimationTests(unittest.TestCase):
    """Projecting a request's real cost instead of a flat per-call figure.

    The flat 900-token projection is what produced free-tier 429s on a real
    mailbox. Alert extraction actually costs around 3,400 tokens, so four calls
    drained a 12,000-token minute while the pacer believed it had room for
    thirteen.
    """

    def test_longer_prompt_projects_more(self):
        short = [{"role": "user", "content": "hi"}]
        long_one = [{"role": "user", "content": "x" * 8000}]
        self.assertLess(llm.estimate_tokens(short, 200),
                        llm.estimate_tokens(long_one, 200))

    def test_output_ceiling_is_included(self):
        messages = [{"role": "user", "content": "hi"}]
        self.assertEqual(
            llm.estimate_tokens(messages, 1500) - llm.estimate_tokens(messages, 200),
            1300,
        )

    def test_alert_extraction_projects_far_above_the_old_flat_figure(self):
        # The regression this change exists for.
        messages = [
            {"role": "system", "content": "s" * 1500},
            {"role": "user", "content": "b" * 6000},
        ]
        projected = llm.estimate_tokens(messages, 1500)
        self.assertGreater(projected, 3000)
        self.assertGreater(projected, llm.ESTIMATED_TOKENS_PER_CALL * 3)

    def test_classification_stays_near_the_old_figure(self):
        # The old default was not wrong for classification, only for the big
        # calls. Guards against over-correcting and throttling the common path.
        messages = [
            {"role": "system", "content": "s" * 1900},
            {"role": "user", "content": "b" * 2000},
        ]
        self.assertLess(llm.estimate_tokens(messages, 200), 2000)


class PacerBooksRealCostTests(unittest.TestCase):
    def setUp(self):
        self.slept = []
        self.now = 0.0

    def clock(self):
        return self.now

    def pacer(self, per_minute=60, tokens_per_minute=12000):
        return llm.Pacer(
            per_minute=per_minute,
            tokens_per_minute=tokens_per_minute,
            sleep=self.slept.append,
            clock=self.clock,
        )

    def test_complete_json_books_a_measured_projection(self):
        pacer = self.pacer()
        seen = []
        pacer.wait = lambda projected_tokens=None: seen.append(projected_tokens)

        client = llm.GroqClient(key="k", pacer=pacer)
        client.poster = lambda url, **kwargs: FakeResponse(
            payload={"choices": [{"message": {"content": '{"label": "Rejected", '
                                                          '"confidence": 0.9, '
                                                          '"reason": "r"}'}}],
                     "usage": {"total_tokens": 3400}})
        client.complete_json(
            [{"role": "user", "content": "x" * 6000}],
            llm.parse_classification, llm.unclear(), max_tokens=1500,
        )

        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], 2000,
                           "must book the real cost, not the flat default")

    def test_big_calls_throttle_sooner_than_small_ones(self):
        # Four 3,400-token calls exceed a 12,000-token minute. That is the
        # ceiling the pacer previously could not see coming.
        pacer = self.pacer()
        for _ in range(3):
            pacer.record(3400)
        self.assertGreater(pacer.token_delay(1.0, 3400), 0,
                           "a fourth big call must wait")
        self.assertEqual(pacer.token_delay(1.0, 900), 0.0,
                         "a small call still fits in what is left")

    def test_oversized_single_call_does_not_hang(self):
        # Nothing spent yet, so waiting cannot free room no earlier call holds.
        # Previously this raised ValueError from min() over an empty list.
        self.assertEqual(self.pacer().token_delay(1.0, 999999), 0.0)


if __name__ == "__main__":
    unittest.main()
