# Job Board Tracker

A local desktop application for tracking job applications across job boards and company portals. The app stores application data in a SQLite database so repeated applications, response status, OA progress, references, payment information, and notes are kept in one place.

## What is implemented

- UI built with [NiceGUI](https://nicegui.io), served locally and opened in a native window,
  backed by a local SQLite database.
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
- All jobs page with a searchable, sortable, paginated table and per-row details.
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

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

That opens a native window. Two flags are available:

- `--browser` opens a normal browser tab instead. This is also the automatic fallback when
  `pywebview` is not installed.
- `--port 8123` moves it off the default 8080.

The server binds to `127.0.0.1`, so it is reachable only from this machine. Gmail and Groq
features report that they are unavailable when their packages or credentials are missing;
everything else keeps working.

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
- Fetching happens in two passes. Headers (`From`, `Subject`, `Date`) are fetched first and are
  the only thing matching looks at. The body is downloaded afterwards, **only for messages that
  already matched**, so mail you will never be shown is never read beyond those three headers.
- Stored per match: the Gmail message ID, those three headers, Gmail's snippet, and the message
  text (plain text where available, otherwise the HTML part with tags stripped, capped at 20,000
  characters). Attachments are never downloaded.
- Everything stays in the local `job_applications.sqlite3` file. Nothing is uploaded anywhere.
- The refresh token is the only real credential and is stored in Windows Credential Manager
  through `keyring`, not in the project folder. For a Desktop OAuth client the client ID and
  secret are public per RFC 8252, so they live in `.env` as ordinary configuration.
- **Disconnect** revokes access with Google before deleting the local token, so no live token
  is left behind that the app can no longer see.

How matching works:

- Only jobs in Pending, Applied, or OA Received with no response date are checked.
- A message must either come from a domain containing the company name, or carry the company
  name in its subject. Free mail domains such as gmail.com never count as a domain match.
- The body is never used to decide a match, only to show you what the email said. Company names
  are short and collide with unrelated mail, so a body-text match would suggest wrong statuses.
- Matches are **suggestions only**. The app never changes a job status on its own; every match
  is confirmed or dismissed by you on the **Email matches** page.
- On that page each match starts collapsed. Click the arrow or the title to expand it and read
  the message. Matches recorded before this feature show a placeholder instead of text; re-scan
  to fetch it.
- Scanning runs only when you press **Check for replies**. There is no background polling.

## AI classification (Groq)

Matched replies can be labelled automatically, so you are not reading every email to
work out whether it was a rejection or an interview invite.

Setup:

1. Create an API key at https://console.groq.com.
2. Copy `.env.example` to `.env` and set `GROQ_API_KEY`.
3. Optionally use **Settings -> Move key to Credential Manager** to get the key out of
   the project folder. The credential store takes precedence over `.env` when both are set.

No extra packages are needed. Groq's endpoint is OpenAI-compatible REST, so it uses the
`requests`, `keyring`, and `python-dotenv` that Gmail support already installs.

What it does:

- Each stored reply is labelled **Rejected**, **Offer**, **Interview**, **OA Received**,
  **Acknowledgement**, or **Unclear**. The last two never change a job.
- A label at or above `GROQ_CONFIDENCE_THRESHOLD` (default 85%) **applies the job status
  automatically**. Below it, the label only pre-fills the dropdown for you to confirm.
- Every automatic change is reversible. The match records the status and response date it
  replaced, and **Undo** on the Email matches page restores both. This matters most for
  Rejected: applying it stamps a response date, which drops the job out of the pool that
  future Gmail scans check.
- A cycle starts automatically after **Check for replies** finds something, and can be run
  on demand from the Email matches page. Only unclassified messages are sent.

Rate limits:

- The free tier allows 30 requests and 12,000 tokens per minute for
  `llama-3.3-70b-versatile`. At roughly 900 tokens per classification the **token** ceiling
  binds first, so requests are paced from tokens rather than sent in a burst. Bodies are
  truncated to 2,000 characters before sending for the same reason.
- Default pace is 12 requests/min, overridable with `GROQ_REQUESTS_PER_MINUTE`.
- If Groq returns **429 Too Many Requests**, the cycle stops cleanly rather than retrying.
  Everything already classified is kept, the page shows how far it got and the suggested
  wait, and a **Resume classification** button restarts from the first unclassified message.

How your data is handled:

- Sent per message: the sender, subject, and the first 2,000 characters of the body.
- The email is untrusted third-party text, and so is the model's reply. The model may only
  return one of the six labels above; anything else becomes Unclear. An email that tries to
  instruct the classifier is labelled Unclear rather than obeyed.
- The Groq key is a real credential, unlike the Gmail Desktop client ID and secret which are
  public per RFC 8252, so it is read from the OS credential store first.
- On a machine with no usable credential store — a bare Linux install, a CI runner, a headless
  server — that read reports nothing and `.env` is used instead. Storing a secret still fails
  loudly there, since silently not saving a credential is worse than an error.

## Tests

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
```

Or one module at a time:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_web_pages.py
```

- `tests/test_gmail_matching.py` covers query building, company matching, body extraction,
  the email match store, and the scan cycle.
- `tests/test_llm_classification.py` covers Groq configuration, prompting, response
  validation, pacing, and the classification cycle.
- `tests/test_web_pages.py` opens every route and clicks through the app using NiceGUI's
  user simulation. No browser and no display are needed, so this runs on a bare CI runner.

The backend tests are `unittest.TestCase` classes and the page tests are pytest-style;
`pytest` collects both, which is why it is the single command.

The tests use a temporary SQLite database and never touch `job_applications.sqlite3`. They
need no network access, no Google credentials, and no Groq key: the HTTP calls, the pacer's
clock, the model client, and the async executor are all injected.

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
