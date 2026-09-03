"""Page chrome: header, navigation, dark mode, and the shared card helper.

Every page opens with `page_shell(...)`, which draws the header and drawer and
then hands back a container for the page body.
"""

import logging
from contextlib import contextmanager

from nicegui import context, ui

from utilities.theme import PRIMARY_COLOR
from web.state import get_state

log = logging.getLogger(__name__)

#: Quasar buttons default to the primary colour, which is the same blue the
#: header is painted with. Left alone, the nav reads as blue-on-blue and is
#: effectively invisible, so everything on the header states white explicitly.
HEADER_BUTTON = "flat dense no-caps color=white"
#: The current page is a white pill with the header colour showing through.
HEADER_BUTTON_ACTIVE = "unelevated dense no-caps color=white text-color=primary"
HEADER_ICON = "flat round dense color=white"

#: Top tabs. The two lists the app is built around - what you have applied to
#: and what you have not - both live here rather than behind the drawer.
NAV_TABS = [
    ("All jobs", "/"),
    ("To apply", "/leads"),
    ("Add application", "/add"),
    ("Dashboard", "/dashboard"),
]

#: Everything else, reached from the drawer.
DRAWER_ENTRIES = [
    ("Referrals", "/referrals", "handshake"),
    ("Email matches", "/email-matches", "mark_email_unread"),
    ("Experiences", "/experiences", "format_list_bulleted"),
    ("Diagnostics", "/diagnostics", "monitor_heart"),
    ("Settings", "/settings", "settings"),
    ("Profile", "/profile", "person"),
    ("Resume notes", "/resume", "description"),
]


def card(padding="p-6"):
    """A surface panel. Cards carry the app's only border and shadow.

    Summary:
        Build the standard bordered, shadowed card container pages use.

    Parameters:
        padding (str): Tailwind padding class. Defaults to "p-6".

    Returns:
        ui.card: The card element, styled and ready to be used as a context
            manager.
    """
    return ui.card().classes(f"w-full {padding} gap-2 shadow-sm").props("flat bordered")


def page_timer(interval, callback):
    """A repeating timer that stops when its client goes away.

    A plain ui.timer keeps firing after the user navigates off the page, doing
    work for a page nobody is looking at. Cancelling on disconnect stops that.

    It does *not* stop the "parent slot of Timer has been deleted" traceback,
    which this docstring used to claim. `Timer._run_in_loop` evaluates its
    context before consulting `_should_stop()`, so the element is already gone
    by the time the cancel flag is read. That noise is filtered in `app.py`
    instead - see `_TimerTeardownNoise`.

    Summary:
        Create a `ui.timer` that cancels itself when its client disconnects.

    Parameters:
        interval (float): Seconds between callback invocations.
        callback (Callable): Zero-argument function invoked each tick.

    Returns:
        ui.timer: The created timer, already wired to cancel on disconnect.

    Note:
        `on_disconnect` passes the client as an argument, but `timer.cancel()`
        takes none - the lambda wrapper is required, not stylistic.
    """
    timer = ui.timer(interval, callback)
    # on_disconnect passes client as argument; timer.cancel() takes no args
    context.client.on_disconnect(lambda _: timer.cancel())
    return timer


def pending_counts(state):
    """Badge numbers for the drawer: work actually waiting on the user.

    Never raises. A count is decoration, and a page that fails to render
    because a badge query blew up would be a poor trade.

    Summary:
        Compute the drawer badge counts for email matches and referrals.

    Parameters:
        state (AppState): The shared app state to query.

    Returns:
        dict[str, int]: Route path to count, for whichever queries succeeded.
            A route is absent from the dict rather than present at 0 if its
            query failed. Counts `/email-matches` (replies waiting on a
            decision) and `/referrals` (postings at a contact's company that
            arrived since it was last checked).

    Note:
        Every query is wrapped so a failure here cannot break page rendering;
        failures are logged at debug level and silently drop that badge.

        The review queue's badge used to sit here. It counted job mail the
        resolver could not attach to an application, and in practice it counted
        job-board digests - 265 of its 312 entries - each asking the user which
        of their applications a list of ten unrelated roles belonged to. That
        question has no answer, so the classifier now labels those alerts up
        front and the alert handler turns them into leads.
    """
    counts = {}
    try:
        counts["/email-matches"] = len(state.store.pending_email_matches())
    except Exception:
        log.debug("Pending match count failed", exc_info=True)
    try:
        # Imported here rather than at module scope: `web/shell.py` is imported
        # by every page, and the pipeline package should not be pulled in just
        # to draw a header.
        from pipeline.referrals import new_match_count

        counts["/referrals"] = new_match_count(state.mail)
    except Exception:
        log.debug("Referral match count failed", exc_info=True)
    return counts


@contextmanager
def page_shell(title, subtitle="", active=""):
    """
    Summary:
        Draw the header, navigation, and drawer, then yield a container for
        the page body.

    Parameters:
        title (str): Page heading shown above the body.
        subtitle (str): Optional line under the heading. Omitted from layout
            when empty.
        active (str): The route of the current page, used to highlight its
            nav tab or drawer entry.

    Yields:
        ui.column: The body container every page renders its content into.

    Note:
        Every page goes through this, so a failure here breaks the whole app
        rather than one page.
    """
    state = get_state()
    ui.colors(primary=PRIMARY_COLOR)
    dark = ui.dark_mode(value=state.dark)

    def toggle_dark():
        """
        Summary:
            Flip dark mode and persist the new choice.
        """
        dark.toggle()
        state.save_dark(bool(dark.value))

    with ui.header().classes("items-center justify-between px-6 py-3"):
        with ui.row().classes("items-center gap-3"):
            ui.button(icon="menu", on_click=lambda: drawer.toggle()).props(
                HEADER_ICON
            ).tooltip("Toggle menu")
            ui.label("Job Board Tracker").classes("text-lg font-semibold")
            ui.label("SQLite-backed application history").classes(
                "text-xs opacity-70 hidden sm:block"
            )
        with ui.row().classes("items-center gap-1"):
            for label, target in NAV_TABS:
                ui.button(label, on_click=lambda t=target: ui.navigate.to(t)).props(
                    HEADER_BUTTON if target != active else HEADER_BUTTON_ACTIVE
                )
            ui.button(icon="dark_mode", on_click=toggle_dark).props(HEADER_ICON).tooltip(
                "Toggle dark mode"
            )

    # Open by default: these four pages are only reachable from here, and a
    # closed drawer behind an unlabelled icon makes them look unimplemented.
    # Quasar collapses it to an overlay on narrow screens on its own.
    with ui.left_drawer(value=True).classes("p-4 gap-2").props("bordered") as drawer:
        ui.label("Menu").classes("text-base font-semibold mb-2")
        counts = pending_counts(state)
        for label, target, icon in DRAWER_ENTRIES:
            button = ui.button(
                label, icon=icon, on_click=lambda t=target: ui.navigate.to(t)
            ).props(
                "flat align=left no-caps"
                + (" color=primary" if target == active else " color=inherit")
            ).classes("w-full")
            waiting = counts.get(target)
            if waiting:
                with button:
                    ui.badge(str(waiting)).props("color=red floating")
        ui.separator().classes("my-2")
        ui.label(
            "These sections store local profile context and the Gmail and AI automations."
        ).classes("text-xs opacity-60")

    with ui.column().classes("w-full max-w-6xl mx-auto p-6 gap-4"):
        ui.label(title).classes("text-3xl font-semibold")
        if subtitle:
            ui.label(subtitle).classes("text-sm opacity-70 -mt-2")
        with ui.column().classes("w-full gap-4") as body:
            yield body
