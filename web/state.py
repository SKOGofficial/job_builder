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
        """
        Summary:
            Build the shared process state: the store and every long-running
            worker.

        Parameters:
            store (JobStore | None): The store to use. None opens the real
                database via `JobStore()`. Tests pass a store pointed at a
                temporary database.

        Note:
            `pipeline` and `scheduler` are left None here and built lazily by
            `web/startup.py`, so importing this module stays cheap for tests
            and the CLI, which do not want a background scheduler running.
        """
        self.store = store or JobStore()
        self.mail = MailStore(self.store.conn)
        self.scanner = GmailScanner(self.store)
        self.classifier = ClassificationRunner(self.store)

        # Built lazily by web/startup.py so importing this module stays cheap
        # for tests and the CLI, which do not want a scheduler.
        self.pipeline = None
        self.scheduler = None
        self._pool = None

    @property
    def pool(self):
        """The model providers, built once and shared.

        One pool for the process, for the same reason the scanner and the
        classifier are: a cooldown earned at 14:00 has to still be in force at
        14:10. It is also what lets Settings show the pipeline's real remaining
        budget rather than a fresh pool's optimistic zero.

        Summary:
            Return the process-wide provider pool, creating it on first use.

        Returns:
            ProviderPool: The shared pool.

        Note:
            Built lazily and imported inside the property so this module stays
            importable without the provider dependencies, which is what the
            page tests rely on.
        """
        if self._pool is None:
            from clients.providers.pool import ProviderPool

            self._pool = ProviderPool(mail=self.mail)
        return self._pool

    @property
    def dark(self):
        """
        Summary:
            Whether the user's stored theme preference is dark mode.

        Returns:
            bool: True when the stored `theme` profile value is "dark".
                Defaults to light when no preference has been saved.

        Raises:
            sqlite3.Error: Propagated from `JobStore.get_profile_value`.
        """
        return self.store.get_profile_value("theme", "light") == "dark"

    def save_dark(self, value):
        """
        Summary:
            Persist the user's dark-mode choice.

        Parameters:
            value (bool): True to store "dark", False to store "light".

        Raises:
            sqlite3.Error: Propagated from `JobStore.save_profile_value`.
        """
        self.store.save_profile_value("theme", "dark" if value else "light")


def get_state():
    """
    Summary:
        Return the process-wide `AppState`, creating it on first call.

    Returns:
        AppState: The shared state. The same instance on every call within a
            process, unless replaced by `set_state`.

    Raises:
        sqlite3.Error: Propagated from `JobStore()` if the first call cannot
            open the database.
    """
    global _state
    if _state is None:
        _state = AppState()
    return _state


def set_state(state):
    """Replace the shared state. Used by tests to point at a temporary store.

    Summary:
        Replace the process-wide `AppState`.

    Parameters:
        state (AppState | None): The state to install. None clears it, so the
            next `get_state()` call builds a fresh one.

    Returns:
        AppState | None: The value that was set, unchanged.
    """
    global _state
    _state = state
    return _state
