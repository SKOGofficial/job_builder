"""Page chrome: header, navigation, dark mode, and the shared card helper.

Every page opens with `page_shell(...)`, which draws the header and drawer and
then hands back a container for the page body.
"""

from contextlib import contextmanager

from nicegui import ui

from web.state import get_state

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


@contextmanager
def page_shell(title, subtitle="", active=""):
    state = get_state()
    dark = ui.dark_mode(value=state.dark)

    def toggle_dark():
        dark.toggle()
        state.save_dark(bool(dark.value))

    with ui.header().classes("items-center justify-between px-6 py-3"):
        with ui.row().classes("items-center gap-3"):
            ui.button(icon="menu", on_click=lambda: drawer.toggle()).props("flat round dense")
            ui.label("Job Board Tracker").classes("text-lg font-semibold")
            ui.label("SQLite-backed application history").classes("text-xs opacity-70")
        with ui.row().classes("items-center gap-1"):
            for label, target in NAV_TABS:
                button = ui.button(label, on_click=lambda t=target: ui.navigate.to(t))
                button.props("flat dense no-caps" if target != active else "unelevated dense no-caps")
            ui.button(icon="dark_mode", on_click=toggle_dark).props("flat round dense").tooltip(
                "Toggle dark mode"
            )

    with ui.left_drawer(value=False).classes("p-4 gap-2") as drawer:
        ui.label("Menu").classes("text-base font-semibold mb-2")
        for label, target, icon in DRAWER_ENTRIES:
            ui.button(label, icon=icon, on_click=lambda t=target: ui.navigate.to(t)).props(
                "flat align=left no-caps"
            ).classes("w-full")
        ui.separator().classes("my-2")
        ui.label(
            "These sections store local profile context for future automation features."
        ).classes("text-xs opacity-60")

    with ui.column().classes("w-full max-w-6xl mx-auto p-6 gap-4"):
        ui.label(title).classes("text-3xl font-semibold")
        if subtitle:
            ui.label(subtitle).classes("text-sm opacity-70 -mt-2")
        with ui.column().classes("w-full gap-4") as body:
            yield body
