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
- `clients/` holds external client integrations, neither of which imports a UI framework:
  `gmail_client.py` (OAuth, Gmail API calls, and the async `GmailScanner`) and `llm_client.py`
  (Groq config, prompting, pacing, and the async `ClassificationRunner`). Both publish progress
  to subscribers and take an injectable executor for blocking calls.
- The SQLite schema is created in `JobStore.init_db`, including the `email_matches` table.
- `tests/` holds the suites: `test_gmail_matching.py`, `test_llm_classification.py` (both
  unittest), and `test_web_pages.py` (pytest, NiceGUI user simulation). `pytest` runs all three.

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
