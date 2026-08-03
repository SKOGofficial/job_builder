"""Page tests driven through NiceGUI's user simulation.

These replace the Tkinter render tests. They open real routes, click real
elements, and assert on what a person would see — with no browser and no
display, so they run on a headless runner without xvfb.
"""

import importlib
import os
import tempfile

import pytest
from nicegui import ui

import utilities.store as store_module
from clients import llm_client
from utilities import credentials
from utilities.identity import identity_key
from utilities.store import JobStore
from web.state import AppState, set_state


# Fixtures ------------------------------------------------------------------


@pytest.fixture
def db_path():
    handle, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(handle)
    original = store_module.DB_PATH
    store_module.DB_PATH = path
    yield path
    store_module.DB_PATH = original
    try:
        os.unlink(path)
    except PermissionError:
        # Windows can hold the handle briefly after close. A leftover temp file
        # is not worth failing a test over.
        pass


@pytest.fixture
def state(db_path, register_routes):
    """Fresh shared state pointed at a throwaway database."""
    app_state = set_state(AppState(JobStore(db_path)))
    yield app_state
    app_state.store.conn.close()
    set_state(None)


@pytest.fixture
def store(state):
    return state.store


@pytest.fixture
def register_routes(user):
    """Re-register every route for this test.

    The user fixture clears NiceGUI's route table on entry, and importing the
    page modules again would be a no-op once they are in sys.modules. Reloading
    them re-runs the @ui.page decorators against the fresh table. Depending on
    `user` is what orders this after the reset.
    """
    import web.pages

    for name in web.pages.__all__:
        importlib.reload(getattr(web.pages, name))


class StubGroq:
    """Returns canned labels in order, so a cycle is deterministic and instant."""

    def __init__(self, results):
        self.results = list(results)
        self.seen = []

    def classify(self, payload):
        self.seen.append(payload)
        return self.results.pop(0)


class RateLimitedGroq:
    def __init__(self, allow=1):
        self.allow = allow
        self.calls = 0

    def classify(self, payload):
        self.calls += 1
        if self.calls > self.allow:
            raise llm_client.GroqRateLimited("limit", retry_after=42)
        return {"label": "Rejected", "confidence": 0.97, "reason": "Declined."}


class FailingKeyring:
    """A machine where keyring is installed but no backend answers."""

    def get_password(self, service, username):
        raise credentials.KeyringError("No recommended backend was available")

    def set_password(self, service, username, value):
        raise credentials.KeyringError("No recommended backend was available")

    def delete_password(self, service, username):
        raise credentials.KeyringError("No recommended backend was available")


def add_job(store, company="Acme", status="Applied", index=0, title="Engineer"):
    return store.create_job(
        {
            "posting_url": f"https://{company.lower().replace(' ', '')}.com/jobs/{index}",
            "position_title": title,
            "company": company,
            "job_type": "Full time",
            "requires_oa": False,
            "completed_oa": False,
            "received_references": False,
            "payment_amount": "",
            "payment_period": "Unspecified",
            "status": status,
            "application_date": "2026-07-28",
            "response_date": None,
            "notes": "Applied through the careers portal.",
        }
    )


def add_match(store, company="Acme", index=0, body="We would like to talk.", status="Applied"):
    job_id = add_job(store, company=company, status=status, index=index)
    store.record_email_match(
        job_id,
        {
            "id": f"msg-{index}",
            "sender": f"careers@{company.lower().replace(' ', '')}.com",
            "subject": f"Your application at {company}",
            "date": "Tue, 28 Jul 2026 10:00:00 -0400",
            "body": body,
            "snippet": body[:40],
        },
    )
    return job_id


def match_id(store):
    return store.pending_email_matches()[0]["id"]


def use_stub_classifier(state, results):
    stub = StubGroq(results)
    state.classifier.client_factory = lambda: stub
    state.classifier.is_configured = lambda: True
    return stub


# Routing -------------------------------------------------------------------


@pytest.mark.parametrize(
    "route, heading",
    [
        ("/", "All job postings"),
        ("/add", "Add a job application"),
        ("/dashboard", "Application dashboard"),
        ("/email-matches", "Email matches"),
        ("/leads", "To apply"),
        ("/review", "Review queue"),
        ("/experiences", "Experiences"),
        ("/settings", "Settings"),
        ("/profile", "Profile"),
        ("/resume", "Resume notes"),
    ],
)
async def test_every_route_renders(user, state, route, heading):
    await user.open(route)
    await user.should_see(heading)


async def test_every_route_renders_without_a_credential_store(
    user, state, route="/settings", monkeypatch=None
):
    # The Linux and CI case: keyring installed, no backend answering. Rendering
    # must not depend on a credential store being present.
    saved = credentials.keyring
    credentials.keyring = FailingKeyring()
    try:
        for path in ("/settings", "/email-matches"):
            await user.open(path)
            await user.should_see("AI classification")
    finally:
        credentials.keyring = saved


# All jobs ------------------------------------------------------------------


async def test_jobs_table_lists_applications(user, store):
    add_job(store, company="Acme", index=1, title="Backend Engineer")
    add_job(store, company="Globex", index=2, title="Platform Engineer")
    await user.open("/")

    # A table's rows are props rather than child elements, so this asserts on
    # the data the table was handed rather than on rendered text.
    table = next(iter(user.find(kind=ui.table).elements))
    assert {row["position_title"] for row in table.rows} == {
        "Backend Engineer",
        "Platform Engineer",
    }
    assert {row["company"] for row in table.rows} == {"Acme", "Globex"}
    # The status chip colour comes from the same palette the dashboard uses.
    assert all(row["status_color"] for row in table.rows)


async def test_empty_jobs_table_explains_itself(user, store):
    await user.open("/")
    await user.should_see("No applications recorded yet")


# Add application -----------------------------------------------------------


async def test_saving_without_required_fields_is_refused(user, store):
    await user.open("/add")
    user.find("Save application").click()
    await user.should_see("Please complete")
    assert store.list_jobs() == []


# Email matches -------------------------------------------------------------


async def test_pending_matches_are_listed(user, store):
    add_match(store, company="Acme", index=1)
    await user.open("/email-matches")
    await user.should_see("1 pending")
    await user.should_see("Engineer at Acme")


async def test_no_matches_message(user, store):
    await user.open("/email-matches")
    await user.should_see("No pending matches")


async def test_applied_classification_shows_badge_and_undo(user, store):
    add_match(store, company="Acme", index=1)
    identifier = match_id(store)
    store.record_classification(identifier, "Rejected", 0.94, "They declined.")
    store.apply_ai_status(identifier, "Rejected")

    await user.open("/email-matches")
    await user.should_see("AI: Rejected")
    await user.should_see("Applied automatically, replacing Applied.")
    await user.should_see("They declined.")
    await user.should_see("Undo")


async def test_inert_label_shows_no_undo(user, store):
    # Acknowledgement never applies, however confident, so nothing to undo.
    add_match(store, company="Acme", index=1)
    identifier = match_id(store)
    store.record_classification(identifier, "Acknowledgement", 0.99, "Routine.")

    await user.open("/email-matches")
    await user.should_see("AI: Acknowledgement")
    await user.should_not_see("Undo")


async def test_undo_restores_the_previous_status(user, store):
    job_id = add_match(store, company="Acme", index=1)
    identifier = match_id(store)
    store.record_classification(identifier, "Rejected", 0.94, "They declined.")
    store.apply_ai_status(identifier, "Rejected")

    await user.open("/email-matches")
    user.find("Undo").click()
    await user.should_see("Reverted the status")

    job = store.conn.execute(
        "SELECT status, response_date FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    assert job["status"] == "Applied"
    assert job["response_date"] in (None, "")
    # Back in the pool that future Gmail scans look at.
    assert job_id in [row["job_id"] for row in store.jobs_awaiting_response()]


async def test_dismiss_clears_a_match(user, store):
    add_match(store, company="Acme", index=1)
    await user.open("/email-matches")
    user.find("Dismiss").click()
    await user.should_see("Match dismissed")
    assert store.pending_email_matches() == []


# Classification cycle ------------------------------------------------------


async def test_classify_button_shows_the_pending_count(user, state, store):
    add_match(store, company="Acme", index=1)
    add_match(store, company="Globex", index=2)
    use_stub_classifier(state, [])
    await user.open("/email-matches")
    await user.should_see("Classify 2 message(s)")


async def test_running_a_cycle_applies_confident_labels(user, state, store):
    job_id = add_match(store, company="Acme", index=1)
    use_stub_classifier(
        state, [{"label": "Interview", "confidence": 0.95, "reason": "A call is proposed."}]
    )

    await user.open("/email-matches")
    user.find("Classify 1 message(s)").click()
    await user.should_see("1 status(es) applied automatically")

    job = store.conn.execute(
        "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    assert job["status"] == "Interview"
    await user.should_see("AI: Interview")


async def test_low_confidence_records_without_applying(user, state, store):
    job_id = add_match(store, company="Acme", index=1)
    use_stub_classifier(
        state, [{"label": "Rejected", "confidence": 0.40, "reason": "Might be."}]
    )

    await user.open("/email-matches")
    user.find("Classify 1 message(s)").click()
    await user.should_see("0 status(es) applied automatically")

    job = store.conn.execute(
        "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    assert job["status"] == "Applied"
    await user.should_not_see("Undo")


async def test_rate_limit_offers_resume(user, state, store):
    add_match(store, company="Acme", index=1)
    add_match(store, company="Globex", index=2)
    stub = RateLimitedGroq(allow=1)
    state.classifier.client_factory = lambda: stub
    state.classifier.is_configured = lambda: True

    await user.open("/email-matches")
    user.find("Classify 2 message(s)").click()
    await user.should_see("Groq rate limit reached")
    await user.should_see("Try again in about 42s")
    await user.should_see("Resume classification")

    # The classification that succeeded before the limit is kept.
    classified = store.conn.execute(
        "SELECT COUNT(*) AS n FROM email_matches WHERE ai_classified_at IS NOT NULL"
    ).fetchone()["n"]
    assert classified == 1


async def test_unconfigured_classifier_points_at_settings(user, state, store):
    add_match(store, company="Acme", index=1)
    state.classifier.is_configured = lambda: False
    await user.open("/email-matches")
    await user.should_see("No Groq API key found")
    await user.should_see("Open Settings")


# Text storage --------------------------------------------------------------


async def test_profile_text_round_trips(user, store):
    store.save_profile_value("profile_text", "London, targeting backend roles")
    await user.open("/profile")
    await user.should_see("London, targeting backend roles")


# Navigation ----------------------------------------------------------------


async def test_top_nav_reaches_its_pages(user, state):
    await user.open("/")
    user.find("Dashboard").click()
    await user.should_see("Application dashboard")
    user.find("Add application").click()
    await user.should_see("Add a job application")
    user.find("All jobs").click()
    await user.should_see("All job postings")


async def test_drawer_reaches_its_pages(user, state):
    for label, heading in [
        ("Review queue", "could not attach to an application"),
        ("Email matches", "Suggested replies matched"),
        ("Experiences", "Individual resume bullets"),
        ("Settings", "AI classification (Groq)"),
        ("Profile", "Store contact details"),
        ("Resume notes", "Free-text resume and CV context"),
    ]:
        await user.open("/")
        user.find(label).click()
        await user.should_see(heading)


# Settings ------------------------------------------------------------------


async def test_settings_shows_every_card(user, state):
    await user.open("/settings")
    await user.should_see("Appearance")
    await user.should_see("Dark mode")
    await user.should_see("Gmail")
    await user.should_see("AI classification (Groq)")


async def test_settings_can_run_a_classification_cycle(user, state, store):
    job_id = add_match(store, company="Acme", index=1)
    use_stub_classifier(
        state, [{"label": "Interview", "confidence": 0.95, "reason": "A call is proposed."}]
    )

    await user.open("/settings")
    user.find("Classify 1 message(s)").click()
    await user.should_see("1 status(es) applied automatically")

    job = store.conn.execute(
        "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    assert job["status"] == "Interview"


async def test_settings_offers_resume_after_a_rate_limit(user, state, store):
    add_match(store, company="Acme", index=1)
    add_match(store, company="Globex", index=2)
    state.classifier.client_factory = lambda: RateLimitedGroq(allow=1)
    state.classifier.is_configured = lambda: True

    await user.open("/settings")
    user.find("Classify 2 message(s)").click()
    await user.should_see("Groq rate limit reached")
    await user.should_see("Resume classification")


# Leads ---------------------------------------------------------------------


def add_lead(state, title="Backend Engineer", company="Acme", status="ready", score=0.9):
    key = identity_key(title, company, "Remote")
    state.mail.upsert_lead({
        "identity_key": key,
        "title": title,
        "company": company,
        "location": "Remote",
        "apply_url": "https://example.com/jobs/1",
        "board": "linkedin",
        "board_job_id": "1",
        "status": status,
    })
    lead = state.mail.lead_by_identity(key)
    if score is not None:
        state.mail.set_lead_relevance(lead["id"], score, "Matches your target roles.")
    state.mail.commit()
    return state.mail.lead_by_identity(key)


async def test_leads_page_lists_open_leads(user, state):
    add_lead(state)
    await user.open("/leads")
    await user.should_see("Backend Engineer")
    await user.should_see("Relevance 90%")
    await user.should_see("Matches your target roles.")


async def test_empty_leads_page_explains_itself(user, state):
    await user.open("/leads")
    await user.should_see("No leads here yet")


async def test_promoting_a_lead_creates_an_application(user, state, store):
    add_lead(state)
    await user.open("/leads")
    user.find("I applied to this").click()
    await user.should_see("Moved to applications")

    jobs = store.list_jobs()
    assert [row["position_title"] for row in jobs] == ["Backend Engineer"]
    assert jobs[0]["status"] == "Applied"
    # The identity carries across, which is what keeps linked mail attached.
    assert jobs[0]["identity_key"] == identity_key("Backend Engineer", "Acme", "Remote")


async def test_dismissing_a_lead_removes_it_from_the_open_list(user, state):
    add_lead(state)
    await user.open("/leads")
    user.find("Not interested").click()
    await user.should_see("Lead dismissed")
    assert state.mail.list_leads() == []


# Review queue --------------------------------------------------------------


def add_message(state, message_id="msg-1", sender="careers@acme.com",
                category="job_update", subject="About your application"):
    state.mail.upsert_message({
        "id": message_id,
        "thread_id": "t-1",
        "sender": sender,
        "subject": subject,
        "date": "Tue, 28 Jul 2026 10:00:00 -0400",
        "snippet": "We wanted to follow up.",
        "labels": [],
    })
    state.mail.store_body(message_id, "We wanted to follow up on your application.")
    state.mail.record_category(message_id, category, 0.9, "Mentions an application.")
    state.mail.commit()


async def test_review_queue_lists_unplaced_messages(user, state):
    add_message(state)
    await user.open("/review")
    await user.should_see("About your application")
    await user.should_see("Application update")


async def test_empty_review_queue_explains_itself(user, state):
    await user.open("/review")
    await user.should_see("Nothing waiting")


async def test_marking_a_sender_not_job_related_blocks_the_domain(user, state):
    add_message(state)
    await user.open("/review")
    user.find("Not job related").click()
    await user.should_see("dropped before classification")
    assert "acme.com" in state.mail.denied_domains()


async def test_linked_message_appears_on_the_job_timeline(user, state, store):
    # The timeline lives in a dialog behind a table row click, and table rows
    # are props rather than elements, so there is nothing to click in the
    # simulation. Rendering the component directly is the closest honest test.
    from web.pages.jobs import timeline

    add_job(store, company="Acme", index=1, title="Engineer")
    job = store.list_jobs()[0]
    add_message(state)
    state.mail.link_message("msg-1", job["identity_key"], "update",
                            resolved_by="manual")
    state.mail.commit()

    await user.open("/")
    with user:
        timeline(state.mail, job["identity_key"])
    await user.should_see("Email timeline")
    await user.should_see("About your application")
    await user.should_see("linked by you")


async def test_timeline_is_empty_without_links(user, state, store):
    from web.pages.jobs import timeline

    add_job(store, company="Acme", index=1, title="Engineer")
    job = store.list_jobs()[0]

    await user.open("/")
    with user:
        timeline(state.mail, job["identity_key"])
    await user.should_see("No emails linked to this role yet")


# Experiences ---------------------------------------------------------------


async def test_experiences_round_trip(user, state):
    state.mail.add_experience({
        "kind": "work",
        "organisation": "Acme",
        "role": "Engineer",
        "bullet": "Built the ingest pipeline.",
        "tags": "python, sqlite",
    })
    await user.open("/experiences")
    await user.should_see("Built the ingest pipeline.")
    await user.should_see("python")


async def test_empty_experiences_page_explains_itself(user, state):
    await user.open("/experiences")
    await user.should_see("No experiences stored yet")


# Pipeline settings ---------------------------------------------------------


async def test_settings_shows_the_pipeline_card(user, state):
    await user.open("/settings")
    await user.should_see("Mailbox ingest")
    await user.should_see("Blocked senders")


async def test_blocked_domains_are_listed_and_removable(user, state):
    state.mail.deny_sender("newsletter.example.com")
    await user.open("/settings")
    await user.should_see("newsletter.example.com")


async def test_dark_mode_choice_is_stored(user, state, store):
    assert store.get_profile_value("theme", "light") == "light"
    await user.open("/settings")
    user.find("Dark mode").click()
    await user.should_see("Appearance")
    assert store.get_profile_value("theme", "light") == "dark"
