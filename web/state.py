"""Process-wide state: the database and the long-running workers.

The store, the Gmail scanner, the classifier, and the ingest pipeline are
created once and shared, so a scan or a classification cycle keeps running
while the user moves between pages. NiceGUI rebuilds a page per client; this
deliberately does not.

Single-user assumption: one of everything for the process. Serving several
people from one instance would need per-user state, which is a design change
rather than a configuration flag.

Database calls stay on the event loop thread, which is the thread that opened
the sqlite connection. Blocking network calls are the only thing handed to a
worker thread, and every worker takes an executor for exactly that purpose.
The background scheduler runs as an asyncio task on that same loop, which is
what keeps the invariant intact - a thread that touched the store would break
it.

`store` owns applications; `mail` owns the mailbox mirror, leads, and
generated artifacts. They share one connection, so an operation spanning both
(promoting a lead into a job) is still atomic.
"""

from clients.gmail_client import GmailScanner
from clients.llm_client import ClassificationRunner
from utilities.mailstore import MailStore
from utilities.store import JobStore

_state = None


class AppState:
    def __init__(self, store=None):
        self.store = store or JobStore()
        self.mail = MailStore(self.store.conn)
        self.scanner = GmailScanner(self.store)
        self.classifier = ClassificationRunner(self.store)

        # Built lazily by web/startup.py so importing this module stays cheap
        # for tests and the CLI, which do not want a scheduler.
        self.pipeline = None
        self.scheduler = None

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
