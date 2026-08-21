# Project Log

Use this file to record meaningful project changes, implementation decisions, and verification notes.

## 2026-08-19 - Groq 413s, and the head-of-line block behind them

Reported as `HTTP 413 Request Entity Too Large` from Groq on two stages. The prompts were
not the problem and were not touched: the router already caps a body at 2,000 characters
and alert extraction at 6,000, and the two requests that failed were 3.7 KB and 7.3 KB -
roughly 1,000 and 2,000 tokens.

Reproduced against the API with the exact stored payloads. `groq/compound` returns 413 for
the 7.3 KB request while accepting a 10-character one, and `openai/gpt-oss-20b` and
`openai/gpt-oss-120b` both serve the identical payload in about a second, parse cleanly
through this project's own `parse_route` and `parse_extraction`, and return the correct
postings. `groq/compound` is an agentic system routed over `openai/gpt-oss-120b` - its
advertised 131k context does not describe what it will accept in one request. The model in
`.env` is a configuration matter and was left to the user.

The useful finding was what the 413 exposed in the pipeline. Two things, both worse than
the error itself:

- **The router stopped the whole pass on any unexpected error.** `except Exception: break`.
  So one message whose payload no provider would take sat at the head of the queue and
  every message behind it stayed unclassified - measured at the time: 188 messages backed
  up behind a single email from 2 June. Now `continue`, with a count logged; a rate limit
  is still `break`, because that one really does apply to everything behind it.
- **A 413 did not fail over.** `ProviderPool.call` fails over on a rate limit, an exhausted
  budget and a misconfiguration, but a 413 arrived as a bare `RuntimeError` and stopped the
  call dead - with Gemini sitting behind it in the chain, with room to spare. Now raised as
  `ProviderRequestTooLarge` and treated as grounds to try the next provider. It
  deliberately does **not** cool the provider down: the payload was too big, the provider
  is fine, and the next message may be a tenth the size.

Measured while diagnosing, and worth recording because it decides whether the prompt caps
need to change - they do not:

- Every task's worst case fits under the 8,000 tokens-per-minute ceiling these models
  report. Alert extraction is the largest at roughly 1,800 prompt tokens plus 1,500
  completion, about 3,300 of 8,000.
- Both `gpt-oss` models returned `completion_tokens` exactly equal to `max_tokens` on the
  extraction call. They reason before answering, so the reply fit with nothing to spare -
  the same trap `draft_referral` hit on Gemini. A digest carrying many more postings could
  truncate. Left alone, since raising it is a judgement about spend.
- `TOKENS_PER_MINUTE` in `providers/base.py` defaults to 12,000 while these models report
  8,000, so the pacer will overshoot until `GROQ_TOKENS_PER_MINUTE` is set.

Verification:

- 689 tests pass, up from 682. Five new in `test_provider_pool.py` covering the failover,
  the absence of a cooldown, the client surviving, and the exception not being a rate
  limit; two new in `test_ingest.py` covering the router skipping a bad message and still
  stopping on a rate limit.
- Each was run against the unfixed code and confirmed to fail there. The first attempt at
  that check was itself wrong - the revert matched an earlier `ProviderNotConfigured` and
  duplicated code instead of removing the handler, so the tests passed and appeared to
  prove nothing was being tested. Redone precisely, three of the five fail as they should.

## 2026-08-19 - Gmail 404s, and two quieter bugs behind them

Started from repeated warnings in the log: `HttpError 404` fetching headers for two message
ids, each with a full traceback. Confirmed against the real mailbox first - both ids return
404 from `users.messages.get`, and neither is trashed, since a trashed message still
fetches. They were permanently deleted.

Cause: `list_history` asked for `historyTypes=["messageAdded"]` alone. Gmail filters those
records server-side, so a message that arrived and was deleted again inside one history
window was reported as added with the deletion invisible. It now asks for `messageDeleted`
too and applies both in the order the records arrive. That narrows the race without closing
it - a message can still be deleted after the last page is generated - so the remainder is
named: `GmailMessageGone`, following `GmailHistoryExpired`'s precedent that a routine
outcome deserves a class rather than an HTTP status parsed at the call site.

Impact of the original 404s was log noise only. The cursor advances past them either way,
which was checked rather than assumed. But reading that path turned up two things that were
not noise:

- **The body fetcher could loop for ever.** It skipped a failed fetch and left `body_text`
  NULL, and `messages_awaiting_body` selected on that column alone, so a mirrored message
  that was later deleted would return on every cycle, spending an API call each time to be
  told again that it is gone. A confirmed deletion now stores an empty body.
- **`messages_awaiting_body` contradicted two docstrings.** Both `store_body` and
  `prune_bodies` claim the queue is governed by `body_fetched_at`; the query used
  `body_text IS NULL`. Since `prune_bodies` deliberately nulls `body_text` on old
  irrelevant mail, every pruned message was eligible for re-fetch - downloaded again,
  pruned again, indefinitely, undoing the retention pass. The predicate now matches what
  the docstrings always said.

Separately, **the incremental path could lose mail.** `_store_headers` capped its batch at
`max_messages` but `_incremental` advanced the cursor regardless, so anything past the cap
in one window was never fetched and could never be listed again. The cursor is now held on
a capped pass. The obvious version of that fix stalls - a deleted id is never stored, stays
unseen for ever, and would refill every subsequent batch - so confirmed deletions are
remembered in `MailboxSync._gone` and the set is cleared when the cursor advances, which
also bounds it.

Measured before changing anything:

- 580 messages had passed the filter with no body. All 580 had `body_fetched_at` NULL and
  no category, so they were a genuine unfetched backlog rather than a stuck loop - an
  earlier guess that `prune_bodies` was feeding them back was wrong, and the data said so.
  It drains at 60 a cycle; it was 520 an hour later.
- `body_text` and `body_fetched_at` were perfectly consistent across all 1,696 rows - no
  row set one without the other - so switching the queue predicate changed nothing about
  today's behaviour, only tomorrow's.

Verification:

- 682 tests pass, up from 669. Twelve new ones in `tests/test_ingest.py` cover deletion
  subtraction in `list_history` (same page, later page, duplicates, a deletion for
  something never added), the deleted-message paths on both fetchers, the held cursor and
  its resumption, the stall the naive fix would have introduced, and a pruned body staying
  out of the queue.
- Each new test was checked against the unfixed code and confirmed to fail there, so none
  of them is passing vacuously.
- `list_history` was called against the real mailbox with the new `historyTypes` to confirm
  the API accepts it. Nothing had changed since the stored cursor, so it returned zero ids.

## 2026-08-19 - Referral contacts, and the morning list of who to ask

The tracker knew about a thousand postings and nothing about the five people who could
get an application read. This adds the missing side: who you know, where they work, and
what their employers are advertising right now.

Two new tables, `contacts` and `referral_outreach`. Neither needed a migration - whole
tables are created by `create_tables` on every `initialise`, before the version gate - so
`SCHEMA_VERSION` stays at 6. `company_slug` is stored on the contact rather than derived
per query, because it is the join key: a lead says "Capital One" and the user types
"Capital One, Inc.".

Cost is the axis the design is built on:

- **Matching is free** and runs on every page render. One `list_leads` call, bucketed by
  slug in Python, joined to contacts. Read-time rather than stamped onto a lead at
  creation, because the first thing anyone does here is add five contacts and expect to
  see the postings already in the list.
- **`Check now` spends a grounded search**, for one company, only when pressed. New
  `check_openings` task on the same chain as `research`, with `find_openings` added to
  both research clients (their `_call` takes an optional system prompt now; the injected
  `caller` still takes the prompt alone, so no test double changed). What it finds becomes
  an ordinary lead tagged `board=careers-check`, inheriting scoring, research and document
  generation. Openings with no URL are dropped by the parser: these come from a model
  reading the web rather than from mail the user received, and a role that cannot be
  verified is not one to spend a real favour on.
- **Drafting** follows `cover_letter.py`'s rule - `score_bullet` picks the supporting
  evidence before the model sees anything. The stakes are higher here than for a covering
  letter: an invented project lands in front of someone who knows the applicant.

Nothing is sent by the app. Gmail stays `gmail.readonly`; a draft gets a copy button and a
`mailto:` link.

Three bugs found while verifying, all real:

- **The badge could never clear for an undated lead.** `is_new_for` keyed only on
  `posted_ts`, which is absent on rows written before it existed. Now falls back to
  `created_at`, the same fallback `purge_stale_leads` uses - undated rows are immortal
  there and were permanently new here.
- **A task's token budget has to cover the model's thinking.** `draft_referral` at 700
  tokens returned JSON truncated after the subject line: `gemini-3.6-flash` reasons before
  answering and `maxOutputTokens` caps both together. Raised to 2000 and documented in
  `routing.py` and `AGENTS.md`. **`write_cover_letter` at 1200 may have the same problem
  and was left alone** - it is existing behaviour and changing it is a separate decision.
- **The page tests would have made live billed calls.** Pressing `Check now` under test
  reached the real pool, and the developer machine has real keys in `.env`. The referral
  page tests now install a stub pool on `state._pool`.

Verification:

- 669 tests pass, up from 623. New `tests/test_referrals.py` (39) covers contact storage,
  slug matching across differently-written company names, the new/checked boundary
  including both date fallbacks, openings parsing (fenced, prose-wrapped, unlinked,
  garbage), the checker's dedupe and already-applied guards, and the draft prompt and
  parser. Seven page tests in `test_web_pages.py` cover the route end to end.
- Exercised by hand against a `VACUUM INTO` copy of the real database, never the database
  itself. A contact entered as "Capital One, Inc." matched all 11 open Capital One leads;
  `Mark checked` cleared the count and the drawer badge; a real draft came back complete
  after the budget fix, citing only stored bullets and inventing no relationship for a
  contact with no notes. Checked in both themes.

Noted while verifying, not changed:

- **`GROQ_MODEL=llama-3.3-70b-versatile` in `.env` is decommissioned** - Groq returns HTTP
  404 for every request. Groq is the primary for `route_email`, `extract_alert`,
  `extract_update`, `extract_acknowledgement`, `score_relevance` and `classify_reply`, so
  all of those are currently falling through to Gemini.
- **An HTTP 503 does not fail over.** `ProviderPool.call` fails over on rate limits,
  budget exhaustion and misconfiguration, but a 503 raises `RuntimeError` and stops there.
  Gemini returned 503 repeatedly during verification and the Groq fallback was never
  tried. Changing this alters failover semantics for every task, so it is left as a
  finding.
## 2026-08-20 - A failed model call is not an attempt

A real data loss, caused by the `handled_at` marker added three days earlier
meeting a provider outage.

`GROQ_MODEL` in `.env` named `llama-3.3-70b-versatile`, which Groq had
decommissioned, so every extraction call returned HTTP 404. That arrived as a
bare `RuntimeError`; `parse_alert` caught it under `except Exception` and
returned no postings; `AlertHandler` read "no postings" as "this digest
contains nothing" and stamped `handled_at`, which permanently removes a message
from the backlog. **70 real job alerts were retired that way** - Disney, Best
Buy, Cherokee Federal, Etched, Cintas - each carrying postings that were never
turned into leads.

The guard written with `handled_at` covered `client is None`. It did not cover
a client that exists and cannot serve, which is the more common failure.

What was wrong, and what replaces it:

- **A bare exception cannot be reasoned about.** `ProviderUnavailable` now names
  "the provider could not serve this request", carrying the HTTP status. Groq
  and Gemini both raise it instead of `RuntimeError`.
- **`parse_alert` swallowed it.** It already re-raised `GroqRateLimited` with a
  comment saying a rate limit is not a parse failure. A provider failure is not
  one either, and now both propagate. A genuine parse bug is still swallowed -
  that one is specific to a message, and blocking the queue on it would be the
  worse failure.
- **Handlers now stop the pass on it**, without marking anything. All three -
  alerts, acknowledgements, updates - treat it the way they treat a rate limit.
- **The pool now fails over on it.** It failed over for rate limits, spend
  ceilings and misconfiguration, but a 404 stopped the whole call with Gemini
  sitting idle behind it in the chain. A dead model name took the pipeline down
  while a working provider went unasked. Cooled down for 5 minutes as well as
  skipped, since the common causes are persistent and rediscovering them once
  per message costs a round trip each time. Not the daily window a spend
  ceiling earns - a 5xx may well be over in a second.

The rule, stated once: **a call that never reached a model is not an attempt,
and nothing may be recorded as tried.**

Repair: `cli.py requeue` clears `handled_at` on messages marked handled that
produced no link. Dry by default. It is deliberately blunt - it puts back
genuinely empty digests along with the damaged ones, costing one wasted
extraction each. That asymmetry is the right way round: a wasted call is a
fraction of a cent, a lost alert is a job never seen. On this database all 70
affected rows classify as real alerts and none as board marketing, so the
bluntness cost nothing.

Verification:

- 667 tests pass, up from 660. New suite
  `tests/test_provider_failure_is_not_an_attempt.py`; its four load-bearing
  tests were run against the unfixed code and confirmed to fail there.
- The suite pins both directions, because the fix sits next to the retry leak
  it must not resurrect: an honestly empty digest is still marked handled and
  still not re-extracted on the next cycle.
- Three existing tests asserted the bare `RuntimeError` and were updated to the
  named exception; they now also assert the status is carried.

Noted, not acted on:

- `.env` has since been moved to `openai/gpt-oss-20b`, so the outage itself is
  over. The failover change is what stops the next one costing anything.
- `/settings` still returns 500 when the database names a provider the running
  build does not know about. Unrelated, still outstanding.

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
