"""Gmail read-only access and workflow integration for the Job Board Tracker.

Combines OAuth mechanics, Gmail API calls, and UI workflow orchestration into a
single module so external service details stay out of the UI.

Credential model:

- Client ID and secret come from .env. For a Desktop OAuth client these are public
  per RFC 8252; they identify the app and do not grant mailbox access on their own.
  PKCE plus the localhost redirect restriction is what protects the flow.
- The refresh token is the only real credential. It lives in Windows Credential
  Manager through keyring, never in the project folder.

The only scope requested is gmail.readonly. Nothing here sends, deletes, or
modifies mail.

Message text is fetched in two passes. Headers alone decide whether a message
matches an application; the body is downloaded only afterwards, for messages
that already matched, so mail the user will never see is never read.
"""

import base64
import binascii
import html
import os
import re
from datetime import date, timedelta
from tkinter import messagebox

try:
    import keyring
    import requests
    from dotenv import load_dotenv
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    GMAIL_AVAILABLE = True
    GMAIL_IMPORT_ERROR = ""
except ImportError as exc:
    keyring = None
    requests = None
    load_dotenv = None
    Request = None
    Credentials = None
    InstalledAppFlow = None
    build = None
    GMAIL_AVAILABLE = False
    GMAIL_IMPORT_ERROR = str(exc)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

KEYRING_SERVICE = "job_builder_gmail"
KEYRING_USERNAME = "refresh_token"

TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"

MISSING_PACKAGES_HINT = (
    "Gmail support needs extra packages. Run: pip install -r requirements.txt"
)

#: Upper bound on stored body text. Long newsletters would otherwise bloat the
#: database and the Text widget for no reading benefit.
MAX_BODY_CHARS = 20000


class GmailNotConfigured(Exception):
    """Raised when .env is missing the client ID or secret."""


class GmailNotConnected(Exception):
    """Raised when no refresh token has been stored yet."""


def client_config():
    if load_dotenv:
        env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
        )
        load_dotenv(dotenv_path=env_path)
    client_id = os.environ.get("GMAIL_CLIENT_ID", "").strip() if os.environ else ""
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "").strip() if os.environ else ""
    if not client_id or not client_secret:
        raise GmailNotConfigured(
            "GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be set in .env. "
            "Copy .env.example to .env and fill in your Google Cloud OAuth client values."
        )
    return client_id, client_secret


def stored_refresh_token():
    if not keyring:
        return None
    return keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)


def is_connected():
    try:
        client_config()
    except GmailNotConfigured:
        return False
    return bool(stored_refresh_token())


def load_credentials():
    """Build credentials from .env config plus the keyring refresh token."""
    client_id, client_secret = client_config()
    refresh_token = stored_refresh_token()
    if not refresh_token:
        raise GmailNotConnected("Gmail is not connected yet. Use Connect Gmail in Settings.")
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


def run_auth_flow():
    """Open the browser for consent and store the resulting refresh token.

    Consent happens entirely in the user's browser. This function never sees or
    handles the account password.
    """
    client_id, client_secret = client_config()
    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": TOKEN_URI,
                "redirect_uris": ["http://localhost"],
            }
        },
        SCOPES,
    )
    creds = flow.run_local_server(port=0, prompt="consent")
    if not creds.refresh_token:
        raise RuntimeError(
            "Google did not return a refresh token. Revoke the app's access in your "
            "Google account settings and connect again."
        )
    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, creds.refresh_token)
    return creds


def disconnect():
    """Revoke access with Google first, then drop the local token.

    Revoking before deleting matters: if the revoke fails and the local copy is
    already gone, a live token would remain valid with no way to see or revoke it
    from this app.
    """
    refresh_token = stored_refresh_token()
    if not refresh_token:
        return False
    response = requests.post(
        REVOKE_URI,
        params={"token": refresh_token},
        headers={"content-type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if response.status_code not in (200, 400):
        raise RuntimeError(
            f"Could not revoke the token with Google (HTTP {response.status_code}). "
            "The local token was kept so you can retry."
        )
    keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
    return True


def _service(creds=None):
    creds = creds or load_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def search_messages(query, max_results=25, creds=None):
    """Return message id/threadId dicts matching a Gmail search query."""
    service = _service(creds)
    response = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )
    return response.get("messages", [])


def get_message_headers(message_id, creds=None):
    """Fetch only the headers for a message.

    format="metadata" with an explicit header allowlist keeps this call to the
    three headers matching needs. Bodies are a separate call (get_message_body)
    made only once a message has matched, so a message that never matches is
    never read beyond these headers.
    """
    service = _service(creds)
    message = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        )
        .execute()
    )
    headers = {
        h["name"].lower(): h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "sender": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
    }


def _decode_part(body):
    """Base64url-decode one payload part, tolerating missing or bad data."""
    data = (body or {}).get("data")
    if not data:
        return ""
    # Gmail strips base64 padding; put it back before decoding.
    padded = data + "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return ""
    return raw.decode("utf-8", errors="replace")


def _html_to_text(markup):
    """Reduce an HTML part to readable plain text."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|h[1-6])\s*>", "\n\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _collect_parts(payload, mime_type):
    """Return decoded text from every part of the given MIME type, depth first."""
    if not isinstance(payload, dict):
        return []
    found = []
    if payload.get("mimeType") == mime_type:
        text = _decode_part(payload.get("body"))
        if text:
            found.append(text)
    for part in payload.get("parts") or []:
        found.extend(_collect_parts(part, mime_type))
    return found


def extract_body(payload, max_chars=MAX_BODY_CHARS):
    """Pull readable text out of a Gmail message payload.

    Prefers text/plain and falls back to text/html with the tags stripped, since
    plenty of recruiting mail ships HTML only. Attachments contribute nothing:
    parts without inline text data are skipped rather than downloaded.
    """
    parts = _collect_parts(payload, "text/plain")
    if not any(part.strip() for part in parts):
        parts = [_html_to_text(part) for part in _collect_parts(payload, "text/html")]
    text = "\n\n".join(part.strip() for part in parts if part.strip())
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n\n[... truncated ...]"
    return text


def get_message_body(message_id, creds=None):
    """Fetch a full message and return its readable text plus Gmail's snippet."""
    service = _service(creds)
    message = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    return {
        "body": extract_body(message.get("payload", {})),
        "snippet": html.unescape(message.get("snippet", "") or ""),
    }


def sender_domain(sender):
    """Extract the domain from a From header value."""
    match = re.search(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", sender or "")
    return match.group(1).lower() if match else ""


COMPANY_SUFFIXES = {
    "inc", "inc.", "llc", "l.l.c.", "ltd", "ltd.", "limited", "corp", "corp.",
    "corporation", "co", "co.", "company", "plc", "gmbh", "ag", "sa", "nv", "bv",
    "group", "holdings", "technologies", "technology", "labs", "systems",
}

GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "live.com", "icloud.com", "aol.com", "proton.me", "protonmail.com",
}


def company_slug(company):
    """Reduce a company name to a lowercase token used for matching."""
    cleaned = re.sub(r"[^\w\s-]", " ", (company or "").lower())
    words = [w for w in cleaned.split() if w and w not in COMPANY_SUFFIXES]
    return "".join(words)


def build_query(company, application_date, extra_days=1):
    """Build a Gmail search query for replies about one application.

    Scoped to mail received on or after the application date. Gmail's after:
    filter is exclusive at day granularity in some timezones, so the window
    starts a day early to avoid dropping same-day replies.
    """
    slug = company_slug(company)
    if not slug:
        return ""
    start = date.fromisoformat(application_date) - timedelta(days=extra_days)
    return f'(from:{slug} OR subject:{slug}) after:{start.strftime("%Y/%m/%d")}'


def message_matches_company(message, company):
    """Conservative check that a message really relates to this company.

    Requires either the sender's domain to contain the company slug, or the
    company slug to appear in the subject. Body text stays out of this decision
    even though matches now store it: company names are short and collide with
    unrelated mail, and a false positive here would suggest a wrong status
    change to the user.
    """
    slug = company_slug(company)
    if not slug:
        return False
    domain = sender_domain(message.get("sender", ""))
    if domain and domain not in GENERIC_DOMAINS:
        domain_root = domain.rsplit(".", 1)[0].replace(".", "").replace("-", "")
        if slug in domain_root or domain_root.endswith(slug):
            return True
    subject_slug = company_slug(message.get("subject", ""))
    return bool(subject_slug) and slug in subject_slug


class GmailWorkflow:
    def __init__(self, app):
        self.app = app

    @property
    def available(self):
        return GMAIL_AVAILABLE

    def is_connected(self):
        return GMAIL_AVAILABLE and is_connected()

    def connect(self):
        try:
            run_auth_flow()
        except GmailNotConfigured as exc:
            messagebox.showerror("Gmail not configured", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Could not connect", str(exc))
            return
        messagebox.showinfo("Gmail connected", "Gmail is connected with read-only access.")
        self.app.show_page("settings")

    def disconnect(self):
        if not messagebox.askyesno(
            "Disconnect Gmail",
            "Revoke this app's access to your Gmail account and remove the stored token?",
        ):
            return
        try:
            disconnect()
        except Exception as exc:
            messagebox.showerror("Could not disconnect", str(exc))
            return
        messagebox.showinfo("Gmail disconnected", "Access was revoked and the token removed.")
        self.app.show_page("settings")

    def scan(self):
        """Look for replies to open applications and record them as suggestions.

        Nothing is applied to a job here. Every hit becomes a pending suggestion
        the user confirms or dismisses on the Email matches page.
        """
        store = self.app.store
        try:
            creds = load_credentials()
        except Exception as exc:
            messagebox.showerror("Gmail unavailable", str(exc))
            return

        jobs = store.jobs_awaiting_response()
        if not jobs:
            messagebox.showinfo("Nothing to check", "No applications are waiting on a response.")
            return

        found = 0
        skipped = []
        for job in jobs:
            query = build_query(job["company"], job["application_date"])
            if not query:
                skipped.append(job["position_title"])
                continue
            try:
                messages = search_messages(query, creds=creds)
            except Exception as exc:
                messagebox.showerror(
                    "Gmail search failed",
                    f"Stopped after checking {found} match(es).\n\n{exc}",
                )
                break
            seen = store.known_message_ids(job["job_id"])
            for stub in messages:
                if stub["id"] in seen:
                    continue
                headers = get_message_headers(stub["id"], creds=creds)
                if not message_matches_company(headers, job["company"]):
                    continue
                try:
                    headers.update(get_message_body(stub["id"], creds=creds))
                except Exception:
                    # A body that will not download is not worth losing the
                    # match over; the page shows a placeholder for empty text.
                    headers.setdefault("body", "")
                    headers.setdefault("snippet", "")
                if store.record_email_match(job["job_id"], headers):
                    found += 1

        summary = f"Found {found} new possible repl{'y' if found == 1 else 'ies'}."
        if skipped:
            summary += f"\n\nSkipped {len(skipped)} job(s) with no company name recorded."
        if found:
            summary += "\n\nReview them on the Email matches page."
        messagebox.showinfo("Gmail check complete", summary)
        self.app.show_page("email_matches")

        # New matches are exactly what the classifier exists to label, so a scan
        # that found something rolls straight into a classification cycle.
        classifier = getattr(self.app, "classifier", None)
        if found and classifier is not None and classifier.is_configured():
            classifier.start()
