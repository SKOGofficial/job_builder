"""Process-wide state: the database and the two long-running workers.

The store, the Gmail scanner, and the classifier are created once and shared,
so a scan or a classification cycle keeps running while the user moves between
pages. NiceGUI rebuilds a page per client; this deliberately does not.

Single-user assumption: one store, one scanner, one classifier for the process.
Serving several people from one instance would need per-user state, which is a
design change rather than a configuration flag.

Database calls stay on the event loop thread, which is the thread that opened
the sqlite connection. Blocking network calls are the only thing handed to a
worker thread, and both workers take an executor for exactly that purpose.
"""

from clients.gmail_client import GmailScanner
from clients.llm_client import ClassificationRunner
from utilities.store import JobStore

_state = None


class AppState:
    def __init__(self, store=None):
        self.store = store or JobStore()
        self.scanner = GmailScanner(self.store)
        self.classifier = ClassificationRunner(self.store)

    @property
    def dark(self):
        return self.store.get_profile_value("theme", "light") == "dark"

    def save_dark(self, value):
        self.store.save_profile_value("theme", "dark" if value else "light")


def get_state():
    global _state
    if _state is None:
        _state = AppState()
    return _state


def set_state(state):
    """Replace the shared state. Used by tests to point at a temporary store."""
    global _state
    _state = state
    return _state
