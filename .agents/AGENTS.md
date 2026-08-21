# Agent Handoff Notes

This repository is a local job application tracker. Future agents should start here, then read `README.md`, `.agents/IMPLEMENTATION_PLAN.md`, and `.agents/CODEX.md`.

## Mission

Help the user log and manage all job applications locally. The app should reduce duplicate applications, keep response and OA status visible, and provide dashboard views that are meaningfully different from each other.

## Important Constraints

- Do not modify or remove `job_applications.sqlite3` unless the user explicitly asks and a backup path is clear.
- Do not commit local database files.
- Do not introduce networked services or email access without explicit user approval.
- Keep generated or changed files ASCII unless the existing file clearly requires otherwise.
- Keep the app usable as a local desktop tool launched with `python app.py`.

## Documentation Requirement For New Functions And Methods

- Every function or method an agent adds must include a docstring or structured comment with:
  - `Summary`: one short sentence describing what the function does. Always present.
  - `Parameters`: each parameter name, type, and purpose.
  - `Returns`: what the function returns and what that value means.
  - `Raises`: any exceptions the function may raise and why.
- **Omit a section that would be empty.** A function that takes no arguments has no
  `Parameters` block, one that returns nothing has no `Returns` block, and one that raises
  nothing has no `Raises` block. Never write `Parameters: None.` - the absence of the section
  is the statement. Only `Summary` is unconditional.
- Where a docstring already carries prose explaining *why* the code is the way it is, keep that
  prose as the opening line or paragraph and add the structured fields beneath it. The prose is
  the more valuable half; do not replace it with fields.
- An optional `Note` section may follow the required ones, for a hazard or non-obvious
  consequence a caller needs to know - for example that a write drops a row out of a pool
  another stage reads.
- Apply this requirement to all new code, including helpers, managers, UI callbacks, async workers, and pipeline stages.

## Approvals On Record

- **2026-07-29** - user approved networked email access (Gmail, read-only).
- **2026-08-02** - user approved running as a long-running service on an Ubuntu home
  server, polling Gmail on a schedule and mirroring mail locally. This supersedes the
  desktop-only reading of the constraint above: `python app.py` must still work as a
  desktop tool, but `--headless` plus a systemd unit is now a supported deployment.
- **2026-08-02** - user approved a second model provider (Anthropic) for company research
  and resume generation, alongside the existing Groq classification.
- **2026-08-04** - user approved a third model provider (Google Gemini) as a second engine
  for classification and as the *primary* for research, with per-task routing editable in
  Settings. Research falls back to Anthropic.

## Cost And Safety Rules Specific To This Repo

- **The app has no authentication.** `host="127.0.0.1"` is the entire access control model.
  Never change the default bind without saying plainly what it exposes.
- **Never auto-merge two jobs** that collapse onto one `identity_key`. Report them. A wrong
  merge destroys application history and the user cannot see that it happened.
- **Never let the resolver guess** between several plausible roles. Link nothing and queue
  it for review.
- **Every automatic status write must be reversible**, capturing the previous status and
  response date first. Applying `Rejected` stamps a response date, which drops the job out
  of the pool future scans check.
- **Keep the expensive model behind the relevance gate and the daily spend ceiling.**
  Removing either turns a parser bug into a large bill. Gemini being primary for research
  lowers the bill but does not replace either guard: Claude still picks up whatever Gemini
  cannot take.
- **Every model call must record which model served it.** The confidence threshold is
  global by design, so the recorded model name is the only thing that can attribute a bad
  label afterwards.

## Code Organisation Preference

The user wants code organised by section and purpose, split into relevant directories rather
than collected into large multi-purpose files. Follow this by default; do not consolidate
modules back into one file.

- One responsibility per module, named for what it does.
- Group related modules into a directory once there is more than one of a kind. UI pages live
  in `pages/`, one module per page.
- Each external service gets its own manager module at the top level, following the shape of
  `gmail_client.py`: all provider detail inside, nothing leaking into the UI. `llm_client.py`
  is the placeholder for LLM work.
- Keep `app.py` an orchestrator. It owns the window, theme, navigation, and shared widget
  helpers, and delegates everything else.
- Keep persistence in `store.py` and presentation constants in `theme.py`, so neither imports
  the UI. Nothing under `clients/` or `utilities/` may import a UI framework.
- When adding a page, create the module under `web/pages/` and import it in
  `web/pages/__init__.py`. Routes register themselves through `@ui.page`.

## Known State

- `app.py` is the entry point: argument parsing and `ui.run`. The shell (header, drawer, dark
  mode) lives in `web/shell.py`.
- `utilities/` holds utility modules: `store.py` (`JobStore`, `normalize_url`, `url_hash`, `today_iso`) `theme.py` (`STATUS_COLORS`, `CHART_COLOR`, `TIME_RANGES`, option lists), and `credentials.py`
  (keyring access that degrades when no backend exists).
- `web/` holds the NiceGUI UI: `shell.py` for page chrome, `state.py` for the shared store and
  workers, and one module per page under `web/pages/`. Profile and Resume share
  `web/pages/text_storage.py`. Pages are rebuilt per request, so anything that must survive
  navigation belongs on `AppState`.
- `clients/` holds external client integrations, none of which imports a UI framework:
  `gmail_client.py` (OAuth, Gmail API calls, History-API sync, and the async `GmailScanner`),
  `llm_client.py` (Groq config, prompting, `complete_json`, and the async
  `ClassificationRunner`), `research_client.py` (Claude with server-side web search, plus
  the `SpendLimiter`), and `gemini_research.py` (Gemini with Google Search grounding). All
  take an injectable caller/executor so tests never reach the network.
- `clients/providers/` holds the multi-provider machinery: `base.py` (the neutral
  exceptions, `Pacer`, `estimate_tokens`), `gemini.py` (the Gemini transport),
  `claude_cli.py` (the Claude Code CLI driven headlessly as a subprocess, both shapes),
  `budget.py`
  (per-day allowances and spread pacing), `routing.py` (the `TASKS` registry and chain
  resolution), and `pool.py` (`ProviderPool`, and the failover state machine). Three things
  here are load-bearing and easy to break:
  - **`GroqRateLimited` is an *alias* of `ProviderRateLimited`, not a subclass.** Six
    pipeline modules catch it by name to stop a batch cleanly. Subclassing would mean those
    catches missed a second provider's 429 - the exact opposite of what is needed.
  - **A task's `max_tokens` must cover the model's *thinking*, not just its answer.**
    Gemini's flash models reason before replying and `maxOutputTokens` caps both together,
    so a budget sized to the output arrives truncated mid-JSON and the parser returns its
    empty fallback. `draft_referral` declares 2000 for a 200-token email for this reason;
    measured, 700 was cut off after the subject line.
  - **`ProviderRequestTooLarge` fails over; it never cools a provider down.** It means
    "this payload is too big for me", not "I am unavailable", and the next message may be a
    tenth the size. It is an *alias target* for `GroqRequestTooLarge` under the same rule as
    `GroqRateLimited`, and it is deliberately **not** a rate limit: Groq sends HTTP 413 both
    for a payload the model will not take and for a request whose prompt plus `max_tokens`
    exceeds the whole per-minute token allowance, and neither improves by waiting.
  - **`ProviderState` has one budget, cooldown and pacer per *provider*, not per client.**
    Gemini holds two clients because grounded research and JSON-mode classification cannot
    share a request body, but they spend one project quota.
  - **The pool never touches sqlite from a worker thread.** `begin_cycle` and `flush` run on
    the loop; everything between them counts in memory.
- `pipeline/` holds the mailbox ingest pipeline, one module per stage: `sync`, `rough_filter`,
  `router`, `resolver`, `extract`, `alerts`, `updates`, `acknowledgements`, `relevance`,
  `generate`, `prepare`, `orchestrator`, `scheduler`, plus board parsers under
  `pipeline/parsers/` registered in its `__init__.py`. Nothing here imports a UI framework, so
  the whole pipeline runs from `cli.py` as well as the app.
- `pipeline/referrals.py` and `pipeline/referral_email.py` serve `web/pages/referrals.py`.
  They are not scheduler stages: matching runs at page load and costs nothing, and
  `OpeningsChecker` runs only when the user presses a button. Three things there are
  deliberate:
  - **Matching is read-time, never stamped on a lead.** Contacts are added and edited after
    leads arrive, and a flag written at lead creation would need a backfill on every edit.
  - **`is_new_for` falls back from `posted_ts` to `created_at`**, the same fallback
    `purge_stale_leads` uses. Undated rows are immortal there and would be permanently new
    here, and a badge that cannot be cleared stops being read.
  - **`score_bullet` picks the evidence, not the model** - the `cover_letter.py` rule, and
    it matters more here, because the reader knows the applicant personally.
- Schema lives in `utilities/schema.py` (current shape, `SCHEMA_VERSION`) and
  `utilities/migrations.py` (`PRAGMA user_version` gate, upgrade steps, pre-migration backup).
  `JobStore.init_db` delegates to them. `utilities/identity.py` owns the (title, company,
  location) identity model; `utilities/mailstore.py` owns pipeline persistence and shares
  `JobStore`'s connection.
- `deploy/` holds the systemd unit, backup script, and backup timer. `cli.py` is the
  UI-free entry point for backfills, stats, and maintenance.
- `tests/` holds the suites. `test_gmail_matching.py`, `test_llm_classification.py`,
  `test_identity.py`, `test_migrations.py`, `test_rough_filter.py`, `test_resolver.py`,
  `test_lifecycle.py`, `test_ingest.py`, and `test_generation.py` are unittest;
  `test_web_pages.py` is pytest with NiceGUI user simulation. `pytest` runs all of them.

## Sync Invariants

Three things in `pipeline/sync.py` and its store methods look interchangeable with a
simpler version and are not:

- **The history cursor may only advance when the whole window was covered.** A capped pass
  holds it. Advancing regardless silently discarded every message past `max_messages` in
  that window - never fetched, and never listable again.
- **`MailboxSync._gone` is what stops a held cursor stalling.** A deleted message can never
  be stored, so it stays "unseen" for ever; without remembering it, a capped batch refills
  with the same dead ids every pass and never reaches the live mail behind them. Cleared
  when the cursor advances, which is also what bounds it.
- **`messages_awaiting_body` keys on `body_fetched_at`, never on `body_text IS NULL`.**
  `prune_bodies` sets `body_text` back to NULL by design, so the simpler predicate hands
  every pruned message back to the fetcher to be downloaded and pruned again, for ever.
  A fetch that fails must therefore either write the timestamp or leave the row genuinely
  retryable - `GmailMessageGone` writes an empty body for exactly this reason, and any
  other error deliberately does not.

## Pipeline Stage Rule

**A failure specific to one item may never end the pass.** `break` in a stage loop is
reserved for conditions that genuinely apply to everything behind it - a rate limit, an
exhausted budget. Anything else is `continue`, because the alternative is a head-of-line
block: the bad item returns to the front of the queue next cycle and stops the same work
again, indefinitely and silently. The router had exactly this, and one email from June left
187 messages unclassified for weeks.

## Concurrency Contract

Load-bearing, and easy to break by accident:

- Database access stays on the thread that opened the sqlite connection - the event loop
  thread. The scheduler is an asyncio task on that same loop, which is why it is safe.
- Only blocking network calls go to `asyncio.to_thread`, and workers receive plain dicts,
  never sqlite `Row` objects.
- A background _thread_ that touches the store would violate this. Do not add one.

## Priority Work

1. Protect and expand job application logging.
2. Fix visible UI encoding issues and review the app in both themes.
3. Add filters and editing to the all-jobs workflow.
4. Build unique dashboard graphs that answer separate tracking questions.
5. Add tests and migration support before larger refactors.
6. Update documentation and project log after each meaningful change.

## Suggested Implementation Approach

- Start with targeted changes in `app.py`.
- Add tests around pure functions and `JobStore` before changing storage behavior.
- Use temporary SQLite databases for tests.
- If the schema changes, add a migration path that preserves existing rows.
- After UI edits, run `python -m py_compile app.py` and manually launch the app when possible.

## Definition of Done

- User data is preserved.
- The requested workflow works from the UI.
- Dashboard charts are distinct and readable.
- Documentation reflects the new state.
- `.agents/PROJECT_LOG.md` has a concise entry for the work completed.

## Git and CI management

- When working on a new feature always create a branch from main to start working on the feature
- Throughout the feature life-cycle, make intermitent commits for each phase of the feature
- If a program fails a CI automaitcally analyse the failure/error message and work on fixing the bug.
