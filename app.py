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


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Job Board Tracker")
    parser.add_argument(
        "--browser",
        action="store_true",
        help="open in a normal browser tab instead of a native window",
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


def main(argv=None):
    args = parse_args(argv)
    # Imported here rather than at module scope so `import app` stays cheap for
    # tests and scripts that only want JobStore.
    from nicegui import ui

    import web.pages  # noqa: F401  (registers every route)

    native = not args.browser and native_available()
    if not args.browser and not native:
        print(
            "pywebview is not installed, so this will open in your browser instead.\n"
            "For a native window: pip install pywebview"
        )

    ui.run(
        title="Job Board Tracker",
        favicon="📋",
        port=args.port,
        native=native,
        reload=False,
        show=not native,
        # Local-only tool: binding to loopback keeps it off the network.
        host="127.0.0.1",
        storage_secret=os.environ.get("NICEGUI_STORAGE_SECRET", "job-board-tracker-local"),
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
