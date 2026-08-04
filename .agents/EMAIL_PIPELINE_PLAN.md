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

## The shape the backend has to support

Added 2026-08-02. Everything below exists to serve three user-facing surfaces.
Design decisions should be checked against these, not against the pipeline in
isolation.

**1. Applied list, with a per-role email timeline.** Open any job you have
applied to and see every email about that role in date order - the
acknowledgement, the OA invite, the rejection. This is the requirement that
makes message-to-job linking a first-class concern rather than a side effect of
classification. It drives 3.3.

**2. To-apply list, application-ready.** Roles surfaced from job-board alert
emails that you have not applied to yet, each already carrying a tailored resume
and CV built from your stored experiences, plus a working link to the
application portal. The intent is: open the list, click through, fill in the
form, send. No "generate" step in the middle. It drives Phase 4 and changes
Phase 6 from on-demand to ahead-of-time.

**3. Acknowledgement emails move rows between the two lists.** An email thanking
you for applying to a role is the signal that it belongs on list 1 and no longer
on list 2. It drives 5.2.

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

### 2. Mirror scope - rough filter, then LLM does the real work

Decided 2026-08-02: the deterministic layer is a **rough filter only**. Its job
is to weed out mail that is obviously not from a job board or a company -
personal mail, social, and an explicit denylist. Everything else has its body
downloaded and goes to the LLM for classification. The LLM is the classifier;
the filter is a bouncer, not a triage nurse.

This trades cost for recall, deliberately. A precision-tuned prefilter would
drop 90%+ of the mailbox but would silently lose the one-off recruiter email
from a company that matches no keyword and no known board. That miss is
invisible - you never learn about the interview invite you did not get shown -
which is the worst failure mode for this app.

The cost is affordable on the steady-state path. A typical personal mailbox is
30-100 messages a day; if 70% survive the rough filter that is ~50 LLM calls at
roughly 900 tokens each. Groq's free tier ceiling is 12,000 tokens/min, so the
existing `Pacer` in `clients/llm_client.py` clears a day's mail in about five
minutes of wall time. **The expensive case is the first run over an existing
mailbox**, which is thousands of messages - see 2.6 for how to bound it.

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

**1.5 Coordinate with the frontend agent.** This phase changes what a job _is_.
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
  body_text,            -- NULL until the rough filter lets it through (2.3)
  filter_verdict,       -- passed | dropped_<rule>, so drops are auditable
  category,             -- NULL until classified (Phase 3)
  category_confidence, category_reason, classified_at,
  fetched_at, body_fetched_at
)
```

`filter_verdict` is not optional bookkeeping. When the user asks "why didn't I
see that recruiter email", the answer has to be recoverable, and a dropped
message with no recorded reason is unanswerable.

**2.2 Incremental sync via the History API.** Do not re-run `messages.list` on
every poll. Store the mailbox `historyId` and call `users.history.list` with
`startHistoryId` to get only what changed. Gmail expires history after roughly a
week, so handle the 404 by falling back to a bounded full `messages.list` sync
and re-seeding `historyId`. This is the difference between a poller that costs
nothing and one that burns quota.

Quota is not a concern at personal-mailbox scale: 1M units/day, and
`history.list` is 2 units, `messages.get` 5. A 5-15 minute poll interval is
comfortable.

**2.3 Rough filter (deterministic, drops only the obvious).** Revised
2026-08-02. This runs on headers alone, before any body download, and drops a
message **only when it is confidently not from a job board or a company**.
Anything it is unsure about survives and goes to the LLM.

Drop rules, all header-only:

1. Sender is a free-mail domain (the existing `GENERIC_DOMAINS` set in
   `clients/gmail_client.py:322`) **and** the subject contains no job keyword.
   The keyword escape hatch matters - small companies and independent recruiters
   do send from Gmail.
2. Gmail label is `CATEGORY_SOCIAL` or `CATEGORY_FORUMS`. Do **not** drop
   `CATEGORY_PROMOTIONS` or `CATEGORY_UPDATES` - LinkedIn and Indeed job alerts
   routinely land in both.
3. Sender domain is on a user-maintained denylist (bank, utilities,
   newsletters, shopping). This is the rule that actually does the work over
   time, and it needs a UI affordance: a "not job related" button on any
   message that adds its domain to the list.
4. Automated bulk mail with no job keyword anywhere in the headers - a
   `List-Unsubscribe` header plus a subject that misses the keyword list.

Everything else -> fetch the body -> classify.

Be honest about the ceiling here: "is this from a company" is close to
unfalsifiable, since nearly all bulk mail is from some company. Rules 1 and 2
are cheap wins; rule 3 is the one that compounds. Expect the filter to remove
somewhere between a third and half a mailbox, not 90%, and size the LLM budget
for that.

Keep the domain lists and keyword list in a data file, not inline in code, so
they can be tuned without a code change. Log the drop rate per rule - if rule 3
is not growing, the "not job related" button is not discoverable enough.

**2.4 Retention and bloat control.** With most bodies now being downloaded, this
matters more than it did under the precision-prefilter design. Two jobs:

- Drop `body_text` for messages the LLM classified `irrelevant`, older than N
  days, keeping the ID, headers, and the classification so they are never
  re-fetched or re-classified.
- Cap stored body length. `MAX_BODY_CHARS` in `clients/gmail_client.py:71` is
  already 20,000; keep that for linked messages and consider a much lower cap
  for anything classified irrelevant, since its only remaining purpose is audit.

`VACUUM` after a prune run, or the file never shrinks.

**2.5 First-run backfill.** The steady-state path is cheap; the initial pass
over an existing mailbox is not. Bound it:

- Default to a date cutoff (last 6-12 months), configurable, rather than the
  whole mailbox.
- Run the backfill through the Groq classification path in the background with
  the existing `Pacer`, and make it resumable - the same "resume from the first
  unclassified message" behaviour `ClassificationRunner` already has.
- Surface progress and let it be paused. A first run over several thousand
  messages is hours of paced calls, and it must not block the app.

**2.6 Scheduler.** An in-process `asyncio` task started at app boot, holding a
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

**3.1 Level 1 - routing.** Classify every message that survived the rough filter
as `job_alert | job_update | job_acknowledgement | irrelevant`. New prompt, same
`GroqClient` plumbing. Keep the existing injection defence from
`clients/llm_client.py`: fenced email content, fixed label set, anything outside
it becomes the inert label. Email bodies remain untrusted third-party text.

`irrelevant` is now a real and common outcome, not an error case - the rough
filter deliberately passes plenty of non-job mail through. Make sure the prompt
says so, or the model will strain to fit ordinary marketing mail into one of the
three job labels.

**3.2 Level 2 - per-category extraction.** Each category gets its own handler
module under a new `pipeline/` package, one file per responsibility, per the
repo's organisation preference:

- `pipeline/router.py` - dispatch on category
- `pipeline/resolver.py` - 3.3, message to job identity
- `pipeline/alerts.py` - Phase 4
- `pipeline/updates.py` - Phase 5
- `pipeline/acknowledgements.py` - Phase 5

**3.3 Linking messages to jobs.** Added 2026-08-02. This is what makes the
per-role email timeline possible, and it is the piece with the most design risk
in the whole plan.

**Link on `identity_key`, not on a row ID.** Both `jobs` and `job_leads` carry
`identity_key` (Phase 1). If links point at the identity rather than at a
`jobs.id` or a `job_leads.id`, then a lead that later gets promoted to a real
application keeps every email already attached to it, with no migration and no
re-linking step. The alert email that first surfaced the role stays visible on
the job's timeline forever, which is the correct behaviour.

```
message_links(
  id INTEGER PRIMARY KEY,
  gmail_message_id TEXT NOT NULL,
  identity_key TEXT NOT NULL,
  link_type TEXT NOT NULL,     -- alert | update | acknowledgement
  confidence REAL,
  resolved_by TEXT,            -- exact_identity | board_job_id | domain+title | llm | manual
  created_at TEXT NOT NULL,
  UNIQUE(gmail_message_id, identity_key)
)
```

**Many-to-many is required, not a nicety.** One alert email contains 5-10
postings, so it links to 5-10 identities. A single nullable `messages.job_id`
column cannot express that, which is why this is a link table.

Resolution order, strongest signal first:

1. `board_job_id` matches a row in `job_sources` - exact, no ambiguity.
2. Computed `identity_key` from the extracted (title, company, location) matches
   an existing `jobs` or `job_leads` row - exact.
3. Sender domain matches a known company **and** the subject or body names a
   title close to one open application at that company - good, but degrade the
   confidence.
4. Sender domain matches a company with several open applications and nothing
   disambiguates the title - **do not guess.** Link nothing; queue it.

**3.4 The unlinked queue.** Rule 4 above is not an edge case; it is common (three
applications at the same large company, an update email that just says "your
application"). A message classified as job-related but not confidently resolved
must land in a review list where the user picks the job, and that choice writes
a `message_links` row with `resolved_by = 'manual'`.

Without this queue, unresolved job mail silently disappears - classified, stored,
and attached to nothing. That is a worse failure than not classifying it at all,
because the app will look like it is working.

**3.5 Store methods.** `messages_for_identity(identity_key)` returns the
timeline for a job detail page, ordered by `received_date`. `unlinked_messages()`
returns the 3.4 queue. `link_message(message_id, identity_key, ...)` and
`unlink_message(...)` cover the manual path - and unlink matters, because a wrong
auto-link needs to be correctable.

**3.6 Cost note.** The Batches API gives 50% off and fits the 2.5 backfill, where
latency does not matter. Live polling should stay synchronous.

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

**4.2 `job_leads` table.**

```
job_leads(
  id INTEGER PRIMARY KEY,
  identity_key TEXT NOT NULL UNIQUE,
  title, company, location,
  apply_url,            -- canonical, not the tracking wrapper (4.4)
  board, board_job_id,
  source_message_id,
  relevance_score, relevance_reason,   -- 6.5 gate
  status,               -- new | preparing | ready | dismissed | applied
  created_at
)
```

Unique on `identity_key`, so the same posting arriving from three boards over
three days produces one lead rather than three.

`status` carries the artifact lifecycle, because the to-apply list needs to
distinguish "we have not looked at this yet" from "resume is being written" from
"go click this now". `ready` is the state the user actually cares about, and the
list should default to filtering on it.

**4.3 Promote action.** `promote_lead(lead_id)` creates the `jobs` row and the
first `job_sources` row, carrying `identity_key` across unchanged so every
`message_links` row already attached to the lead now resolves to the job. Sets
the lead to `applied`.

Two callers: the user clicking "I applied to this", and the acknowledgement
handler in 5.2 doing it automatically when a thank-you email arrives.

**4.4 Give the list a link that actually works.** The href in an alert email is
a tracking redirect, and those can be single-use or expire - a stale one sends
the user to a dead page, which defeats the whole "click through and apply"
flow.

Build the canonical URL from the board and the board job ID instead
(`linkedin.com/jobs/view/<id>`, `indeed.com/viewjob?jk=<key>`), and keep the
original tracking URL in `job_sources` as a fallback. Where no board-native ID
can be extracted, store the tracking URL and mark the lead so a dead link is
diagnosable rather than mysterious.

**4.5 Dedupe against what you have already applied to.** Before creating a lead,
check `identity_key` against `jobs`. A job board will happily keep recommending
a role you applied to three weeks ago, and a to-apply list that keeps
resurrecting finished work stops being trusted quickly.

---

## Phase 5 - Updates and acknowledgements

**5.1 Job updates.** For a `job_update` message: resolve the target through
`pipeline/resolver.py` (3.3), write the `message_links` row so the email appears
on that job's timeline, then extract the status change. Then reuse what already
exists - `record_classification()`, `apply_ai_status()`, `undo_ai_status()`, the
confidence threshold, and the previous-status snapshot in
`utilities/store.py:380-456`. That machinery is already correct and already
reversible; the only new parts are the resolution and the link.

Link the message **even when the confidence is too low to apply a status
change.** Showing the user an email on the right job and letting them decide is
useful on its own; those two decisions are independent.

**5.2 Acknowledgements - the bridge between the two lists.** Revised 2026-08-02.
For a `job_acknowledgement`: extract (title, company, location), compute
`identity_key`, link the message, then

- **matches a `job_leads` row** -> `promote_lead()`, set the job to `Applied`,
  stamp `application_date` from the email's received date. The role leaves the
  to-apply list and appears on the applied list with its email history already
  attached. This is the main path and the one to get right.
- **matches an existing job** -> move it to `Applied` if it is still `Pending`,
  and backfill `application_date` if it is empty.
- **matches nothing** -> create the `jobs` row directly. An acknowledgement is
  evidence the application was really submitted, so this is the one place
  auto-creating a job is correct - unlike an alert, which is just an advert.

Set `application_date` from the email, not from `today_iso()`. Mail gets
processed late - after a backfill, after downtime - and a wrong application date
quietly corrupts the dashboard's time series.

**5.3 Prefer the lead's data over the email's.** When an acknowledgement
promotes a lead, the lead already has clean structured fields from the board
parser. The acknowledgement email's extracted title is often looser ("your
application to our Engineering team"). Keep the lead's title, company, location,
and `apply_url`; take only the date and the confirmation from the email.

**5.4 Keep every write reversible.** Same rule the existing code follows: record
what was replaced, and expose an undo. An auto-applied `Rejected` stamps a
response date, which drops the job out of the pool future scans check - that is
already flagged as the dangerous case in the store docstrings, and it stays
dangerous here.

---

## Phase 6 - Research and resume generation

**6.1 Hard prerequisite: structured experience data.** Today the resume is one
free-text blob - `resume_text` in the `profile` key/value table, edited through
`web/pages/text_storage.py`. Tailoring a resume to a posting means selecting and
ordering _specific bullets_ against posting keywords, which cannot be done
reliably against an unstructured blob.

So Phase 6 starts with an `experiences` table: `(id, kind, organisation, role,
start_date, end_date, bullet, tags, impact)`. This is the P2 backlog item
"reusable experience bullets with tags".

**Start this one early.** It depends on nothing else in this plan, and it is
partly data entry rather than code - the bullets have to be written before
anything can select from them. Left until last it becomes the thing blocking a
finished pipeline. See the revised ordering at the end of this document.

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

Store the result in a `job_research` table keyed by `identity_key` (same reason
as 6.6 - it has to survive lead promotion), with a fetched-at timestamp so it
can be refreshed rather than re-run blindly.

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

**6.4 Ahead of time, not on demand.** Revised 2026-08-02. The to-apply list must
already have a resume and CV attached when the user opens it - no "generate"
button in the middle of the flow. So generation is triggered by lead creation,
runs in the background, and moves the lead `new -> preparing -> ready`.

Failures must be visible, not silent: a lead whose generation errored stays out
of `ready` and shows the reason, so the list never contains a row whose "open
resume" link is dead.

**6.5 Gate generation on relevance, or this gets expensive.** Straightforward
arithmetic: a daily LinkedIn digest is 5-10 postings, and at roughly $0.30-0.50
of Opus tokens per lead that is $45-150/month - more than the homelab costs to
run, and most of it spent on roles that will be dismissed at a glance.

The fix keeps the UX exactly as specified while cutting most of the spend: score
each new lead for relevance **before** researching it, using Groq (free, already
wired up, already paced) against the stored profile and target roles. Only leads
above the threshold get the Opus research and generation pass; the rest sit at
`new` with their score visible, and a manual "prepare this one anyway" button
covers the misses.

Store `relevance_score` and `relevance_reason` on the lead (4.2) so the
threshold can be tuned against real data rather than guessed. Set it
deliberately low at first - a missed good lead costs more than a wasted dollar.

Also add a hard daily spend ceiling on the research client. A parser bug that
turns one alert email into 400 leads should cost a few dollars and an alert, not
a month's budget.

**6.6 Artifacts belong to the identity.** Write to
`generated/<identity_key>/resume.pdf`, not `generated/<job_id>/`. A lead becomes
a job on promotion and its `job_id` is assigned at that moment; keying the
artifacts on `identity_key` means nothing has to move or be regenerated when
5.2 fires. Record the paths in a `job_artifacts` table keyed on `identity_key`,
with a generated-at timestamp and the model used.

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
  of personal data, asserting extracted postings and canonical apply URLs (4.4).
- `tests/test_ingest.py` - fake History API responses; assert incremental sync,
  the expired-historyId fallback, and per-rule rough-filter drop counts. Include
  a fixture for the case the filter must **not** drop: a recruiter emailing from
  a Gmail address with a job keyword in the subject.
- `tests/test_resolver.py` - added 2026-08-02. Each resolution tier in 3.3, and
  critically the ambiguous case: two open applications at the same company, an
  update email that names neither title. Assert it links nothing and queues.
- `tests/test_lifecycle.py` - added 2026-08-02. The end-to-end row movement:
  alert email creates a lead, acknowledgement promotes it to a job, the alert
  email is still on the job's timeline afterwards. This is the one that proves
  the `identity_key` linking decision actually works.
- `tests/test_research.py` - injected HTTP, no network, no API key. Same pattern
  as `tests/test_llm_classification.py`. Include the spend-ceiling cutoff (6.5).

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

Revised 2026-08-02, because Phase 6 is no longer optional polish - the to-apply
list is defined as application-ready, so it is not finished until generation
exists.

Phase 0 and Phase 1 are both prerequisites and are independent of each other -
they can go in parallel. Phase 2 needs Phase 1's schema. Phase 3 needs Phase 2's
messages.

Phases 4 and 5 both need Phase 3 and are independent of each other. **Do Phase 5
first.** It reuses the existing status-write machinery, so it is the cheapest
end-to-end proof that ingest -> classify -> link -> act works, and it delivers
surface 1 (the applied list with its email timeline) on its own. Phase 4 alone
delivers a to-apply list with no resumes attached, which is not the requested
feature.

Phase 6 splits, and the split matters:

- **6.1 (the `experiences` table) is a hard prerequisite** and does not depend on
  anything else in this plan. Start it early - in parallel with Phase 0/1 if
  there is capacity. It is data entry as much as code, and it will be the thing
  blocking the finish line otherwise.
- 6.2-6.6 need Phase 4's leads and 6.1's experiences.

So the critical path to the full feature is: 1 -> 2 -> 3 -> 5 -> 4 -> 6.2-6.6,
with 0 and 6.1 running alongside from the start.
