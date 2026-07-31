"""Settings page: appearance and Gmail connection management."""

from tkinter import ttk

from clients.gmail_client import MISSING_PACKAGES_HINT
from pages.base import BasePage


class SettingsPage(BasePage):
    name = "settings"
    title = "Settings"

    def render(self):
        ttk.Label(self.content, text=self.title, style="Title.TLabel").pack(anchor="w")
        self.render_appearance_card()
        self.render_gmail_card()

    def render_appearance_card(self):
        card = self.card(self.content)
        card.pack(fill="x", pady=(16, 0))
        ttk.Label(card, text="Appearance", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(
            card,
            text=f"Current theme: {self.app.theme_name.title()}",
            style="MutedSurface.TLabel",
        ).pack(anchor="w", pady=(5, 12))
        ttk.Button(
            card, text="Toggle Dark / Light", style="Primary.TButton", command=self.app.toggle_theme
        ).pack(anchor="w")

    def render_gmail_card(self):
        gmail = self.app.gmail
        card = self.card(self.content)
        card.pack(fill="x", pady=(16, 0))
        ttk.Label(card, text="Gmail", style="CardTitle.TLabel").pack(anchor="w")

        if not gmail.available:
            ttk.Label(
                card, text=MISSING_PACKAGES_HINT, style="MutedSurface.TLabel", wraplength=620
            ).pack(anchor="w", pady=(5, 0))
            return

        connected = gmail.is_connected()
        ttk.Label(
            card,
            text=f"Status: {'Connected' if connected else 'Not connected'}",
            style="MutedSurface.TLabel",
        ).pack(anchor="w", pady=(5, 4))
        ttk.Label(
            card,
            text=(
                "Read-only access is used to spot replies about your applications. "
                "The app never sends, deletes, or changes mail. Headers decide what "
                "matches; the message text is then saved for matched mail only, so you "
                "can read it on the Email matches page. Sign-in happens in your browser."
            ),
            style="MutedSurface.TLabel",
            wraplength=620,
        ).pack(anchor="w", pady=(0, 12))

        buttons = ttk.Frame(card, style="Surface.TFrame")
        buttons.pack(anchor="w")
        if connected:
            ttk.Button(
                buttons, text="Check for replies", style="Primary.TButton", command=gmail.scan
            ).pack(side="left", padx=(0, 8))
            ttk.Button(buttons, text="Disconnect", command=gmail.disconnect).pack(side="left")
        else:
            ttk.Button(
                buttons, text="Connect Gmail", style="Primary.TButton", command=gmail.connect
            ).pack(side="left")
