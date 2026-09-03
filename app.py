"""Job Board Tracker: application entry point.

The UI is NiceGUI, served locally and opened in a native window by default. The
database lives in `utilities/store.py` and each external service gets its own
module under `clients/`, none of which import anything from the UI.

Run it with:

    python app.py

Pass --browser to open a normal tab instead of a native window, or --port to
move it off 8080.
"""

import argparse
import logging
import os

import clients.gmail_client as _gmail_client_mod
import clients.llm_client as _llm_client_mod
from clients.gmail_client import GMAIL_AVAILABLE, GMAIL_IMPORT_ERROR, GmailScanner
from clients.llm_client import GROQ_AVAILABLE, GROQ_IMPORT_ERROR, ClassificationRunner
from utilities.store import DB_PATH, JobStore, normalize_url, today_iso, url_hash
from utilities.theme import (
    CHART_COLOR,
    JOB_TYPES,
    PAY_PERIODS,
    STATUS_COLORS,
    STATUSES,
    TIME_RANGES,
)

gmail_client = _gmail_client_mod if GMAIL_AVAILABLE else None
llm_client = _llm_client_mod if GROQ_AVAILABLE else None

# Re-exported so `import app` remains a single convenient entry point for tests
# and scripts even though the implementations live in dedicated modules.
__all__ = [
    "JobStore",
    "DB_PATH",
    "normalize_url",
    "url_hash",
    "today_iso",
    "CHART_COLOR",
    "STATUS_COLORS",
    "TIME_RANGES",
    "JOB_TYPES",
    "STATUSES",
    "PAY_PERIODS",
    "GMAIL_AVAILABLE",
    "GMAIL_IMPORT_ERROR",
    "GmailScanner",
    "gmail_client",
    "GROQ_AVAILABLE",
    "GROQ_IMPORT_ERROR",
    "ClassificationRunner",
    "llm_client",
    "main",
]

DEFAULT_PORT = 8080

#: Loopback, and this is the app's entire access control model - there is no
#: login page. Anything else exposes the mailbox mirror to that network.
DEFAULT_HOST = "127.0.0.1"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Job Board Tracker")
    parser.add_argument(
        "--browser",
        action="store_true",
        help="open in a normal browser tab instead of a native window",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help=(
            "serve without a window or browser, for running as a service. "
            "Reach it over an SSH tunnel or Tailscale."
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            "interface to bind (default 127.0.0.1). The app has no "
            "authentication, so binding anything else exposes your mailbox "
            "mirror to that network."
        ),
    )
    parser.add_argument(
        "--no-poll",
        action="store_true",
        help="do not start the background Gmail poller",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to serve on")
    return parser.parse_args(argv)


def native_available():
    """Whether a native window can be opened. Needs pywebview."""
    try:
        import webview  # noqa: F401
    except ImportError:
        return False
    return True


def configure_logging():
    """Send logs to stderr so systemd's journal captures them.

    The scanner and classifier surface errors into a UI string, which vanishes
    when no page is open. On a server that is most of the time, so anything
    worth diagnosing at 3am has to reach the journal instead.
    """
    logging.basicConfig(
        level=os.environ.get("JOB_BUILDER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("nicegui").addFilter(_TimerTeardownNoise())


class _TimerTeardownNoise(logging.Filter):
    """Drop the traceback NiceGUI logs when a page timer outlives its page.

    Navigating away from Settings or Email matches logs a full traceback ending
    in `RuntimeError: The parent slot of Timer(id=N) has been deleted.` The app
    is fine - the timer is already on its way out - but at a 0.4s tick the race
    is hit on almost every navigation, and a traceback that appears during
    normal use trains you to ignore tracebacks.

    It cannot be fixed from our side. `Timer._run_in_loop` evaluates
    `with self._get_context():` *before* consulting `_should_stop()`, and it is
    `_get_context` that raises on a deleted element - so the element is gone
    before anything checks whether the timer should still be running.
    `web/shell.py`'s `page_timer` cancels on disconnect, which sets the flag
    that `_should_stop` reads, and the raise still beats it there.

    So this is suppression, knowingly: one exact message, on one logger, for a
    condition with no user-visible effect. Every other NiceGUI error still gets
    through.

    Summary:
        Filter out NiceGUI's benign timer-teardown traceback.
    """

    #: Matched on the message rather than the exception type, because by the
    #: time it reaches a handler it is an ordinary log record. Narrow enough
    #: that a real RuntimeError about anything else still passes.
    MESSAGE = "parent slot of Timer"

    def filter(self, record):
        """
        Summary:
            Decide whether one log record should be emitted.

        Parameters:
            record (logging.LogRecord): The record to test.

        Returns:
            bool: False for the timer-teardown race, True for everything else.
        """
        return self.MESSAGE not in record.getMessage()


def main(argv=None):
    args = parse_args(argv)
    configure_logging()

    # Imported here rather than at module scope so `import app` stays cheap for
    # tests and scripts that only want JobStore.
    from nicegui import ui

    import web.pages  # noqa: F401  (registers every route)
    from web.startup import register_background_tasks

    if args.headless:
        native = False
        show = False
    else:
        native = not args.browser and native_available()
        show = not native
        if not args.browser and not native:
            print(
                "pywebview is not installed, so this will open in your browser instead.\n"
                "For a native window: pip install pywebview"
            )

    if args.host != DEFAULT_HOST:
        # Worth being loud about. There is no login page: whoever can reach the
        # port can read every stored email body and the whole application
        # history.
        logging.getLogger(__name__).warning(
            "Binding %s instead of %s. This app has NO authentication - anyone "
            "who can reach this address can read your mailbox mirror. Prefer "
            "loopback plus an SSH tunnel or Tailscale.",
            args.host, DEFAULT_HOST,
        )

    if not args.no_poll:
        register_background_tasks()

    ui.run(
        title="Job Board Tracker",
        favicon="📋",
        port=args.port,
        native=native,
        reload=False,
        show=show,
        host=args.host,
        # No literal fallback here on purpose. NiceGUI already fails loudly -
        # a RuntimeError - the moment `app.storage.user` or `.browser` is
        # actually used without a secret configured, which is nothing today.
        # A hardcoded default would defeat that: a public, guessable signing
        # key sitting next to a --host flag, silently live the day someone
        # adds a storage call.
        storage_secret=os.environ.get("NICEGUI_STORAGE_SECRET"),
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
