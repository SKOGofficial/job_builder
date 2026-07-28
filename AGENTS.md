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

## Known State

- The app is implemented in one file, `app.py`.
- The SQLite schema is created in `JobStore.init_db`.
- The app already logs core job application fields.
- Existing profile and resume pages are free-text storage pages.
- Dashboard currently has summary cards and one daily applications bar chart.
- UI text currently contains broken encoded characters in a few places and should be cleaned up.

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
