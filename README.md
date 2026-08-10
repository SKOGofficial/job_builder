# Job Board Tracker

An application for tracking job applications across job boards and company portals. It stores everything in a local SQLite database, and runs either as a desktop app or as a service on a home server that watches your inbox and keeps the tracker up to date on its own.

Two lists sit at the centre of it:

- **Applications** - roles you have applied to, each showing every email about that
  role in date order: the acknowledgement, the assessment invite, the rejection.
- **To apply** - roles picked up from job-board alert emails that you have not applied
  to yet, each with a tailored resume and CV already generated from your stored
  experience, plus a working link to the application portal.

Acknowledgement emails ("thanks for applying") move a role from the second list to the
first on their own.

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
- Background ingest pipeline that polls Gmail, classifies job mail, links each email to
  the role it concerns, turns job-board alerts into leads, and applies status changes.
  See "The ingest pipeline" below.
- Company research and tailored resume/CV generation for leads, using Gemini with Google
  Search grounding and falling back to Claude. See "Research and resume generation" below.
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

That opens a native window. The flags:

- `--browser` opens a normal browser tab instead. This is also the automatic fallback when
  `pywebview` is not installed.
- `--headless` serves without a window or a browser, for running as a service.
- `--no-poll` starts without the background Gmail poller.
- `--port 8123` moves it off the default 8080.
- `--host` changes the bind address. Read the warning under "Running as a service" first.

The server binds to `127.0.0.1`, so it is reachable only from this machine. Gmail, Groq,
and Anthropic features report that they are unavailable when their packages or credentials
are missing; everything else keeps working.

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
- Fetching happens in two passes. Headers are fetched first; the body is downloaded afterwards,
  and only for messages that got past the first pass. Attachments are never downloaded.
- **The ingest pipeline widens this.** The per-job scanner described here only ever read
  bodies for mail that already matched an application. The pipeline stores headers for your
  whole mailbox and downloads bodies for anything its rough filter cannot rule out - which is
  a large share of it. That is the trade that makes it possible to catch a recruiter email
  matching no keyword and no known company. See "The ingest pipeline" for what is kept and
  for how to prune it.
- Stored per match: the Gmail message ID, headers, Gmail's snippet, and the message text
  (plain text where available, otherwise the HTML part with tags stripped, capped at 20,000
  characters).
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

## AI classification (Groq, Gemini)

Matched replies and mirrored mail are labelled automatically, so you are not reading every
email to work out whether it was a rejection or an interview invite.

Two providers do this work, and a third does research. Which one runs for a given job is
routing, editable in **Settings -> Task routing**.

Setup:

1. Create an API key at https://console.groq.com, and optionally one at
   https://aistudio.google.com/apikey.
2. Copy `.env.example` to `.env` and set `GROQ_API_KEY`, `GEMINI_API_KEY`, or both.
3. Optionally use **Settings -> Move key to credential manager** to get a key out of the
   project folder. The credential store takes precedence over `.env` when both are set.

No extra packages are needed. Both endpoints are plain REST, so they use the `requests`,
`keyring`, and `python-dotenv` that Gmail support already installs.

What it does:

- Each stored reply is labelled **Rejected**, **Offer**, **Interview**, **OA Received**,
  **Acknowledgement**, or **Unclear**. The last two never change a job.
- A label at or above `LLM_CONFIDENCE_THRESHOLD` (default 85%) **applies the job status
  automatically**. Below it, the label only pre-fills the dropdown for you to confirm. The
  threshold is a property of the classification rather than of the provider, so every model
  is held to the same bar. `GROQ_CONFIDENCE_THRESHOLD` is still read, for existing setups.
- Which model produced a label is recorded and shown on the Email matches page, so a bad
  label can be traced to the model that produced it.
- Every automatic change is reversible. The match records the status and response date it
  replaced, and **Undo** on the Email matches page restores both. This matters most for
  Rejected: applying it stamps a response date, which drops the job out of the pool that
  future Gmail scans check.
- A cycle starts automatically after **Check for replies** finds something, and can be run
  on demand from the Email matches page. Only unclassified messages are sent.

### Routing and failover

Every model call in the app is a named task with an ordered list of providers. The first
that is configured, is not cooling down from a rate limit, and has daily allowance left
takes the call. Otherwise the next one does.

| Task | Default order |
|---|---|
| Route incoming email | Groq, then Gemini |
| Extract job alerts | Groq, then Gemini |
| Read application updates | Groq, then Gemini |
| Read acknowledgements | Groq, then Gemini |
| Score lead relevance | Groq, then Gemini |
| Classify matched replies | Groq, then Gemini |
| Research a company and role | Gemini, then Claude |

A fourth provider, **Claude Code**, is available for every task but is in no default order —
see [below](#using-the-claude-code-cli-as-a-provider).

Edit these in **Settings -> Task routing**, or set `LLM_ROUTE_<TASK>` in `.env`. A task with
no saved choice follows `.env`, so changing that file keeps working; **Reset** on a row
deletes the saved choice rather than freezing today's default into the database.

A provider is only handed work it can actually take. If none of a task's providers has
headroom, the call falls to `LLM_DEFAULT_PROVIDER` (Groq, because a per-minute limit always
clears while a daily one does not) and waits — but only briefly, and never long enough to
freeze the interface. Past that the stage stops cleanly with everything it has already
written intact, exactly as it did before, and the next cycle resumes.

Rate limits:

- **Groq**: the free tier allows 30 requests and 12,000 tokens per minute for
  `llama-3.3-70b-versatile`. At roughly 900 tokens per classification the **token** ceiling
  binds first, so requests are paced from tokens rather than sent in a burst. Bodies are
  truncated to 2,000 characters before sending for the same reason.
- **Gemini**: the request ceiling binds first, and unlike Groq there is also a **daily**
  cap. That is the limit a second engine actually reaches, so requests spread out as the
  day's allowance is spent rather than running flat out until they stop — bursts pass
  freely through the first half, then pace. Settings shows how much is left.
- Daily usage is recorded in the database, so restarting the app cannot un-spend it.
- Free-tier limits are per project and move. Google publishes yours in AI Studio rather
  than in the docs; `GEMINI_REQUESTS_PER_MINUTE`, `GEMINI_TOKENS_PER_MINUTE` and
  `GEMINI_REQUESTS_PER_DAY` start conservative and are meant to be raised.

### Using the Claude Code CLI as a provider

If the `claude` CLI is installed and signed in, it can serve any task. It is not an HTTP
client: it runs `claude -p` as a subprocess with the prompt on stdin and reads a JSON
envelope back. Configuration is discovery rather than secrets — install it, run `claude`
once, and Settings shows it as available.

Nothing routes to it by default, because one call is an agent loop taking tens of seconds
where Groq answers in under a second. Pointing `Route incoming email` at it turns a
twenty-message batch into twenty agent invocations.

**Classification works; grounded research depends on your account.** Classification is
verified end to end. Research asks the CLI for `WebSearch` and `WebFetch`, and some
accounts refuse both in headless mode regardless of the permissions passed. When that
happens the model correctly declines to invent facts and returns empty fields, so the
provider treats an all-empty result as a failure and hands the lead to the next provider in
the chain rather than caching emptiness against it. You will see one warning naming the
refused tools. Try it before routing research here permanently.

Two things are deliberate and worth knowing before you turn it on:

- **It runs in an empty directory, not in this project.** Without `--bare` the CLI loads
  `CLAUDE.md`, hooks and MCP configuration from wherever it starts. Started here, every
  classification would quietly inherit this repo's `.claude/CLAUDE.md` as context.
  `CLAUDE_CLI_WORKDIR` moves it; the default is `~/.job_builder/claude_cli`.
- **It gets no tools except web access, and only for research.** The CLI is an agent with
  Bash, Read and Edit available, and the text being classified is untrusted email. The
  permission mode denies everything not explicitly allowed, so the list is an allowlist
  rather than a denylist and future tools are closed by default. This is not theoretical:
  during testing the model reached for a personal Indeed connector on the account —
  including its "get my resume" tool — from a job-research prompt. Every one was refused.

On authentication: as configured it uses your signed-in subscription. Anthropic's
[legal and compliance documentation](https://code.claude.com/docs/en/legal-and-compliance)
asks developers building on the Agent SDK to use API-key authentication, and states that
Pro and Max limits assume ordinary individual use. Setting `CLAUDE_CLI_BARE=1` with
`ANTHROPIC_API_KEY` runs it that way instead.

How your data is handled:

- Sent per message: the sender, subject, and the first 2,000 characters of the body.
- The email is untrusted third-party text, and so is the model's reply. The model may only
  return one of the six labels above; anything else becomes Unclear. An email that tries to
  instruct the classifier is labelled Unclear rather than obeyed. Both providers use the
  same prompt and the same validation, so adding one does not weaken this.
- Provider keys are real credentials, unlike the Gmail Desktop client ID and secret which
  are public per RFC 8252, so they are read from the OS credential store first. They travel
  in request headers, never in a URL, because URLs end up in logs and tracebacks.
- On a machine with no usable credential store — a bare Linux install, a CI runner, a headless
  server — that read reports nothing and `.env` is used instead. Storing a secret still fails
  loudly there, since silently not saving a credential is worse than an error.

## The ingest pipeline

The per-job Gmail scanner above asks "has anyone replied about this application?".
The pipeline works the other way round: it pulls the mailbox in, decides what each
message is, and lets that decide which job it touches or creates.

Stages, one module each under `pipeline/`:

1. **Sync** - `users.history.list` for the incremental path, falling back to a bounded
   `messages.list` when the stored history cursor has expired (Gmail keeps roughly a
   week). Headers only.
2. **Rough filter** - drops mail that is confidently not from a job board or a company:
   personal mail with no job wording, Social and Forums, and a denylist you build by
   pressing "not job related". Everything else survives. It is deliberately permissive -
   a filter tuned for precision silently loses the recruiter email that matches no
   keyword, and you never learn what you missed. Every verdict is recorded, so "why
   didn't I see that email" is answerable.
3. **Bodies** - downloaded only for what got through.
4. **Classify** - whichever model is routed to the job labels each message `job_alert`,
   `job_update`, `job_acknowledgement`, or `irrelevant`. Most mail is irrelevant and the
   prompt says so. The model that produced each label is recorded alongside it.
5. **Resolve and link** - work out which role a message concerns, strongest signal first:
   the board's own job ID, then an exact identity match, then sender domain plus title
   similarity. **When several roles remain plausible it links nothing** and the message
   goes to a review queue. Guessing would attach a rejection to the wrong role.
6. **Handle** - alerts become leads, acknowledgements promote leads into applications,
   updates apply a status change when confident enough.

Links point at a role's *identity*, not at a table row, so a lead that becomes an
application keeps every email already attached to it. The alert that first surfaced a
role still appears on its timeline next to the rejection that closed it.

### Command line

Useful without the UI, and safe to run while the app is up (the database is in WAL mode):

```powershell
.venv\Scripts\python.exe cli.py status
.venv\Scripts\python.exe cli.py backfill --days 365 --max 2000
.venv\Scripts\python.exe cli.py sync --once
.venv\Scripts\python.exe cli.py filter-stats --samples 20
.venv\Scripts\python.exe cli.py prune --days 30
.venv\Scripts\python.exe cli.py deny newsletters.example.com
```

`filter-stats` is the one to watch early on. If the denylist count is not growing, the
"not job related" control is not being used and the model is being paid to reject the
same newsletters every day.

### Storage and pruning

Headers are kept for everything, bodies for everything the filter passed. `cli.py prune`
drops bodies of mail classified irrelevant and older than N days, keeping the ID, headers,
and classification so nothing is re-fetched or re-classified. Linked messages are never
pruned - they are the timeline you actually read. The scheduler runs the same pass about
daily and vacuums afterwards.

## Running as a service

> **This app has no authentication.** Whoever can reach the port can read every stored
> email body and your whole application history. Keep it on `127.0.0.1` and reach it over
> an SSH tunnel (`ssh -L 8080:localhost:8080 <server>`), Tailscale, or WireGuard. Only use
> `--host 0.0.0.0` behind a reverse proxy that authenticates, and treat adding real login
> as a prerequisite rather than a nice-to-have.

`deploy/` has a systemd unit, a backup script, and a backup timer:

```bash
sudo cp deploy/job-builder.service /etc/systemd/system/
sudo cp deploy/job-builder-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now job-builder job-builder-backup.timer
journalctl -u job-builder -f
```

Two things differ on a headless box:

- **Secrets.** There is usually no secret service, so `keyring` cannot store the Gmail
  refresh token. Set `JOB_BUILDER_SECRETS=file` and the app writes a 0600 JSON file
  instead (`JOB_BUILDER_SECRETS_PATH`, default `~/.config/job_builder/credentials.json`).
  Opt-in on purpose - nobody should end up with a refresh token on disk without choosing it.
- **OAuth consent.** There is no browser to open. The redirect port is pinned (default
  8765) so it can be forwarded: run `ssh -L 8765:localhost:8765 <server>`, connect Gmail
  from Settings or the CLI, and open the printed URL on your desktop.

Backups use `VACUUM INTO` rather than `cp`. A plain copy of a live SQLite file can catch it
mid-write, and in WAL mode also misses everything still in the `-wal` file. The script
runs an integrity check on each backup before pruning old ones.

## Research and resume generation

Leads on the to-apply list arrive with a resume and CV already built, so the flow is
open the list, click through, fill in the form, send.

How it stays affordable:

- **Groq and Gemini** do the high-volume work - classifying every message, and scoring each
  new lead against your profile. Free tiers, paced, and one covers for the other.
- **Gemini** researches first, using Google Search grounding. Free, and only leads that
  clear the relevance score reach it.
- **Claude** (`claude-opus-5`, with server-side web search) picks up research Gemini cannot
  take - no key, daily allowance spent, or rate limited.

Without the gate, a daily digest of five to ten postings is $45-150 a month, most of it
spent on roles dismissed at a glance. With it, spend follows the roles worth pursuing, and
with Gemini in front most of it never reaches a paid model at all.
`RESEARCH_DAILY_OUTPUT_TOKENS` is a hard daily ceiling on Claude on top of that - a backstop
against a parser bug turning one email into hundreds of leads, not a budget you should hit.
When it is reached, research falls back to Gemini rather than stopping. A lead scored too
low can still be prepared by hand from the UI or with `cli.py prepare --lead <id>`.

One API constraint worth knowing, because it shapes the code: Gemini rejects a request that
asks for Google Search grounding *and* a JSON response type together. Research needs the
search, so its reply arrives as ordinary text and the parser digs the JSON out of it -
tolerantly, and with the same validation and length clamps applied afterwards either way.

Generation is two separate steps. Bullet selection ranks your stored experiences against
the posting's keywords - deterministic, no model. Rendering fills a template. Markdown and
HTML always work; a PDF is produced as well if the `typst` binary is on PATH. Typst rather
than LaTeX because it is a single static binary instead of a multi-gigabyte install.

This needs structured experience bullets rather than the free-text resume field - a resume
cannot be tailored from a prose blob. Add them on the Resume page.

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
- `tests/test_identity.py` covers the normalization that decides whether two spellings of
  a job are the same job. Organised as pairs that must collapse and pairs that must not.
- `tests/test_migrations.py` builds a pre-migration database, migrates it, and asserts
  every row survived. The one that protects real user data.
- `tests/test_rough_filter.py` covers each drop rule, and the cases that must **not** be
  dropped - a recruiter mailing from Gmail, a job alert filed under Promotions.
- `tests/test_resolver.py` covers each resolution tier, including the ambiguous case that
  must refuse to guess.
- `tests/test_lifecycle.py` runs alert to lead to application end to end and asserts the
  alert email is still on the job's timeline afterwards.
- `tests/test_ingest.py` covers incremental sync, the expired-cursor fallback, router
  validation, and board parsing.
- `tests/test_generation.py` covers research parsing, the spend ceiling, relevance
  scoring, bullet selection, and rendering.

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

### How a job is identified

A posting URL is a poor identity: LinkedIn, Indeed, and a company's own portal each hand
out a different URL for the same role, and alert emails wrap those in tracking links that
change per send. So the identity is `identity_key` - a hash of the normalized
(title, company, location) triple.

Normalization collapses spelling and nothing else. "Sr." becomes "senior" and "Google LLC"
becomes "Google", but "Engineer II" keeps its level and a senior role never merges with a
non-senior one. Under-merging leaves two rows, which is untidy; over-merging destroys one
role's history invisibly, so the rules lean the safe way.

`job_id` remains the stable handle other tables reference, and stays URL-derived on rows
created before the rework. `job_sources` records every URL a role has been seen at -
one job, many boards - along with each board's own job ID, which is a far better dedupe
key than the tracking wrapper around it.

### Tables

- `jobs` - applications. Gains `location`, `identity_key`, and nullable `posting_url`
  (a job created from an acknowledgement may never have had a URL).
- `job_sources` - the many-boards-one-role mapping.
- `messages` - the mailbox mirror: headers, optional body, filter verdict, category.
- `message_links` - which roles a message concerns, keyed on `identity_key`, many-to-many
  because one alert digest carries several postings.
- `job_leads` - the to-apply list, unique on `identity_key`.
- `job_research`, `job_artifacts` - research payloads and generated files, both keyed on
  `identity_key` so they survive a lead being promoted.
- `experiences` - structured resume bullets with tags.
- `sender_denylist` - domains you have marked as never job related.
- `profile` - key/value store for profile text, settings, and sync cursors.
- `email_matches` - the legacy per-job scanner's suggestions, unchanged.

### Migrations

Schema changes are gated on `PRAGMA user_version`. A fresh database is stamped at the
current version and never runs migration code; an existing one is upgraded in order, each
step in its own transaction. A structural change takes a backup first, into `backups/`.

The v1 backfill computes an identity for every existing row. Where two rows collapse onto
one key - the same role logged twice from two boards - **nothing is merged**. They are
reported for review, because a wrong merge destroys application history and you would not
see it happen.

## Future work

- Web automation integration: use tools such as Beautiful Soup or Selenium to scrape job portals, follow relevant links, and extract information with regex operations for automatic verification of job postings and unique Job IDs.
- Pop-up feature: create a companion web popup app in the same repo that works with the same local computer API. It can quickly store job postings for later review and help autofill resume contents.
- Merge flow for duplicate identities surfaced by the v1 backfill, so two rows for one role
  can be combined without losing either one's history.
- Authentication, so the app can be exposed beyond loopback without a reverse proxy in front.
- More board parsers. LinkedIn and Indeed are deterministic; everything else falls back to
  the model, which works but costs more and is less exact.
