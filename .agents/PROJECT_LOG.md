# Project Log

Use this file to record meaningful project changes, implementation decisions, and verification notes.

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
