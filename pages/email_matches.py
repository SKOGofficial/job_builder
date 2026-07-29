"""Email matches page: review suggested Gmail replies for open applications.

Every match is a suggestion. Confirming is the only path that writes a status,
so an incorrect heuristic match can never change a record on its own.
"""

import tkinter as tk
from tkinter import ttk

from clients.gmail_client import MISSING_PACKAGES_HINT
from pages.base import BasePage
from utilities.theme import STATUSES


class EmailMatchesPage(BasePage):
    name = "email_matches"
    title = "Email matches"
    subtitle = (
        "Suggested replies matched to open applications. Nothing is applied until you confirm it."
    )

    def render(self):
        self.render_heading()
        gmail = self.app.gmail

        if not gmail.available:
            card = self.card(self.content)
            card.pack(fill="x")
            ttk.Label(
                card, text=MISSING_PACKAGES_HINT, style="MutedSurface.TLabel", wraplength=620
            ).pack(anchor="w")
            return

        toolbar = ttk.Frame(self.content, style="TFrame")
        toolbar.pack(fill="x", pady=(0, 12))
        if gmail.is_connected():
            ttk.Button(
                toolbar, text="Check for replies", style="Primary.TButton", command=gmail.scan
            ).pack(side="left")
        else:
            ttk.Button(
                toolbar,
                text="Connect Gmail in Settings",
                command=lambda: self.show_page("settings"),
            ).pack(side="left")

        matches = self.store.pending_email_matches()
        if not matches:
            card = self.card(self.content)
            card.pack(fill="x")
            ttk.Label(
                card,
                text="No pending matches. Use Check for replies to scan your inbox.",
                style="MutedSurface.TLabel",
            ).pack(anchor="w")
            return

        for match in matches:
            self.render_match_card(match)

    def render_match_card(self, match):
        card = self.card(self.content)
        card.pack(fill="x", pady=(0, 10))
        ttk.Label(
            card,
            text=f"{match['position_title']} at {match['company'] or 'Unknown company'}",
            style="CardTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            card, text=f"From: {match['sender']}", style="MutedSurface.TLabel", wraplength=760
        ).pack(anchor="w", pady=(6, 0))
        ttk.Label(
            card, text=f"Subject: {match['subject']}", style="Surface.TLabel", wraplength=760
        ).pack(anchor="w", pady=(2, 0))
        ttk.Label(
            card,
            text=f"Received: {match['received_date']}  |  Current status: {match['job_status']}",
            style="MutedSurface.TLabel",
        ).pack(anchor="w", pady=(2, 10))

        actions = ttk.Frame(card, style="Surface.TFrame")
        actions.pack(fill="x")
        ttk.Label(actions, text="Set status to", style="Surface.TLabel").pack(side="left")
        status_var = tk.StringVar(value="Interview")
        ttk.Combobox(
            actions, textvariable=status_var, values=STATUSES, state="readonly", width=16
        ).pack(side="left", padx=8)
        ttk.Button(
            actions,
            text="Confirm",
            style="Primary.TButton",
            command=lambda: self.confirm(match["id"], status_var.get()),
        ).pack(side="left", padx=(4, 8))
        ttk.Button(actions, text="Dismiss", command=lambda: self.dismiss(match["id"])).pack(
            side="left"
        )

    def confirm(self, match_id, status):
        self.store.confirm_email_match(match_id, status)
        self.show_page("email_matches")

    def dismiss(self, match_id):
        self.store.dismiss_email_match(match_id)
        self.show_page("email_matches")
