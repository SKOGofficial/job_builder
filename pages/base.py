"""Shared base class for every page.

A page owns the widgets inside the app's content frame and nothing outside it.
Navigation, theming, and the shell stay with the app so pages never reach past
their own area.

Page instances are created once and reused, so a page may keep state (a form
variable, a selected range) across renders.
"""

from tkinter import ttk


class BasePage:
    #: Key used by the app registry and by show_page().
    name = ""
    #: Heading rendered at the top of the page.
    title = ""
    #: Optional sentence shown under the heading.
    subtitle = ""

    def __init__(self, app):
        self.app = app

    # Convenience passthroughs so pages read cleanly ------------------------

    @property
    def store(self):
        return self.app.store

    @property
    def theme(self):
        return self.app.theme

    @property
    def content(self):
        return self.app.content

    def card(self, parent, padding=(22, 20)):
        return self.app.card(parent, padding)

    def show_page(self, name):
        self.app.show_page(name)

    def render_heading(self):
        ttk.Label(self.content, text=self.title, style="Title.TLabel").pack(anchor="w")
        if self.subtitle:
            ttk.Label(self.content, text=self.subtitle, style="Muted.TLabel").pack(
                anchor="w", pady=(4, 16)
            )

    def render(self):
        raise NotImplementedError
