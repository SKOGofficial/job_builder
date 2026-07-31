# Backend Plan: Mailbox Ingest Pipeline + Job Identity Rework

Status: proposed, not started. Written 2026-07-31.

This plan covers the feature set requested on 2026-07-31: run the app 24/7 on an
Ubuntu homelab server, poll Gmail on a schedule, mirror mail locally, triage it
into three job-related categories, and act on each category. It also covers the
requested change of job identity from posting URL to (title, company, location).

Read `.agents/AGENTS.md` and `.agents/IMPLEMENTATION_PLAN.md` first. Note that
both are stale in places (they describe a Tkinter UI; the app is NiceGUI now).

## What changes at the architecture level

The current Gmail integration is **job-driven pull**: for each job still waiting
on a response, build a Gmail query from the company name, fetch headers, match
conservatively, store a suggestion. `GmailScanner.scan()` in
`clients/gmail_client.py` is that loop.

What is being asked for is the inverse - **mailbox-driven ingest**: pull all
mail on a schedule, store it, classify it, then let the classification decide
which job it touches or creates. This is not an extension of `GmailScanner`; it
is a second ingest path, and it makes the per-job query loop redundant for
everything except a manual "check this one job now" action.

Keep `GmailScanner` during the transition. Retire it once the new path proves
out, so there is a working fallback while the poller is being debugged.

## Decisions to make before Phase 1

These change the shape of the work. Recommendations are given; each can be
overridden.

### 1. Network exposure and authentication - the important one

The app has **no authentication of any kind**. Every page is served to whoever
connects. `app.py` currently binds `host="127.0.0.1"` with the comment
"Local-only tool: binding to loopback keeps it off the network." That comment is
load-bearing: it is the entire access control model.

Once the server holds a full mirror of a personal mailbox, changing that bind to
`0.0.0.0` means anyone on the LAN - a guest phone, a compromised IoT device -
can read every email body, every job application, and the profile page.

**Recommendation:** keep `host="127.0.0.1"`. Reach it over Tailscale, WireGuard,
or an SSH tunnel (`ssh -L 8080:localhost:8080 server`). If it must be reachable
on the LAN directly, put it behind a reverse proxy that terminates TLS and
enforces auth (Caddy + basic auth is about six lines), and treat adding real
login to the app as its own P0 item.

### 2. Mirror scope - all bodies, or headers plus selected bodies

The request was to pull all messages locally, and separately to deal with
"message bloat from the ones that are irrelevant."

Those pull against each other. A full body mirror of a busy mailbox runs to
hundreds of MB and means every LLM triage decision happens after the expensive
part (the download) is already done.

**Recommendation - the middle path:** sync **every message ID and header set**
(cheap, complete, nothing is ever missed, and it satisfies "save all the message
IDs"), then download **bodies only for messages that pass a deterministic
prefilter**. The prefilter is Phase 2 and should kill 90%+ of a normal mailbox
before any LLM sees it. This is a config flag, so full-body mirroring stays
available if the recall turns out to matter.

Note this inverts a privacy property the current code advertises: today, bodies
are fetched only for messages that already matched a job. The README and the
`gmail_client.py` module docstring both make that claim explicitly. Those
statements become false and must be updated in the same PR - not left to drift.

### 3. Alert-created jobs - separate table, or a status on `jobs`

A single LinkedIn alert email carries 5-10 postings, daily. Writing those
straight into `jobs` would put hundreds of rows the user never applied to into
the same table as real applications, which wrecks the dashboard: "applications
over time" counts them, and response rate divides by them.

**Recommendation:** a separate `job_leads` table with an explicit "promote to
application" action. This is the same idea as the "structured intake queue"
already in the P1 backlog of `.agents/IMPLEMENTATION_PLAN.md`. The requested
`application-pending` state is exactly what a lead is; it just lives in its own
table so the metrics stay honest.

Alternative if a single table is preferred: add `Lead` to `STATUSES` in
`utilities/theme.py` and exclude it from `stats()`, `daily_counts()`,
`cumulative_counts()`, and `status_counts()` in `utilities/store.py`. That is
four exclusion clauses that must never be forgotten, which is why the separate
table is the recommendation.

### 4. Research provider

Groq has no web search, so "internet-aided LLM" needs either a separate search
API bolted onto Groq, or a provider with server-side search.

**Recommendation - keep both, split by job:**

- **Groq stays** for triage and classification. It is high volume (every
  candidate message), the task is "pick one of N labels," the free tier covers
  it, and `clients/llm_client.py` already handles pacing, validation, and rate
  limits properly. Do not rewrite working code.
- **Add Claude Opus 5** (`claude-opus-5`) in a new `clients/research_client.py`
  for company research and resume generation. It has a server-side web search
  tool (`web_search_20260209`) and web fetch (`web_fetch_20260209`), so no
  separate search API key is needed. Volume is low - one call per lead the user
  actually decides to pursue.

Rough cost: Opus 5 is $5/M input, $25/M output. A research pass pulling in
search results is on the order of 20-50k input and a few thousand output tokens,
so call it $0.25-0.50 per prepared application in token cost. Check current
web-search tool pricing separately; it may carry a per-use charge on top of
tokens. Use `client.messages.count_tokens()` against a real prompt to get a
number rather than trusting that estimate.

This split is also the reason not to consolidate: the cheap model does the
thousand-call job, the expensive model does the ten-call job.

---

## Phase 0 - Server readiness

Blockers for running 24/7 headless. None of the later phases matter if the
process cannot stay up.

**0.1 Headless launch.** `app.py:98-108` calls `ui.run(..., show=not native)`.
With no `pywebview` and no display, `native_available()` returns False and
`show=True` tries to open a browser that does not exist. Add a `--headless`
flag that forces `native=False, show=False`.

**0.2 OAuth consent from a headless box.** `run_auth_flow()` in
`clients/gmail_client.py:153` calls `flow.run_local_server(port=0,
prompt="consent")`, which picks a random port and launches a browser. Neither
works over SSH.

Fix: pin the port and suppress the browser launch -
`flow.run_local_server(port=8765, open_browser=False, prompt="consent")`. It
then prints a URL. Forward the port from a desktop
(`ssh -L 8765:localhost:8765 server`), open the printed URL in a local browser,
and the redirect lands back on the server. Loopback redirects on any port are
permitted for Desktop OAuth clients per RFC 8252, so no Google Cloud console
change is needed.

**0.3 Secrets without a desktop keyring.** `utilities/credentials.py` already
degrades correctly on reads - `read_secret()` returns None when no backend
answers - and `llm_client.api_key()` already falls back to `.env`. Two gaps:

- `write_secret()` raises on a headless box, by design. So "Move key to
  Credential Manager" in Settings will fail there. Detect and disable the
  button rather than letting it throw.
- **The Gmail refresh token has no fallback at all.** `stored_refresh_token()`
  (`gmail_client.py:105`) only reads keyring. On a server with no Secret
  Service, Gmail can never be connected.

Fix: add a deliberate, opt-in file backend for the refresh token - a
`JOB_BUILDER_SECRETS=file` mode writing to a 0600 file outside the repo (e.g.
`~/.config/job_builder/credentials.json`), or load it via systemd
`EnvironmentFile=` / `LoadCredential=`. Make it explicit rather than a silent
fallback, so nobody ends up with a token on disk without choosing it.

**0.4 systemd unit.** `Restart=always`, `RestartSec=10`, `User=` a dedicated
non-root account, `WorkingDirectory=` the repo, `ExecStart=` the venv Python.
Add `Environment=PYTHONUNBUFFERED=1` so logs reach the journal promptly.

**0.5 SQLite for long-running use.** In `JobStore.__init__`:

```
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;
```

WAL lets a CLI or maintenance script read while the app writes. `busy_timeout`
stops spurious "database is locked" errors. WAL is persisted in the database
file, so it only takes effect once, but setting it per connection is harmless.

**0.6 Logging.** Replace anything that prints with the `logging` module at
module level, so systemd captures it and a 3am failure is diagnosable. The
scanner and classifier currently surface errors only into `self.message`, which
is a UI string - it vanishes when no page is open.

**0.7 Backups.** There is already a
`job_applications.backup-20260731-074917.sqlite3` in the repo root, which
suggests manual copies. Automate: a timer that runs `VACUUM INTO
'backups/job_applications-<date>.sqlite3'` (safe against a live writer, unlike
`cp`) and keeps the last N.

---

## Phase 1 - Job identity: URL to (title, company, location)

Do this **before** ingest, so the ingest code writes into the final schema
instead of being rewritten a phase later.

**1.1 Real migrations.** `JobStore.migrate()` only does `ADD COLUMN`. This phase
needs to make `posting_url` and `url_hash` nullable, which SQLite cannot do with
`ALTER TABLE`. That means the table-rebuild dance: create new table, `INSERT
INTO ... SELECT`, drop old, rename - inside a transaction.

Add a version gate first. `PRAGMA user_version` is built into SQLite and needs
no table of its own:

```
version = conn.execute("PRAGMA user_version").fetchone()[0]
if version < 1: migrate_to_v1(conn); conn.execute("PRAGMA user_version = 1")
```

Back up before the first rebuild runs. `AGENTS.md` forbids touching
`job_applications.sqlite3` without a clear backup path - this is that path.

**1.2 Normalization helpers.** New `utilities/identity.py`, holding pure
functions with no SQLite and no UI:

- `normalize_title(s)` - lowercase, strip punctuation, drop seniority noise that
  varies between boards (`sr.`/`senior`, `jr.`, trailing roman numerals, `(remote)`).
- `normalize_company(s)` - reuse the existing `company_slug()` and
  `COMPANY_SUFFIXES` from `clients/gmail_client.py:316-332`. **Move them here**;
  `store.py` needs them and must not import from `clients/`.
- `normalize_location(s)` - canonicalize the remote variants ("Remote",
  "Remote - US", "San Francisco, CA (Remote)") and city/state forms.
- `identity_key(title, company, location)` - sha256 of the joined normalized
  triple, first 12 hex, uppercased. Same shape as the existing `url_hash()` so
  IDs stay visually consistent.

This normalization is the weakest link in the whole feature and deserves the
most tests. "Senior Software Engineer" vs "Sr. Software Engineer" at "Google"
vs "Google LLC" must collapse; two genuinely different roles must not.

**1.3 Schema changes.**

- `jobs`: add `location TEXT`, add `identity_key TEXT` with a unique index.
  Make `posting_url` and `url_hash` nullable - a job from an alert email may
  have no URL at all.
- **Keep `job_id` as the primary key and do not rewrite it.** `email_matches`
  references it, and rewriting would orphan every stored match. New jobs derive
  `job_id` from `identity_key`; existing jobs keep their URL-derived ID. The
  identity is `identity_key`; `job_id` is just a stable handle.
- New `job_sources` table: `(id, job_id, url, board, board_job_id, first_seen)`.
  This is the "many boards, one position" requirement - one job row, many source
  URLs. `board_job_id` matters: LinkedIn and Indeed both put a stable numeric job
  ID in their URLs, which dedupes better than the tracking URL around it.

**1.4 Backfill.** Compute `identity_key` for every existing row from its
title and company. Location is NULL for all of them (no such column today), so
it must be excluded from the hash when absent, or every legacy row collides on
the same NULL location. Decide explicitly: recommendation is to include location
in the hash only when present, and record which scheme was used per row.

Backfill will surface genuine duplicates - the same job logged twice from two
boards. Do **not** auto-merge. Report them on a review page and let the user
merge; a wrong merge silently destroys application history. A `merge_jobs()`
operation can be a later item.

**1.5 Coordinate with the frontend agent.** This phase changes what a job *is*.
Every page that displays, creates, or filters jobs is affected -
`web/pages/add_application.py`, `jobs.py`, `dashboard.py`, `email_matches.py`.
`tests/test_app_pages.py` renders every page and will fail loudly. Land the
store changes and the page changes together, or the app breaks between commits.

---

## Phase 2 - Mailbox mirror and scheduler

**2.1 `messages` table.** Separate from `email_matches`, so the hot job queries
never scan message bodies:

```
messages(
  gmail_message_id TEXT PRIMARY KEY,
  thread_id, sender, subject, received_date, snippet,
  body_text,            -- NULL until the prefilter says fetch it
  category,             -- NULL until triaged (Phase 3)
  category_confidence, triaged_at,
  fetched_at, body_fetched_at
)
```

**2.2 Incremental sync via the History API.** Do not re-run `messages.list` on
every poll. Store the mailbox `historyId` and call `users.history.list` with
`startHistoryId` to get only what changed. Gmail expires history after roughly a
week, so handle the 404 by falling back to a bounded full `messages.list` sync
and re-seeding `historyId`. This is the difference between a poller that costs
nothing and one that burns quota.

Quota is not a concern at personal-mailbox scale: 1M units/day, and
`history.list` is 2 units, `messages.get` 5. A 5-15 minute poll interval is
comfortable.

**2.3 Prefilter (deterministic, runs before any LLM).** Ordered cheapest-first:

1. Known job-board sender domains (linkedin.com, indeed.com, greenhouse.io,
   lever.co, ashbyhq.com, workday, smartrecruiters, ...) -> candidate.
2. Sender domain matches a company already in `jobs` -> candidate.
3. Subject keyword hits ("application", "interview", "assessment",
   "position", "role", "we received", "thank you for applying") -> candidate.
4. Everything else -> mark irrelevant, never fetch the body.

Keep the list in a data file, not inline in code, so it can be tuned without a
code change. Log the pass rate: if it is not filtering most of the mailbox, it
is not doing its job.

**2.4 Retention.** A prune job that drops `body_text` for messages triaged
irrelevant and older than N days, keeping the ID and headers so they are never
re-fetched. This is the direct answer to the "message bloat" concern.

**2.5 Scheduler.** An in-process `asyncio` task started at app boot, holding a
reference to the shared `AppState`.

**Preserve the existing concurrency contract**, documented in `web/state.py` and
both client modules: database access stays on the event loop thread (the thread
that opened the sqlite connection); only blocking network calls go to
`asyncio.to_thread`; worker threads receive plain dicts, never sqlite `Row`
objects. A scheduler running on the same event loop keeps that invariant intact.
A background thread that touches the store would break it.

Add a CLI entry point (`python -m job_builder.cli ingest`) for manual and debug
runs. With WAL from 0.5 it can run alongside the app.

---

## Phase 3 - Triage and routing

The existing six Groq labels (Rejected / Offer / Interview / OA Received /
Acknowledgement / Unclear) are a different axis from the three requested
categories. Two levels are needed.

**3.1 Level 1 - routing.** Classify each prefiltered message as
`job_alert | job_update | job_acknowledgement | irrelevant`. New prompt, same
`GroqClient` plumbing. Keep the existing injection defence from
`clients/llm_client.py`: fenced email content, fixed label set, anything outside
it becomes the inert label. Email bodies remain untrusted third-party text.

**3.2 Level 2 - per-category extraction.** Each category gets its own handler
module under a new `pipeline/` package, one file per responsibility, per the
repo's organisation preference:

- `pipeline/router.py` - dispatch on category
- `pipeline/alerts.py` - Phase 4
- `pipeline/updates.py` - Phase 5
- `pipeline/acknowledgements.py` - Phase 5

**3.3 Cost note.** The Batches API gives 50% off and fits the backlog case -
first run over an existing mailbox - where latency does not matter. Live polling
should stay synchronous.

---

## Phase 4 - Job alerts to leads

**4.1 Per-board parsers, LLM as fallback.** LinkedIn and Indeed alert emails are
HTML with a repeating card structure that is stable enough to parse
deterministically. A parser is free, fast, exactly reproducible, and unit
testable against a saved fixture - all things an LLM extraction is not. Use the
LLM only for boards without a parser.

`pipeline/parsers/linkedin.py`, `parsers/indeed.py`, `parsers/generic_llm.py`,
registered in `parsers/__init__.py` - the same registry pattern `web/pages/`
already uses.

Wrinkle: alert links are tracking redirects. The board-native job ID is usually
already in the URL path or query (`/jobs/view/<id>/`, `?jk=<key>`), so extract
it rather than following the redirect. Following it costs a request and can burn
a single-use tracking token.

**4.2 `job_leads` table.** `(id, identity_key, title, company, location,
source_url, board, board_job_id, source_message_id, status, created_at)`, unique
on `identity_key` so the same posting arriving from three boards on three days
produces one lead. `status` covers new / interested / dismissed / promoted.

**4.3 Promote action.** `promote_lead(lead_id)` creates the `jobs` row and the
first `job_sources` row, and marks the lead promoted. This is the only path from
lead to application, and it stays user-initiated.

---

## Phase 5 - Updates and acknowledgements

**5.1 Job updates.** For a `job_update` message: resolve which job it refers to
(sender domain, company in subject, and the `job_sources` URLs give three
signals), then extract the status change. Then reuse what already exists -
`record_classification()`, `apply_ai_status()`, `undo_ai_status()`,
the confidence threshold, and the previous-status snapshot in
`utilities/store.py:380-456`. That machinery is already correct and already
reversible; the only new part is the message-to-job resolution.

**5.2 Acknowledgements.** For a `job_acknowledgement`: extract (title, company,
location), compute `identity_key`, then

- identity matches an existing job -> move it to `Applied` / `Pending`
- identity matches a `job_leads` row -> promote it, then set the status
- no match -> create the job directly, since an acknowledgement is proof the
  application was actually submitted

This is the one place where auto-creating a `jobs` row is right: the email is
evidence of a real application, unlike an alert which is just an advert.

**5.3 Keep every write reversible.** Same rule the existing code follows: record
what was replaced, and expose an undo. An auto-applied `Rejected` stamps a
response date, which drops the job out of the pool future scans check - that is
already flagged as the dangerous case in the store docstrings, and it stays
dangerous here.

---

## Phase 6 - Research and resume generation

**6.1 Hard prerequisite: structured experience data.** Today the resume is one
free-text blob - `resume_text` in the `profile` key/value table, edited through
`web/pages/text_storage.py`. Tailoring a resume to a posting means selecting and
ordering *specific bullets* against posting keywords, which cannot be done
reliably against an unstructured blob.

So Phase 6 starts with an `experiences` table: `(id, kind, organisation, role,
start_date, end_date, bullet, tags, impact)`. This is the P2 backlog item
"reusable experience bullets with tags" - it is a real prerequisite, not
polish, and it is the reason this phase is last.

**6.2 `clients/research_client.py`.** New module, same shape as the two existing
clients: all provider detail inside, nothing leaking to the UI, injectable HTTP
for tests. Claude Opus 5 with the server-side web search and web fetch tools:

```
tools=[
    {"type": "web_search_20260209", "name": "web_search"},
    {"type": "web_fetch_20260209", "name": "web_fetch"},
]
```

Web fetch only retrieves URLs already present in the conversation, so pass the
posting URL from `job_sources` in the prompt. Use structured outputs
(`client.messages.parse()` with a Pydantic model) so the research comes back as
typed fields - company summary, products, tech stack, recent news, posting
keywords - rather than prose to re-parse.

Store the result in a `job_research` table keyed by `job_id`, with a fetched-at
timestamp so it can be refreshed rather than re-run blindly.

**6.3 Generation.** Two steps, kept separate: select and rank experience bullets
against the extracted posting keywords, then render. Rendering should be a
deterministic template, per the existing backlog note "generate resume artifacts
through a deterministic template before adding AI-assisted wording" - that
ordering is right, because a template failure is debuggable and a model
hallucinating a job you never had is not.

On the renderer: the README mentions LaTeX. On a headless Ubuntu box, **Typst**
is the better choice - a single static binary versus a multi-GB TeX Live
install, and much faster. LaTeX remains the fallback if a specific existing
`.tex` template must be reused.

Write artifacts to the filesystem (`generated/<job_id>/resume.pdf`) with a DB
row pointing at them. Do not put PDFs in SQLite.

**6.4 On demand, not automatic.** Research and generation run when the user asks
for a specific lead, not for every alert that arrives. At roughly $0.30-0.50 a
call, automatic research on a daily LinkedIn digest would cost more per month
than the server.

---

## Testing

The existing suite is strong - 1,660 test lines against ~1,800 source lines,
with the HTTP call, the pacer clock, and the model client all injected. Keep
that discipline:

- `tests/test_identity.py` - the normalization functions, with adversarial pairs
  that must collapse and near-miss pairs that must not. Highest value tests here.
- `tests/test_migrations.py` - build a v0 database, migrate, assert every row
  survived. This is the one that protects real user data.
- `tests/test_parsers.py` - saved LinkedIn/Indeed alert HTML fixtures, scrubbed
  of personal data, asserting extracted postings.
- `tests/test_ingest.py` - fake History API responses; assert incremental sync,
  the expired-historyId fallback, and prefilter pass rates.
- `tests/test_research.py` - injected HTTP, no network, no API key. Same pattern
  as `tests/test_llm_classification.py`.

---

## Documentation debt to clear alongside

These are already wrong, and this feature makes them wronger:

- `README.md:7` and `.agents/IMPLEMENTATION_PLAN.md:8` say the UI is Tkinter. It
  is NiceGUI.
- `utilities/store.py:3` - "This module holds no Tkinter code".
- `clients/gmail_client.py:70` - "the Text widget".
- `README.md:73-77` and the `gmail_client.py` module docstring describe the
  headers-first, body-only-on-match privacy model. Phase 2 changes that.
- `.agents/IMPLEMENTATION_PLAN.md:93` - "Message IDs and headers only, no bodies
  at rest". Already false; the `email_matches` table has stored `body_text`
  since the classification feature landed.
- `AGENTS.md` says "Keep the app usable as a local desktop tool launched with
  `python app.py`" and "Do not introduce networked services without approval".
  This request is that approval for a long-running server. Record it in
  `AGENTS.md` the same way the Gmail decision was recorded ("User approved
  networked email access on 2026-07-29").

---

## Suggested order

Phase 0 and Phase 1 are both prerequisites and are independent of each other -
they can go in parallel. Phase 2 needs Phase 1's schema. Phase 3 needs Phase 2's
messages. Phases 4 and 5 both need Phase 3 and are independent of each other;
Phase 5 is lower risk because it reuses existing status-write machinery, so it
is the better first proof that the pipeline works end to end. Phase 6 needs
Phase 4 for leads and needs its own structured-experience prerequisite.
