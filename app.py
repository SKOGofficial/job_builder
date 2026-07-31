"""Job Board Tracker: application shell and entry point.

This module owns the window, the theme, navigation, and the shared widgets that
pages draw into. Each page lives in its own module under `pages/`, the database
lives in `store.py`, and each external service gets its own manager module
(`gmail_client.py`, `llm_client.py`).

Run it with:

    python app.py
"""

import tkinter as tk
from tkinter import ttk

import clients.gmail_client as _gmail_client_mod
import clients.llm_client as _llm_client_mod
from clients.gmail_client import GMAIL_AVAILABLE, GMAIL_IMPORT_ERROR, GmailWorkflow
from clients.llm_client import GROQ_AVAILABLE, GROQ_IMPORT_ERROR, ClassificationRunner

gmail_client = _gmail_client_mod if GMAIL_AVAILABLE else None
llm_client = _llm_client_mod if GROQ_AVAILABLE else None
from pages import DRAWER_ENTRIES, NAV_TABS, PAGE_CLASSES
from utilities.store import DB_PATH, JobStore, normalize_url, today_iso, url_hash
from utilities.theme import (
    JOB_TYPES,
    PAY_PERIODS,
    STATUS_COLORS,
    STATUSES,
    THEMES,
    TIME_RANGES,
    apply_styles,
)

# Re-exported so `import app` remains a single convenient entry point for tests
# and scripts even though the implementations now live in dedicated modules.
__all__ = [
    "JobTrackerApp",
    "JobStore",
    "DB_PATH",
    "normalize_url",
    "url_hash",
    "today_iso",
    "THEMES",
    "STATUS_COLORS",
    "TIME_RANGES",
    "JOB_TYPES",
    "STATUSES",
    "PAY_PERIODS",
    "GMAIL_AVAILABLE",
    "GMAIL_IMPORT_ERROR",
    "gmail_client",
    "GROQ_AVAILABLE",
    "GROQ_IMPORT_ERROR",
    "llm_client",
]

DEFAULT_PAGE = "add"


class JobTrackerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Job Board Tracker")
        self.geometry("1180x760")
        self.minsize(980, 640)

        self.store = JobStore()
        self.theme_name = self.store.get_profile_value("theme", "light")
        self.theme = THEMES[self.theme_name]
        self.active_page = DEFAULT_PAGE
        self.drawer_visible = False

        self.gmail = GmailWorkflow(self)
        # Pages are built once and reused, so per-page state such as the
        # dashboard time range survives navigation.
        self.pages = {cls.name: cls(self) for cls in PAGE_CLASSES}
        # Owns the classification cycle. It lives on the app rather than the
        # page so a run in progress survives the user navigating away.
        self.classifier = ClassificationRunner(self)

        self.configure(bg=self.theme["bg"])
        apply_styles(self, self.theme, self.theme_name)
        self.build_shell()
        self.show_page(DEFAULT_PAGE)

    # Shell -----------------------------------------------------------------

    def build_shell(self):
        self.topbar = ttk.Frame(self, padding=(18, 14), style="TFrame")
        self.topbar.pack(fill="x")
        ttk.Button(self.topbar, text="☰", width=3, command=self.toggle_drawer).pack(side="left")
        ttk.Label(self.topbar, text="Job Board Tracker", style="Title.TLabel").pack(
            side="left", padx=(16, 0)
        )
        ttk.Label(
            self.topbar, text="SQLite-backed application history", style="Muted.TLabel"
        ).pack(side="left", padx=(14, 0), pady=(7, 0))
        ttk.Button(self.topbar, text="Dark / Light", command=self.toggle_theme).pack(side="right")

        self.body = ttk.Frame(self, style="TFrame")
        self.body.pack(fill="both", expand=True)

        self.drawer = ttk.Frame(self.body, padding=(16, 18), style="Surface.TFrame")
        self.main = ttk.Frame(self.body, padding=(22, 10, 22, 24), style="TFrame")
        self.main.pack(side="left", fill="both", expand=True)

        self.nav = ttk.Frame(self.main, style="TFrame")
        self.nav.pack(fill="x", pady=(0, 18))
        self.nav_buttons = {}
        for index, (label, page) in enumerate(NAV_TABS):
            button_style = "ActiveTab.TButton" if page == self.active_page else "Tab.TButton"
            self.nav_buttons[page] = ttk.Button(
                self.nav,
                text=label,
                style=button_style,
                command=lambda p=page: self.show_page(p),
            )
            self.nav_buttons[page].pack(side="left", padx=(0 if index == 0 else 6, 0))

        self.content = ttk.Frame(self.main, style="TFrame")
        self.content.pack(fill="both", expand=True)

    def toggle_drawer(self):
        self.drawer_visible = not self.drawer_visible
        if self.drawer_visible:
            self.drawer.pack(side="left", fill="y", before=self.main)
            self.render_drawer()
        else:
            self.drawer.pack_forget()

    def render_drawer(self):
        self.clear(self.drawer)
        ttk.Label(self.drawer, text="Menu", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 14))
        for label, page in DRAWER_ENTRIES:
            ttk.Button(self.drawer, text=label, command=lambda p=page: self.show_page(p)).pack(
                fill="x", pady=5
            )
        ttk.Label(
            self.drawer,
            text="These sections store local profile context for future automation features.",
            style="MutedSurface.TLabel",
            wraplength=190,
        ).pack(anchor="w", pady=(22, 0))

    def toggle_theme(self):
        self.theme_name = "dark" if self.theme_name == "light" else "light"
        self.theme = THEMES[self.theme_name]
        self.store.save_profile_value("theme", self.theme_name)
        for child in self.winfo_children():
            child.destroy()
        self.configure(bg=self.theme["bg"])
        apply_styles(self, self.theme, self.theme_name)
        self.build_shell()
        if self.drawer_visible:
            self.drawer_visible = False
            self.toggle_drawer()
        self.show_page(self.active_page)

    # Navigation ------------------------------------------------------------

    def show_page(self, page):
        if page not in self.pages:
            raise KeyError(f"Unknown page: {page}")
        self.active_page = page
        self.update_nav_tabs()
        self.clear(self.content)
        self.content.update_idletasks()
        self.pages[page].render()

    def update_nav_tabs(self):
        for page, button in getattr(self, "nav_buttons", {}).items():
            button.configure(
                style="ActiveTab.TButton" if page == self.active_page else "Tab.TButton"
            )

    # Shared widget helpers -------------------------------------------------

    def clear(self, frame):
        for child in frame.winfo_children():
            child.destroy()

    def card(self, parent, padding=(22, 20)):
        frame = ttk.Frame(parent, padding=padding, style="Surface.TFrame")
        frame.configure(borderwidth=1, relief="solid")
        return frame


def main():
    JobTrackerApp().mainloop()


if __name__ == "__main__":
    main()
