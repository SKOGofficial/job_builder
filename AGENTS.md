# Agent Handoff Notes

This repository is a local job application tracker. Future agents should start here, then read `README.md`, `IMPLEMENTATION_PLAN.md`, and `CODEX.md`.

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
  the UI shell.
- When adding a page, create the module under `pages/` and register the class in
  `pages/__init__.py`. Navigation is built from that registry, so nothing else needs editing.

## Known State

- `app.py` is the shell and entry point: window, theme, navigation, drawer, and the `card`
  and `clear` helpers pages draw with.
- `store.py` holds `JobStore` plus `normalize_url`, `url_hash`, and `today_iso`. It has no
  Tkinter import so it can be tested headlessly. `JobStore` resolves `DB_PATH` at call time,
  not as a default argument, so tests can redirect it.
- `theme.py` holds the palettes, `STATUS_COLORS`, `TIME_RANGES`, the option lists, and
  `apply_styles`.
- `pages/` holds one module per page, all subclassing `BasePage` in `pages/base.py`. Profile
  and Resume share `pages/text_storage.py`. Page instances are created once and reused, so
  per-page state such as the dashboard range survives navigation.
- `gmail_client.py` owns OAuth and the Gmail API. `gmail_workflow.py` sits between it and the
  UI, deciding which jobs to scan and reporting results.
- `llm_client.py` is an intentionally empty placeholder for LLM orchestration.
- The SQLite schema is created in `JobStore.init_db`, including the `email_matches` table.
- Tests are `test_gmail_matching.py` and `test_app_pages.py`. The latter needs a display.

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
- `PROJECT_LOG.md` has a concise entry for the work completed.
