"""Gmail read-only access and workflow integration for the Job Board Tracker.

Combines OAuth mechanics, Gmail API calls, and UI workflow orchestration into a
single module so external service details stay out of the UI.

Credential model:

- Client ID and secret come from .env. For a Desktop OAuth client these are public
  per RFC 8252; they identify the app and do not grant mailbox access on their own.
  PKCE plus the localhost redirect restriction is what protects the flow.
- The refresh token is the only real credential. It lives in Windows Credential
  Manager through keyring, never in the project folder.

This module holds no UI code. Scan progress is published to subscribers and the
caller decides how to display it, so the same client works behind any front end.

The only scope requested is gmail.readonly. Nothing here sends, deletes, or
modifies mail.

Message text is fetched in two passes. Headers alone decide whether a message
matches an application; the body is downloaded only afterwards, for messages
that already matched, so mail the user will never see is never read.
"""

import asyncio
import base64
import binascii
import html
import logging
import os
import re
import sys
from datetime import date, timedelta

from utilities import credentials
from utilities.identity import COMPANY_SUFFIXES, company_slug

log = logging.getLogger(__name__)

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
    """Return the stored refresh token, or None when none is available.

    A machine with no usable credential store reports no token rather than
    raising, which reads as "not connected" — the same as never having
    connected, and the state the Settings page already handles.
    """
    return credentials.read_secret(KEYRING_SERVICE, KEYRING_USERNAME)


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


#: Fixed so the consent redirect can be forwarded from a desktop:
#:     ssh -L 8765:localhost:8765 server
#: A random port (the previous `port=0`) cannot be tunnelled, because you do
#: not know which port to forward until after the flow has already started.
#: Loopback redirects on any port are permitted for Desktop OAuth clients per
#: RFC 8252, so this needs no Google Cloud console change.
AUTH_REDIRECT_PORT = int(os.environ.get("GMAIL_OAUTH_PORT", "8765"))


def run_auth_flow(open_browser=None, port=None):
    """Run OAuth consent and store the resulting refresh token.

    Consent happens entirely in the user's browser. This function never sees or
    handles the account password.

    On a headless server pass `open_browser=False`: the flow prints the
    authorization URL instead of trying to launch a browser that does not
    exist. Open that URL on a machine that has one, with the port forwarded,
    and the redirect lands back here.

    `open_browser` defaults to False whenever no display is detectable, so the
    server case works without the caller having to know.
    """
    client_id, client_secret = client_config()
    port = AUTH_REDIRECT_PORT if port is None else port
    if open_browser is None:
        open_browser = display_available()

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": TOKEN_URI,
                "redirect_uris": [f"http://localhost:{port}"],
            }
        },
        SCOPES,
    )
    creds = flow.run_local_server(
        port=port,
        open_browser=open_browser,
        prompt="consent",
        authorization_prompt_message=(
            "Open this URL to authorise Gmail access.\n"
            f"If this machine has no browser, forward the port first:\n"
            f"    ssh -L {port}:localhost:{port} <this-server>\n"
            "then open the URL on your desktop.\n\n    {url}\n"
        ),
    )
    if not creds.refresh_token:
        raise RuntimeError(
            "Google did not return a refresh token. Revoke the app's access in your "
            "Google account settings and connect again."
        )
    credentials.write_secret(KEYRING_SERVICE, KEYRING_USERNAME, creds.refresh_token)
    return creds


def display_available():
    """Whether launching a browser on this machine could work.

    Windows and macOS always have a session; on Linux a graphical session sets
    DISPLAY or WAYLAND_DISPLAY, and an SSH shell on a server sets neither.
    """
    if os.name == "nt" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


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
    credentials.delete_secret(KEYRING_SERVICE, KEYRING_USERNAME)
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


#: Headers the rough filter and the classifier need. `List-Unsubscribe` marks
#: automated bulk mail, which is one of the drop signals.
METADATA_HEADERS = ["From", "Subject", "Date", "List-Unsubscribe"]


def get_message_headers(message_id, creds=None):
    """Fetch only the headers and labels for a message.

    format="metadata" with an explicit header allowlist keeps this call small.
    Bodies are a separate call (get_message_body) made only once the rough
    filter has passed the message, so mail that is obviously not job related
    costs one metadata fetch and nothing more.

    `labels` carries Gmail's own categorisation, which the rough filter uses:
    CATEGORY_SOCIAL and CATEGORY_FORUMS are safe to drop, while PROMOTIONS and
    UPDATES are not - job alerts routinely land in both.
    """
    service = _service(creds)
    message = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=METADATA_HEADERS,
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
        "list_unsubscribe": headers.get("list-unsubscribe", ""),
        "labels": message.get("labelIds", []) or [],
        "snippet": html.unescape(message.get("snippet", "") or ""),
    }


# --- mailbox-wide sync -------------------------------------------------------


class GmailHistoryExpired(Exception):
    """Raised when Gmail no longer holds history from our stored cursor.

    Google keeps history for roughly a week. Past that the incremental path is
    gone and the caller has to fall back to a bounded full list and re-seed the
    cursor. Not an error condition - just a slower path.
    """


def get_profile(creds=None):
    """Mailbox profile, including the current historyId to sync forward from."""
    service = _service(creds)
    return service.users().getProfile(userId="me").execute()


def list_history(start_history_id, creds=None, max_pages=20):
    """Message IDs changed since `start_history_id`.

    Returns `(message_ids, new_history_id)`. This is the cheap path: 2 quota
    units per page against 5 for a list call, and it returns only what actually
    changed rather than re-walking the mailbox.

    Raises `GmailHistoryExpired` when the cursor is too old, which is a normal
    outcome after downtime longer than Gmail's retention.
    """
    service = _service(creds)
    message_ids = []
    latest = start_history_id
    page_token = None
    pages = 0

    while pages < max_pages:
        try:
            response = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                )
                .execute()
            )
        except Exception as exc:  # googleapiclient raises HttpError
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                raise GmailHistoryExpired(
                    f"Gmail no longer has history from {start_history_id}"
                ) from exc
            raise

        for record in response.get("history", []) or []:
            for added in record.get("messagesAdded", []) or []:
                message = added.get("message") or {}
                if message.get("id"):
                    message_ids.append(message["id"])
        latest = response.get("historyId", latest)
        page_token = response.get("nextPageToken")
        pages += 1
        if not page_token:
            break

    # Gmail can report the same message across pages; order is not meaningful.
    return list(dict.fromkeys(message_ids)), latest


def iter_message_ids(query="", creds=None, max_results=None, page_size=500):
    """Every message ID matching a query, following pagination.

    The bounded full-sync path: used to seed a new install, and to recover when
    the history cursor has expired. `max_results` caps the walk so a first run
    over a decade-old mailbox does not run unbounded.
    """
    service = _service(creds)
    collected = []
    page_token = None

    while True:
        response = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=page_size,
                pageToken=page_token,
            )
            .execute()
        )
        for stub in response.get("messages", []) or []:
            collected.append(stub["id"])
            if max_results and len(collected) >= max_results:
                return collected
        page_token = response.get("nextPageToken")
        if not page_token:
            return collected


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


# `COMPANY_SUFFIXES` and `company_slug` now live in `utilities/identity.py`,
# because the identity model needs them too and `store.py` must not import
# from `clients/`. Re-exported here so this module's public surface is
# unchanged for existing callers.
__all_shared__ = ("COMPANY_SUFFIXES", "company_slug")

#: Free mail providers. A sender here is never treated as the company itself,
#: no matter what the local part says.
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "live.com", "icloud.com", "aol.com", "proton.me", "protonmail.com",
    "yahoo.co.uk", "hotmail.co.uk", "me.com", "mac.com", "gmx.com",
    "zoho.com", "fastmail.com", "hey.com", "pm.me",
}


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


IDLE, RUNNING, DONE, ERROR = "idle", "running", "done", "error"


class GmailScanner:
    """Scans for replies to open applications and records them as suggestions.

    Nothing is applied to a job here. Every hit becomes a pending suggestion the
    user confirms or dismisses on the Email matches page.

    The scan is async so the UI stays responsive during what is a long run of
    network calls. Every blocking Google call goes through an injectable
    executor (asyncio.to_thread by default), while database access stays on the
    calling thread, which is the one that owns the sqlite connection.

    This class holds no reference to a UI toolkit. Progress reaches the screen
    through subscribers, and the caller decides how to present it.
    """

    def __init__(self, store, executor=None, credential_loader=None):
        self.store = store
        self.executor = executor or asyncio.to_thread
        self.credential_loader = credential_loader or load_credentials
        self.state = IDLE
        self.total = 0
        self.checked = 0
        self.found = 0
        self.current = ""
        self.message = ""
        self.skipped = []
        self.listeners = []

    # Status ----------------------------------------------------------------

    @property
    def available(self):
        return GMAIL_AVAILABLE

    def is_connected(self):
        return GMAIL_AVAILABLE and is_connected()

    @property
    def busy(self):
        return self.state == RUNNING

    def subscribe(self, callback):
        self.listeners.append(callback)
        return callback

    def unsubscribe(self, callback):
        if callback in self.listeners:
            self.listeners.remove(callback)

    def emit(self):
        for callback in list(self.listeners):
            callback(self)

    def progress_text(self):
        if self.state == RUNNING:
            suffix = f" — {self.current}" if self.current else ""
            return f"Checking {min(self.checked + 1, self.total)} of {self.total}{suffix}"
        return self.message

    # Scanning --------------------------------------------------------------

    async def scan(self):
        """Run one scan. Returns the number of new matches recorded."""
        if self.busy:
            return 0
        self.state = RUNNING
        self.checked = 0
        self.found = 0
        self.current = ""
        self.message = ""
        self.skipped = []

        jobs = self.store.jobs_awaiting_response()
        self.total = len(jobs)
        if not jobs:
            self.state = DONE
            self.message = "No applications are waiting on a response."
            self.emit()
            return 0
        self.emit()

        try:
            creds = await self.executor(self.credential_loader)
        except Exception as exc:
            self.state = ERROR
            self.message = f"Gmail unavailable: {exc}"
            self.emit()
            return 0

        for job in jobs:
            self.current = job["company"] or job["position_title"] or ""
            self.emit()
            query = build_query(job["company"], job["application_date"])
            if not query:
                self.skipped.append(job["position_title"])
                self.checked += 1
                continue
            try:
                await self.scan_job(job, query, creds)
            except Exception as exc:
                self.state = ERROR
                self.message = (
                    f"Gmail search failed after {self.found} match(es): {exc}"
                )
                self.emit()
                return self.found
            self.checked += 1
            self.emit()

        self.state = DONE
        self.message = self.summary()
        self.emit()
        return self.found

    async def scan_job(self, job, query, creds):
        messages = await self.executor(search_messages, query, 25, creds)
        seen = self.store.known_message_ids(job["job_id"])
        for stub in messages:
            if stub["id"] in seen:
                continue
            headers = await self.executor(get_message_headers, stub["id"], creds)
            if not message_matches_company(headers, job["company"]):
                continue
            try:
                headers.update(await self.executor(get_message_body, stub["id"], creds))
            except Exception:
                # A body that will not download is not worth losing the match
                # over; the page shows a placeholder for empty text.
                headers.setdefault("body", "")
                headers.setdefault("snippet", "")
            if self.store.record_email_match(job["job_id"], headers):
                self.found += 1

    def summary(self):
        text = f"Found {self.found} new possible repl{'y' if self.found == 1 else 'ies'}."
        if self.skipped:
            text += f" Skipped {len(self.skipped)} job(s) with no company name recorded."
        return text
