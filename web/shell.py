"""Page chrome: header, navigation, dark mode, and the shared card helper.

Every page opens with `page_shell(...)`, which draws the header and drawer and
then hands back a container for the page body.
"""

from contextlib import contextmanager

from nicegui import context, ui

from utilities.theme import PRIMARY_COLOR
from web.state import get_state

#: Quasar buttons default to the primary colour, which is the same blue the
#: header is painted with. Left alone, the nav reads as blue-on-blue and is
#: effectively invisible, so everything on the header states white explicitly.
HEADER_BUTTON = "flat dense no-caps color=white"
#: The current page is a white pill with the header colour showing through.
HEADER_BUTTON_ACTIVE = "unelevated dense no-caps color=white text-color=primary"
HEADER_ICON = "flat round dense color=white"

#: Top tabs, mirroring the ones the Tkinter shell used.
NAV_TABS = [
    ("All jobs", "/"),
    ("Add application", "/add"),
    ("Dashboard", "/dashboard"),
]

#: Everything else, reached from the drawer.
DRAWER_ENTRIES = [
    ("Email matches", "/email-matches", "mark_email_unread"),
    ("Settings", "/settings", "settings"),
    ("Profile", "/profile", "person"),
    ("Resume & Experiences", "/resume", "description"),
]


def card(padding="p-6"):
    """A surface panel. Cards carry the app's only border and shadow."""
    return ui.card().classes(f"w-full {padding} gap-2 shadow-sm").props("flat bordered")


def page_timer(interval, callback):
    """A repeating timer that stops when its client goes away.

    A plain ui.timer keeps firing after the user navigates off the page, and
    NiceGUI then raises "The parent slot of Timer has been deleted" on every
    tick. Cancelling on disconnect keeps the log clean and stops the work.
    """
    timer = ui.timer(interval, callback)
    # on_disconnect passes client as argument; timer.cancel() takes no args
    context.client.on_disconnect(lambda _: timer.cancel())
    return timer


@contextmanager
def page_shell(title, subtitle="", active=""):
    state = get_state()
    ui.colors(primary=PRIMARY_COLOR)
    dark = ui.dark_mode(value=state.dark)

    def toggle_dark():
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
        for label, target, icon in DRAWER_ENTRIES:
            ui.button(label, icon=icon, on_click=lambda t=target: ui.navigate.to(t)).props(
                "flat align=left no-caps"
                + (" color=primary" if target == active else " color=inherit")
            ).classes("w-full")
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
