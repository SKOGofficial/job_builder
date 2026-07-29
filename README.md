# Job Board Tracker

A local desktop application for tracking job applications across job boards and company portals. The app stores application data in a SQLite database so repeated applications, response status, OA progress, references, payment information, and notes are kept in one place.

## What is implemented

- Desktop UI built with Python `tkinter` and a local SQLite database.
- Job insertion portal centered on the main screen.
- URL-first workflow that generates deterministic Job IDs from the normalized job posting URL.
- Duplicate URL detection before saving.
- Duplicate correlation flow: when the same URL represents a distinct posting, the app allows saving it with a related suffix, such as `AB12CD34EF56-2`.
- Manual fields for:
  - Job posting URL
  - Position title
  - Company
  - Internship/full-time/part-time/contract/unpaid type
  - OA required and OA completed
  - References received
  - Payment amount and period
  - Application status
  - Application and response dates
  - Notes
- All jobs page with a searchable-style table layout and row details.
- Status update flow for applications after they are saved.
- Dashboard with:
  - Number of jobs applied to
  - Number heard back from
  - Offers received
  - Pending applications
  - Cumulative applications line chart with a 7d/14d/30d/90d/all-time range selector
  - Status breakdown pie chart
- Gmail integration that suggests replies matched to open applications, with per-match
  confirm and dismiss. See the Gmail integration section below.
- Dark and light mode with a restrained, color-blind-friendly palette.
- Hamburger menu with:
  - Settings, including Gmail connect and disconnect
  - Email matches
  - Profile
  - Resume & Experiences

## Running the app

The tracker itself uses only Python standard library modules. The optional Gmail integration
needs extra packages, so the project ships a virtual environment setup.

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

The app still launches without those packages installed; the Gmail features simply
report that they are unavailable.

The SQLite database is created automatically as `job_applications.sqlite3` in the project folder.

## Gmail integration

The app can scan Gmail for replies to applications that are still waiting on a response.

Setup:

1. Create a project in the Google Cloud console and enable the Gmail API.
2. Configure the OAuth consent screen and add your own account as a test user.
3. Create credentials of type **OAuth client ID -> Desktop app**.
4. Copy `.env.example` to `.env` and fill in `GMAIL_CLIENT_ID` and `GMAIL_CLIENT_SECRET`.
5. Launch the app, open the menu, then **Settings -> Connect Gmail**. Consent happens in your browser.

How it handles your data:

- The only scope requested is `gmail.readonly`. The app never sends, deletes, or modifies mail.
- Only message headers are fetched (`From`, `Subject`, `Date`). Message bodies are never downloaded.
- Stored per match: the Gmail message ID plus those three headers. No body text is kept.
- The refresh token is the only real credential and is stored in Windows Credential Manager
  through `keyring`, not in the project folder. For a Desktop OAuth client the client ID and
  secret are public per RFC 8252, so they live in `.env` as ordinary configuration.
- **Disconnect** revokes access with Google before deleting the local token, so no live token
  is left behind that the app can no longer see.

How matching works:

- Only jobs in Pending, Applied, or OA Received with no response date are checked.
- A message must either come from a domain containing the company name, or carry the company
  name in its subject. Free mail domains such as gmail.com never count as a domain match.
- Matches are **suggestions only**. The app never changes a job status on its own; every match
  is confirmed or dismissed by you on the **Email matches** page.
- Scanning runs only when you press **Check for replies**. There is no background polling.

## Tests

```powershell
.venv\Scripts\python.exe -m unittest test_gmail_matching
```

The tests use a temporary SQLite database and never touch `job_applications.sqlite3`,
and they require no network access or Google credentials.

## Product report website

A static product report website is available at `website/index.html`. It describes the current tracker,
dashboard direction, future Gmail and Grok API integration concept, and offer comparison roadmap without
implementing those integrations.

## Data model

The `jobs` table stores every application. Each posting URL is normalized and hashed with SHA-256 to create a stable URL-derived Job ID. If the same URL is used again, the app warns the user and can create a correlated Job ID with a numeric suffix.

The `profile` table stores local profile, settings, and resume/experience notes used by the hamburger menu pages.

The `email_matches` table stores suggested Gmail replies linked to a job: the Gmail message ID,
sender, subject, received date, and whether the suggestion was reviewed or dismissed. It is
created additively, so existing databases are preserved.

## Future work

- Web automation integration: use tools such as Beautiful Soup or Selenium to scrape job portals, follow relevant links, and extract information with regex operations for automatic verification of job postings and unique Job IDs.
- Pop-up feature: create a companion web popup app in the same repo that works with the same local computer API. It can quickly store job postings for later review and help autofill resume contents.
- Resume, CV, and Experience: add a resume builder that uses LaTeX to generate tailored resumes from existing resumes and stored experience. It should map experience bullets to the job description and keywords extracted from the posting.
