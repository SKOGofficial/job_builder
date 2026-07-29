"""Coordinates Gmail access with the job store and the UI.

`gmail_client` knows how to talk to Google and nothing about jobs. This module
sits between the two: it decides which jobs to check, applies the match rules,
and reports results through dialogs. Both the Settings page and the Email
matches page drive Gmail through this one object, so the behaviour cannot drift
between them.
"""

from tkinter import messagebox

# Gmail support is optional. The tracker is usable as a purely local tool
# without the Google libraries installed, so an import failure disables the
# feature instead of stopping the app from launching.
try:
    import gmail_client

    GMAIL_AVAILABLE = True
    GMAIL_IMPORT_ERROR = ""
except ImportError as exc:
    gmail_client = None
    GMAIL_AVAILABLE = False
    GMAIL_IMPORT_ERROR = str(exc)


MISSING_PACKAGES_HINT = (
    "Gmail support needs extra packages. Run: pip install -r requirements.txt"
)


class GmailWorkflow:
    def __init__(self, app):
        self.app = app

    @property
    def available(self):
        return GMAIL_AVAILABLE

    def is_connected(self):
        return GMAIL_AVAILABLE and gmail_client.is_connected()

    def connect(self):
        try:
            gmail_client.run_auth_flow()
        except gmail_client.GmailNotConfigured as exc:
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
            gmail_client.disconnect()
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
            creds = gmail_client.load_credentials()
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
            query = gmail_client.build_query(job["company"], job["application_date"])
            if not query:
                skipped.append(job["position_title"])
                continue
            try:
                messages = gmail_client.search_messages(query, creds=creds)
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
                headers = gmail_client.get_message_headers(stub["id"], creds=creds)
                if not gmail_client.message_matches_company(headers, job["company"]):
                    continue
                if store.record_email_match(job["job_id"], headers):
                    found += 1

        summary = f"Found {found} new possible repl{'y' if found == 1 else 'ies'}."
        if skipped:
            summary += f"\n\nSkipped {len(skipped)} job(s) with no company name recorded."
        if found:
            summary += "\n\nReview them on the Email matches page."
        messagebox.showinfo("Gmail check complete", summary)
        self.app.show_page("email_matches")
