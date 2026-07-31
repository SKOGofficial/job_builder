"""Email matches page: review suggested Gmail replies for open applications.

Every match is a suggestion. Confirming is the only path that writes a status,
so an incorrect heuristic match can never change a record on its own.

Each card collapses to its headers and expands to show the stored message text,
so the page stays scannable when many matches are pending.
"""

import tkinter as tk
from tkinter import ttk

from clients.gmail_client import MISSING_PACKAGES_HINT
from pages.base import BasePage
from utilities.theme import STATUSES

NO_BODY_HINT = (
    "No message text stored for this match. Matches recorded before message "
    "bodies were saved show nothing here; run Check for replies again to fetch it."
)

#: Text widget height bounds, in lines. Short replies stay compact, long ones
#: scroll internally instead of pushing the action buttons off screen.
MIN_BODY_LINES = 4
MAX_BODY_LINES = 18


class EmailMatchesPage(BasePage):
    name = "email_matches"
    title = "Email matches"
    subtitle = (
        "Suggested replies matched to open applications. Nothing is applied until you confirm it."
    )

    def __init__(self, app):
        super().__init__(app)
        #: Match ids currently showing their message. Page instances are reused,
        #: so what the user opened survives navigating away and back.
        self.expanded = set()

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

        ttk.Label(
            toolbar,
            text=f"{len(matches)} pending",
            style="Muted.TLabel",
        ).pack(side="left", padx=(14, 0))

        column = self.scroll_area()
        for match in matches:
            self.render_match_card(column, match)

    # Layout helpers --------------------------------------------------------

    def scroll_area(self):
        """Return a scrolling column that the match cards are packed into.

        Expanding a message makes the page taller than the window, so the cards
        need their own viewport; without it an expanded body below the fold
        would be unreachable.
        """
        outer = ttk.Frame(self.content, style="TFrame")
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, bg=self.theme["bg"], highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        column = ttk.Frame(canvas, style="TFrame")
        window = canvas.create_window((0, 0), window=column, anchor="nw")
        column.bind(
            "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))

        def on_wheel(event):
            # bind_all outlives this page, so drop the binding once the canvas is
            # gone or the user has navigated elsewhere.
            if not canvas.winfo_exists() or self.app.active_page != self.name:
                canvas.unbind_all("<MouseWheel>")
                return
            # Let a message body scroll itself when the pointer is over it.
            if isinstance(event.widget, tk.Text):
                return
            canvas.yview_scroll(-int(event.delta / 120), "units")

        canvas.bind_all("<MouseWheel>", on_wheel)
        return column

    def render_match_card(self, parent, match):
        match_id = match["id"]
        card = self.card(parent)
        card.pack(fill="x", pady=(0, 10))

        header = ttk.Frame(card, style="Surface.TFrame")
        header.pack(fill="x")
        toggle = ttk.Button(
            header,
            text="▼" if match_id in self.expanded else "▶",
            width=3,
            style="Range.TButton",
            command=lambda: self.toggle(match_id, toggle, body, actions),
        )
        toggle.pack(side="left", padx=(0, 10))
        title = ttk.Label(
            header,
            text=f"{match['position_title']} at {match['company'] or 'Unknown company'}",
            style="CardTitle.TLabel",
            cursor="hand2",
        )
        title.pack(side="left")
        title.bind("<Button-1>", lambda _e: self.toggle(match_id, toggle, body, actions))

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

        body = self.build_body(card, match)
        if match_id in self.expanded:
            body.pack(fill="both", expand=True, pady=(0, 12))

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
            command=lambda: self.confirm(match_id, status_var.get()),
        ).pack(side="left", padx=(4, 8))
        ttk.Button(actions, text="Dismiss", command=lambda: self.dismiss(match_id)).pack(
            side="left"
        )

    def build_body(self, card, match):
        """Build the collapsible message pane. Not packed until it is expanded."""
        frame = ttk.Frame(card, style="Surface.TFrame")
        body_text = (match["body_text"] or "").strip()
        if not body_text:
            ttk.Label(
                frame, text=NO_BODY_HINT, style="MutedSurface.TLabel", wraplength=760
            ).pack(anchor="w")
            return frame

        lines = body_text.count("\n") + 1
        height = max(MIN_BODY_LINES, min(lines, MAX_BODY_LINES))
        # tk.Text is not a ttk widget, so it takes theme colors directly.
        text = tk.Text(
            frame,
            wrap="word",
            height=height,
            bg=self.theme["surface_2"],
            fg=self.theme["text"],
            selectbackground=self.theme["primary"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=("Segoe UI", 10),
            padx=12,
            pady=10,
        )
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.insert("1.0", body_text)
        # Disabled rather than editable: the text stays selectable and copyable
        # but cannot be changed, since the stored copy is a record of the email.
        text.configure(state="disabled")
        text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return frame

    def toggle(self, match_id, button, body, actions):
        if match_id in self.expanded:
            self.expanded.discard(match_id)
            body.pack_forget()
            button.configure(text="▶")
        else:
            self.expanded.add(match_id)
            body.pack(fill="both", expand=True, pady=(0, 12), before=actions)
            button.configure(text="▼")

    # Actions ---------------------------------------------------------------

    def confirm(self, match_id, status):
        self.store.confirm_email_match(match_id, status)
        self.expanded.discard(match_id)
        self.show_page("email_matches")

    def dismiss(self, match_id):
        self.store.dismiss_email_match(match_id)
        self.expanded.discard(match_id)
        self.show_page("email_matches")
