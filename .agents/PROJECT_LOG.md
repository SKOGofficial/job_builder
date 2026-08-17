# Project Log

Use this file to record meaningful project changes, implementation decisions, and verification notes.

## 2026-08-17 - Rule classification, no review queue, and a fresh to-apply list

Measured first. The review queue held 312 messages, and 265 of them - 85% - were
job-board digests from five senders: LinkedIn job alerts, Glassdoor, ZipRecruiter,
Indeed match, Jobright. Each was shown beside a picker asking which of the user's
applications a list of ten unrelated roles belonged to. That question has no answer, so
the queue could only ever grow.

Three causes, all fixed:

- **Alerts were in the queue's definition at all.** `unlinked_messages()` selected every
  job category with no link, and an alert is by definition *not* about a submitted
  application. Removed with the rest of the feature.
- **Handlers could not record a failed attempt.** They selected their backlog as "in my
  category and linked to nothing". A digest carrying no parseable posting never gets a
  link, so it came back every cycle and was re-extracted at full model cost, for ever.
  New `messages.handled_at` (v5) makes "tried and found nothing" distinguishable from
  "not tried".
- **Nothing deterministic ran before the model.** Added `pipeline/classify.py`, a
  sender-and-subject rule tier ahead of the router.

The classifier was built against the stored mailbox rather than guessed at. Backtested
over all 655 labelled messages: rules fire on 458 (70%) and agree with the model on 434.
Of the 24 disagreements, all but two are the rules being right - the model labelled three
identical Amazon receipts `job_acknowledgement` and a fourth `irrelevant`, and called
"Welcome to MyGreenhouse" a job alert eleven times. A first draft included a tier
labelling anything from a board domain as an alert; it was measured, found to produce
four of the five genuine errors ("Terms of Service Updates", "Thank you for purchasing
Premium"), and removed. Declining is free; a wrong confident label is permanent.

Decisions worth keeping:

- **Subject intent is settled before sender reputation.** `jobs-noreply@linkedin.com`
  sends all three job categories, and `noreply@glassdoor.com` carries both job alerts and
  a forum digest separated only by display name.
- **"Thank you for your interest in X" gets no rule.** It opens acknowledgements and
  rejections in equal measure; only the body separates them. Guessing would silently mark
  live applications dead.
- **`inmail-hit-reply@linkedin.com` is deliberately not noise.** It looks like network
  chatter and is actually how a recruiter's approach arrives, carrying real roles.
- **A cycle with no provider has not "tried".** Handlers only stamp `handled_at` when
  they had a model to extract with, or when they resolved. Caught while dry-running
  against a copy of the real database: without the guard, a cooling-off provider would
  have permanently discarded parseable digests and real receipts.

To-apply list, keyed on a new `job_leads.posted_ts` (v6) taken from the alert email's
received time:

- Ordered by posting date, newest first, ahead of both `ready` and relevance. Applying
  early decides more than any signal that was outranking it.
- Open leads past `LEAD_FRESHNESS_DAYS` (14) are deleted each cycle. `applied` and
  `dismissed` are exempt - the first is the record of an application, and deleting the
  second lets the next alert re-suggest a rejected role.
- `created_at` could carry neither. It records when the pipeline read the email: a
  backfill stamped 127 leads with one creation minute for postings spanning three weeks,
  which would order the list arbitrarily and then expire all of it in a single day.

Classification also moved out of the model-gated stages, since the rule tier needs no
provider. An exhausted free tier used to freeze the to-apply list for the rest of the day
over work that costs nothing.

Verification:

- 623 tests pass, up from 568. New suites: `test_classify.py` (rules, built from real
  senders and subjects), `test_lead_freshness.py` (ordering, purge exemptions, the
  `NULLIF` that stops two unknown dates collapsing to epoch 0), `test_handled_marker.py`
  (the retry leak, backlog ordering, and the no-provider guard). Migration tests cover v5
  and v6 including both backfills.
- Migrations and the purge were exercised against a **copy** of
  `job_applications.sqlite3`, never the file itself. On that copy: schema reaches v6, 365
  leads all get a posting date, 180 stale open leads are purged, 118 remain, and the
  applied and dismissed counts are untouched. UI verified against the same copy.
- Stored labels are deliberately left alone. Re-running the rules over history would
  relabel 24 messages, but every lead they would produce is already past the freshness
  window, so it is churn for no gain.

Known gaps:

- `/settings` returns 500 when the database names a provider the running build does not
  know about - reproducible today with the `claude_cli` fallback row written by the CLI
  provider branch. Pre-existing and unrelated, but it breaks the very page you would use
  to fix the setting.
- The rules cover the boards in this mailbox. A new board falls through to the model,
  which is the safe direction, but `MessageRouter.by_rule` is the number to watch: a
  collapse means a sender or subject format changed.

## 2026-08-04 - Gemini as a second engine, and per-task model routing

Groq's free tier was the throughput ceiling on the whole pipeline. A 429 ended a stage:
each handler caught `GroqRateLimited`, broke, and kept what it had written. Safe, but the
backlog only drained as fast as one free tier allowed. This adds Gemini as a real second
provider and a small orchestrator that decides per task which model runs, tracks what each
has spent, fails over when one runs out, and paces so a provider chugs along instead of
bursting and stalling. Gemini is also now the *primary* for research, with Claude behind it.

Every classification call in the project already funnelled through one signature -
`complete_json(messages, parser, fallback, max_tokens)` - and every stage took an injected
client. So the pool hands each stage a task-bound view of the same shape, and **six of the
seven pipeline modules changed not at all**. That property is the design's main correctness
guarantee, and `tests/test_provider_pool.py` drives the real `AlertHandler.run` rather than
a mock precisely to defend it.

Decisions worth knowing before changing any of this:

- **`GroqRateLimited` became an alias of `ProviderRateLimited`, not a subclass.** Six
  modules catch it by name. A subclass would mean those catches did *not* fire for a Gemini
  429 - the exact opposite of what was needed. The cost, accepted knowingly:
  `GroqNotConfigured` and `ResearchNotConfigured` are now literally the same class.
  `SpendCeilingReached` stays distinct, because `prepare.py` reads it as "stop the stage"
  while a rate limit means "stop this pass".
- **Pacing and failover turned out to be one decision.** A provider needing 400s to honour
  its daily spread is, from the caller's side, simply unavailable, so `spread_delay` feeds
  the same comparison as every other pacing rule and exceeding it fails over. No special
  case was needed.
- **"Default to Groq and wait" is bounded.** `dispatch()` and `prepare()` are called
  synchronously from the async `run()`, so an unbounded sleep freezes the UI. Two budgets:
  2s inline, 45s off the loop - the latter under the scheduler's 60s minimum interval, so a
  wait can never overlap two cycles. Past the budget, "wait" means what it already meant
  here: raise, keep what is written, resume next cycle.
- **A 429 overrides the local counters.** A shared project quota or a limit lowered upstream
  can exhaust an allowance our arithmetic did not predict, so the provider's own refusal
  wins, and a day-scoped one is persisted so a restart cannot un-exhaust it.
- **One budget, cooldown and pacer per provider, not per client.** Gemini holds two clients
  because grounded research and JSON-mode classification cannot share a request body, but
  they spend one project quota. Registering them separately would have let a classification
  429 leave research hammering the same exhausted key.
- **API finding, verified against Google's docs and issue tracker:** Google Search grounding
  and `responseMimeType: "application/json"` are **mutually exclusive** - together they
  return HTTP 400, "Function calling with a response mime type: 'application/json' is
  unsupported". `responseSchema` likewise. Research needs the tool, so it cannot have the
  response type; the reply is plain text and `parse_research` digs the JSON out, tolerating
  fences and prose. `tests/test_gemini_research.py` asserts the pairing directly, because if
  it regresses every research call fails and the request still looks reasonable.
- **Corrected a stale model id.** `gemini-2.0-flash`, used in the first commit of this
  branch, has been shut down by Google. Now `gemini-3.6-flash`, pinned rather than the
  `gemini-flash-latest` alias: that alias is hot-swapped on every release, which would
  change classifier behaviour with no commit behind it.
- **New tables need no migration entry**, because `create_tables` runs
  `CREATE TABLE IF NOT EXISTS` unconditionally before the version gate. New *columns* do.
  `email_matches.ai_model` deliberately goes through `ensure_email_match_columns` rather
  than `migrate_v3`: that table predates the gate, so a database can report a current
  `user_version` and still lack the column, and a gated migration would skip it.

Schema v3: `messages.category_model`, `email_matches.ai_model`, plus `provider_usage` (one
row per call, so a daily ceiling survives a restart) and `provider_settings` (routing; an
absent row means "follow .env", which is why Reset deletes rather than writing a default).

Verified: 492 tests pass on Python 3.14. The app was also launched against a throwaway
database - all three providers degrade with a readable reason when unconfigured, a pipeline
cycle completes cleanly, and the Settings cards render in both the provider and routing
halves. The real `job_applications.sqlite3` was not touched.

Not done, and deliberately: no time-of-day routing (routing is per task, and "when" is
handled by budget-aware pacing); no per-provider confidence threshold - the threshold is a
property of the classification, so `LLM_CONFIDENCE_THRESHOLD` is global with
`GROQ_CONFIDENCE_THRESHOLD` still read for existing setups.

## 2026-07-31 - UI moved from Tkinter to NiceGUI

- Replaced the Tkinter UI with NiceGUI under `web/`. `pages/` was deleted; `app.py` is now just
  an entry point. The backend under `clients/` and `utilities/` carried over unchanged in
  behaviour, which was the point of keeping it UI-free.
- Removed the last UI coupling in `clients/`: `gmail_client.py` imported `tkinter.messagebox`.
  `GmailWorkflow` became the async `GmailScanner`, publishing progress to subscribers instead
  of opening dialogs.
- `ClassificationRunner` moved from `threading.Thread` + `queue.Queue` + `after()` polling to an
  async cycle with an injectable executor (`asyncio.to_thread` by default). Database access
  stays on the calling thread; only the blocking HTTP call is offloaded.
- Hand-drawn Canvas charts (~190 lines) were replaced by `ui.echart`, and the Treeview by
  `ui.table`, which brings sorting, filtering, and pagination.
- `utilities/theme.py` lost its ttk style sheet and keeps only domain vocabulary and chart
  colours.
- Tests: the Tkinter render tests were replaced by `tests/test_web_pages.py`, which drives the
  app through NiceGUI's user simulation — no browser, no display, so CI no longer needs Tk or
  xvfb. `pytest` now runs both the unittest backend suites and the page tests.
- Verified: 128 tests pass. Rendering of every route was also checked against a running server
  with a seeded database, including that both ECharts draw to real canvases.

## 2026-07-29 - Codebase reorganization: clients, utilities, and tests

- Organized client integrations into `clients/` directory (`clients/gmail_client.py` and `clients/llm_client.py`).
- Combined `gmail_client.py` and `gmail_workflow.py` into a single module `clients/gmail_client.py` containing OAuth mechanics, Gmail API calls, and `GmailWorkflow` UI orchestration.
- Organized utility files into `utilities/` (`utilities/store.py` and `utilities/theme.py`) and provided a `utilties/` alias package for compatibility.
- Updated `JobStore.DB_PATH` in `utilities/store.py` to calculate absolute path relative to project root (`BASE_DIR`) so database resolution is robust across working directories.
- Organized unit and UI integration tests into `tests/` directory (`tests/test_gmail_matching.py` and `tests/test_app_pages.py`).
- Updated relative imports across `app.py`, `pages/`, `clients/`, `utilities/`, `tests/`, and `.github/workflows/tests.yml`.
- Removed redundant root-level files (`gmail_client.py`, `gmail_workflow.py`, `llm_client.py`, `store.py`, `theme.py`, `test_app_pages.py`, `test_gmail_matching.py`).

Verification:
- `python -m compileall -q app.py clients utilities utilties pages tests` passed with 0 errors.
- `python -m unittest discover -s tests -p "test_*.py"` passed (34 tests run, 0 failures, 0 errors).

- Moved `AGENTS.md`, `CODEX.md`, `IMPLEMENTATION_PLAN.md`, and `PROJECT_LOG.md` into `.agents/` directory.
- Configured configuration pointers in `.claude/` and `.codex/` so all agents (Claude, Codex, Antigravity) automatically discover their instruction files.
- Updated `README.md` and document internal links to reference `.agents/` paths.

## 2026-07-29 - Gmail reply detection

- User explicitly approved networked email access, satisfying the AGENTS.md constraint against
  adding email integration without approval.
- Added `gmail_client.py` holding all OAuth and Gmail API mechanics, kept out of `app.py`.
- Added `email_matches` table through `CREATE TABLE IF NOT EXISTS`, which is additive and leaves
  existing rows in `job_applications.sqlite3` untouched.
- Added an Email matches page plus Gmail connect, disconnect, and Check for replies in Settings.
- Added `requirements.txt`, `.env.example`, and a Python 3.14 `.venv`. `.env`, `client_secret.json`,
  and `.venv/` are gitignored.
- Added `test_gmail_matching.py` with 22 tests covering slug handling, query building, sender
  parsing, match rules, and the email_matches store.

Key decisions:

- Matches are suggestions only. The app never writes a job status automatically. A heuristic
  email match applied silently would be hard to notice and hard to undo, so every match needs
  an explicit confirm or dismiss.
- Only `gmail.readonly` is requested, and messages are fetched with `format="metadata"` and a
  header allowlist, so message bodies are never downloaded or stored.
- The refresh token goes to Windows Credential Manager through `keyring`. The client ID and
  secret stay in `.env` because a Desktop OAuth client is a public client per RFC 8252; those
  values identify the app rather than granting mailbox access.
- Disconnect revokes with Google before deleting locally, so a failed revoke cannot strand a
  live token that the app can no longer see.
- Matching rejects free mail domains such as gmail.com as a domain signal, since short company
  slugs would otherwise match unrelated mail.
- The Gmail import is guarded so the tracker still launches as a local-only tool when the
  packages are absent.

Verification:

- `.venv\Scripts\python.exe -m unittest test_gmail_matching` passes, 22 tests, on Python 3.14.5.
- Rendered all seven pages programmatically against a temporary database with no errors, and
  confirmed the drawer lists Email matches.
- Not yet exercised against a live Google account; that needs the user's own OAuth client and
  browser consent.

## 2026-06-23 - Product report website redesign

- Reworked `website/index.html` and `website/styles.css` into a static product report for the Job Builder roadmap.
- Focused the page on current tracker capabilities, future dashboard metrics, Gmail and Grok response intelligence, and offer comparison planning.
- Kept the site informational only; no app code, email integration, API calls, compensation logic, or database behavior was implemented.
- Created the work on branch `codex/frontend-report-website`.

Verification:

- Checked for non-ASCII characters in changed static files.
- Reviewed the page in the browser at desktop and mobile widths.
- Confirmed no horizontal overflow and confirmed the next report section is visible in the first viewport.

## 2026-06-23 - Static product report website

- Added `website/index.html` and `website/styles.css` as a static report website for the Job Builder product vision.
- Covered current tracking capabilities, dashboard metric direction, future Gmail and Grok API response intelligence, and offer comparison planning.
- Kept the page informational only; no email, API, compensation, or database features were implemented.
- Updated `README.md` with the website entry point.

Verification:

- Static website files were created. No app code or database data was modified.

## 2026-06-23 - Documentation and planning pass

- Inspected repository structure, README, `app.py`, `.gitignore`, and current SQLite row counts.
- Confirmed the app is a local Tkinter and SQLite job application tracker.
- Confirmed the database file is ignored by git and currently contains existing local data.
- Added `IMPLEMENTATION_PLAN.md` with prioritized work for logging, UI polish, unique graphs, automation, resume workflow, and engineering quality.
- Added `CODEX.md` with project state, working rules, verification commands, and relevant skills for future Codex sessions.
- Added `AGENTS.md` with handoff notes, constraints, known state, and priority work.

Verification:

- Documentation-only change. No app code or database data was modified.

## 2026-08-02 - Mailbox ingest pipeline and job identity rework

Implements `.agents/EMAIL_PIPELINE_PLAN.md`. Branch `feature/email-ingest-pipeline`.

Identity model:

- Job identity moved from posting URL to a normalized (title, company, location) hash in
  `utilities/identity.py`. Normalization collapses spelling only - "Sr." to "senior",
  "Google LLC" to "Google" - and deliberately keeps seniority and level, because
  over-merging destroys one role's history invisibly while under-merging is only untidy.
- `job_id` stays the stable handle `email_matches` references; `identity_key` carries the
  identity. New `job_sources` table records many board URLs per role, with each board's
  own job ID.
- Real migrations added: `utilities/schema.py` (current shape) and `utilities/migrations.py`
  (`PRAGMA user_version` gate, table rebuild for the nullable `posting_url`, pre-migration
  backup). The v1 backfill reports identity collisions rather than merging them.

Pipeline (`pipeline/`, one module per stage):

- Gmail sync over the History API with a bounded full-sync fallback when the cursor expires.
- Rough filter that drops only what is confidently not job related, recording a verdict per
  message so drops are auditable. Deliberately permissive; the model does the real triage.
- Groq router labelling each message alert / update / acknowledgement / irrelevant.
- Resolver placing a message against a role, strongest signal first, refusing to guess when
  several roles remain plausible - those go to a review queue.
- Handlers: alerts become leads, acknowledgements promote leads into applications (dates
  taken from the email, not today), updates apply reversible status changes.
- Links point at `identity_key`, so a promoted lead keeps every email already attached.
- In-process asyncio scheduler, retention pass, and `cli.py` for backfills and maintenance.

Research and generation:

- `clients/research_client.py` uses Claude Opus 5 with server-side web search, gated by a
  Groq relevance score and capped by a daily output-token ceiling.
- `pipeline/generate.py` does deterministic bullet selection then template rendering
  (Markdown and HTML always; PDF via Typst when available). Artifacts keyed on
  `identity_key` so they survive promotion.

Server readiness:

- `--headless`, `--host`, `--no-poll` flags; logging to stderr for the journal; WAL and
  busy timeout on the connection; pinned OAuth redirect port so consent can be tunnelled;
  opt-in file backend for secrets where no keyring exists; `deploy/` systemd unit, backup
  script using `VACUUM INTO`, and backup timer.

Verification:

- 267 tests pass (`python -m unittest discover -s tests -t .`), up from 155. New suites:
  identity normalization, migrations against a frozen pre-migration schema, rough filter,
  resolver ambiguity, end-to-end lifecycle, ingest, and generation.
- No changes to `job_applications.sqlite3`. Migrations were exercised only against
  temporary databases, and take a backup before any structural change.

Known gaps:

- No UI yet for the to-apply list, the per-role email timeline, or the unlinked review
  queue. The store methods are in place (`messages_for_identity`, `unlinked_messages`,
  `list_leads`); the pages are frontend work.
- Duplicate identities from the v1 backfill are reported but there is no merge flow.
- Only LinkedIn and Indeed have deterministic parsers; other boards use the model fallback.
