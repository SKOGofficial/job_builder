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
  - Daily applications chart
- Dark and light mode with a restrained, color-blind-friendly palette.
- Hamburger menu with placeholder/storage pages for:
  - Settings
  - Profile
  - Resume & Experiences

## Running the app

This project uses only Python standard library modules.

```powershell
python app.py
```

The SQLite database is created automatically as `job_applications.sqlite3` in the project folder.

## Product report website

A static product report website is available at `website/index.html`. It describes the current tracker,
dashboard direction, future Gmail and Grok API integration concept, and offer comparison roadmap without
implementing those integrations.

## Data model

The `jobs` table stores every application. Each posting URL is normalized and hashed with SHA-256 to create a stable URL-derived Job ID. If the same URL is used again, the app warns the user and can create a correlated Job ID with a numeric suffix.

The `profile` table stores local profile, settings, and resume/experience notes used by the hamburger menu pages.

## Future work

- Mail integration: integrate the Gmail API to detect when a company replies about a job application, so the user knows when to revisit the company portal.
- Web automation integration: use tools such as Beautiful Soup or Selenium to scrape job portals, follow relevant links, and extract information with regex operations for automatic verification of job postings and unique Job IDs.
- Pop-up feature: create a companion web popup app in the same repo that works with the same local computer API. It can quickly store job postings for later review and help autofill resume contents.
- Resume, CV, and Experience: add a resume builder that uses LaTeX to generate tailored resumes from existing resumes and stored experience. It should map experience bullets to the job description and keywords extracted from the posting.
