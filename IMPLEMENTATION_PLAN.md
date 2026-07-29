# Implementation Plan

This file tracks the remaining work for the Job Board Tracker. Keep it current as features are built, verified, or deferred.

## Current State

- Local desktop app in `app.py` using Python standard library `tkinter`.
- Data persistence is local SQLite through `job_applications.sqlite3`.
- `jobs` table logs application URL, deterministic Job ID, position, company, type, OA status, references, pay, status, dates, notes, and timestamps.
- `profile` table stores local profile, settings, and resume or experience text.
- README documents the implemented workflow and high-level future work.
- Current local database has existing user data and is ignored by git.

## Implementation Backlog

### P0 - Preserve and improve application logging

- Add an edit flow for existing job application records, not only status updates.
- Add explicit fields for source board, recruiter contact, portal username, next action, follow-up date, and location or remote status.
- Add audit-style history for status changes, notes updates, and response events.
- Add import and export for applications as CSV or XLSX.
- Add database backup and restore from the UI.
- Add validation around response date so response status and dates remain consistent.

### P0 - UI correctness and polish

- Fix mojibake text currently visible in `app.py` for the hamburger button and detail separators.
- Review dark and light mode contrast for entries, comboboxes, tree rows, dialogs, and disabled text.
- Make the All Jobs table easier to scan with filtering by status, company, type, date, and keyword.
- Add empty states for no jobs, no dashboard data, and no matching filters.
- Improve keyboard navigation and focus order for the add application form.
- Keep controls compact and work-focused; this is an operations tracker, not a marketing page.

### P1 - Unique dashboard graphs

- Replace the single daily bar chart with a richer dashboard that shows genuinely distinct views:
  - Application volume over time.
  - Status funnel or status distribution.
  - Response rate by week.
  - OA required versus completed.
  - Applications by company or source board.
  - Follow-up queue by due date.
- Use different chart types only when they answer different questions.
- Add chart labels, readable axes, and zero-data handling.
- Keep the charts color-blind-friendly and avoid a one-hue dashboard.

### P1 - Automation support

- Add a structured intake queue for copied job URLs that still need review.
- Add optional page scraping for title, company, pay, and location when a posting URL is provided.
- Add duplicate detection that considers normalized URL, company, title, and external job IDs when available.
- Add a browser popup or lightweight companion entry point that writes to the same local API or database safely.

### P1 - Gmail integration (design approved)

Replaces the earlier "design OAuth and privacy boundaries first" placeholder. User approved
networked email access on 2026-07-29, satisfying the AGENTS.md constraint.

Goal: detect when a company replies about an application so the user knows to revisit the portal.

Dependencies and configuration:

- Add `requirements.txt` with `google-api-python-client`, `google-auth`, `google-auth-oauthlib`,
  `python-dotenv`, and `keyring`. The app is no longer standard library only; README must say so.
- OAuth scope is `gmail.readonly`. The app never sends, deletes, or modifies mail.
- Client ID and client secret live in a gitignored `.env`. For a Desktop OAuth client these are
  public per RFC 8252 and are treated as configuration, not secrets. PKCE plus the redirect URI
  restriction is what actually protects the flow.
- The refresh token is the only real credential. Store it in Windows Credential Manager through
  `keyring` under service `job_builder_gmail`, never in the project folder.
- Add `.env` and `client_secret.json` to `.gitignore`. Ship a `.env.example`.

New module `gmail_client.py`, kept out of `app.py`:

- `load_credentials()` reads client config from `.env` and the refresh token from keyring.
- `run_auth_flow()` runs `InstalledAppFlow` on localhost and stores the refresh token in keyring.
- `disconnect()` calls Google's revoke endpoint first, then deletes the local token, so a failed
  revoke never leaves a live token the user can no longer see.
- `search_messages()` and `get_message_headers()` fetch with `format="metadata"` only.

Matching behavior:

- `JobStore.jobs_awaiting_response()` returns jobs in Pending, Applied, or OA Received with no
  response date.
- Build a Gmail query per job from company and application date.
- Match conservatively on sender domain and company name in subject. Company names are short and
  collide with unrelated mail, so body matching would produce false positives.
- Matches are suggestions only. The app never writes a status automatically; the user confirms or
  dismisses each one. An incorrect silent status write is hard to notice and hard to undo.

Storage and UI:

- New `email_matches` table: `id`, `job_id`, `gmail_message_id`, `sender`, `subject`,
  `received_date`, `reviewed`, `dismissed`. Message IDs and headers only, no bodies at rest.
- Created with `CREATE TABLE IF NOT EXISTS`, which is additive and preserves the existing database.
- New "Email matches" drawer page listing suggestions with Confirm and Dismiss actions.
- Settings gains Connect Gmail, connection status, and Disconnect.
- Replies are checked on demand through a "Check for replies" button. No background polling in v1.

Testing:

- Unit test the query builder and matcher against fake headers with no network access.
- Use a temporary SQLite database for `email_matches` tests.

User prerequisite: create a Google Cloud project, enable the Gmail API, configure the OAuth consent
screen, and download Desktop app credentials. Consent happens in the user's browser.

### P2 - Resume and profile workflow

- Expand Profile and Resume & Experiences from free text into structured sections.
- Add reusable experience bullets with tags for skills, role type, impact, and keywords.
- Add job-specific resume tailoring that maps posting keywords to stored experience.
- Generate resume artifacts through a deterministic template before adding AI-assisted wording.
- Keep resume generation separate from application logging so tracker data remains reliable.

### P2 - Engineering quality

- Split `app.py` into store, UI, charts, and domain helpers after tests exist.
- Add unit tests for URL normalization, Job ID creation, duplicate handling, stats, and date behavior.
- Add a lightweight UI smoke test or launch check.
- Add schema migration support before changing existing SQLite tables.
- Add a developer command list for run, test, lint, and backup.

## Acceptance Checklist

- Every job application can be logged with enough detail to revisit the portal later.
- Duplicate and correlated postings are easy to understand from the UI.
- Dashboard graphs answer different questions and do not repeat the same metric in different shapes.
- The UI is readable in dark and light mode with no broken characters.
- Existing `job_applications.sqlite3` data is preserved through migrations.
- README, `CODEX.md`, `AGENTS.md`, and this file stay aligned with the actual app.
