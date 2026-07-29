"""Gmail read-only access for the Job Board Tracker.

Kept separate from app.py so the UI never touches OAuth mechanics directly.

Credential model:

- Client ID and secret come from .env. For a Desktop OAuth client these are public
  per RFC 8252; they identify the app and do not grant mailbox access on their own.
  PKCE plus the localhost redirect restriction is what protects the flow.
- The refresh token is the only real credential. It lives in Windows Credential
  Manager through keyring, never in the project folder.

The only scope requested is gmail.readonly. Nothing here sends, deletes, or
modifies mail.
"""

import os
import re
from datetime import date, timedelta

import keyring
import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

KEYRING_SERVICE = "job_builder_gmail"
KEYRING_USERNAME = "refresh_token"

TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"


class GmailNotConfigured(Exception):
    """Raised when .env is missing the client ID or secret."""


class GmailNotConnected(Exception):
    """Raised when no refresh token has been stored yet."""


def client_config():
    load_dotenv()
    client_id = os.environ.get("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise GmailNotConfigured(
            "GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be set in .env. "
            "Copy .env.example to .env and fill in your Google Cloud OAuth client values."
        )
    return client_id, client_secret


def stored_refresh_token():
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
    # 200 means revoked. 400 usually means it was already invalid, which is
    # equally fine to clear locally. Anything else is a real failure.
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

    format="metadata" with an explicit header allowlist means message bodies are
    never downloaded, so nothing sensitive is held in memory or written to disk.
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


def sender_domain(sender):
    """Extract the domain from a From header value."""
    match = re.search(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", sender or "")
    return match.group(1).lower() if match else ""


# Suffixes stripped before turning a company name into a domain guess or a
# subject keyword, so "Acme Corp." and "Acme" behave the same.
COMPANY_SUFFIXES = {
    "inc", "inc.", "llc", "l.l.c.", "ltd", "ltd.", "limited", "corp", "corp.",
    "corporation", "co", "co.", "company", "plc", "gmbh", "ag", "sa", "nv", "bv",
    "group", "holdings", "technologies", "technology", "labs", "systems",
}

# Free and shared mail domains never count as a domain match. A recruiter mailing
# from gmail.com would otherwise match every job whose company slug is short.
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
    company slug to appear in the subject. Body text is deliberately not
    considered: company names are short and collide with unrelated mail, and a
    false positive here would suggest a wrong status change to the user.
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
