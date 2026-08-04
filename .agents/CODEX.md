# Codex Project Notes

Use this file as the operating guide for future Codex sessions in this repository.

## Project Summary

Job Board Tracker is a local desktop application for logging job applications and tracking follow-up state. It is built with NiceGUI and SQLite, served on loopback and opened in a native window.

Primary goal: make it easy to record every job application, detect duplicate postings, track responses and OA progress, and review progress through useful dashboard views.

## Current Files

- `app.py`: entry point. Parses arguments and calls `ui.run`; the UI itself lives in `web/`.
- `README.md`: user-facing project overview and run instructions.
- `job_applications.sqlite3`: local user data, ignored by git. Do not delete, reset, overwrite, or commit it.
- `.agents/IMPLEMENTATION_PLAN.md`: feature backlog and acceptance checklist.
- `.agents/PROJECT_LOG.md`: project activity log.
- `.agents/AGENTS.md`: agent handoff notes and constraints.

## Current Behavior

- Run with `python app.py`.
- Creates or opens `job_applications.sqlite3` in the project folder.
- Generates stable 12-character Job IDs from normalized posting URLs.
- Allows correlated duplicate Job IDs with numeric suffixes for repeated URLs that represent distinct postings.
- Supports application entry, all-jobs listing, status updates, profile text storage, resume text storage, dark/light theme, and a basic daily applications chart.

## Working Rules

- Preserve user data in `job_applications.sqlite3`.
- Treat database schema changes as migrations. Do not assume a fresh database.
- Use Python standard library unless a new dependency is justified and documented.
- Prefer small, verifiable changes over broad rewrites.
- Keep UI dense, readable, and task-oriented.
- Before changing UI text, check for broken encoding characters around the menu icon and detail separators.
- Update `.agents/IMPLEMENTATION_PLAN.md` and `.agents/PROJECT_LOG.md` when completing meaningful work.
- For every new function or method, include a docstring or structured comment with a short `Summary` describing what the method does, plus `Parameters`, `Returns`, and `Raises`. Omit any of those three that would be empty rather than writing "None." - only `Summary` is unconditional. Keep any existing prose that explains *why*, and add the fields beneath it. See `.agents/AGENTS.md` for the full rule and `utilities/store.py` for a worked example.

## Git and CI Management

All agents must follow these practices (defined in `.agents/AGENTS.md`):

- Create feature branches from main for new work—never commit directly to main.
- Make intermittent commits for each phase of feature development, not just at the end.
- When a CI build fails, automatically analyze the error message and fix the root cause before re-pushing.
- Write clear, descriptive commit messages that explain the "why" behind changes.

## Verification

Use these checks when relevant:

```powershell
python -m py_compile app.py
python app.py
```

For database work, test against a copy of `job_applications.sqlite3` or create a temporary database path through `JobStore`.

## Relevant Skills

- Python standard library development.
- Tkinter and ttk UI layout.
- SQLite schema design and migration.
- Desktop UI QA for dark and light themes.
- Data visualization design for operational dashboards.
- Local data privacy and backup handling.
- Optional future work: web scraping, browser automation, OAuth/email integration, resume generation, and spreadsheet export.
