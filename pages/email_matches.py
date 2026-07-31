"""Email matches page: review suggested Gmail replies for open applications.

Every match is a suggestion. Confirming is the only path that writes a status,
so an incorrect heuristic match can never change a record on its own.

Each card collapses to its headers and expands to show the stored message text,
so the page stays scannable when many matches are pending.
"""

import tkinter as tk
from tkinter import ttk

from clients import llm_client
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
        #: Progress widgets, refreshed in place while a cycle runs so the page
        #: is not rebuilt on every classified message.
        self.progress_label = None
        self.progress_bar = None

    def render(self):
        self.render_heading()
        gmail = self.app.gmail
        self.progress_label = None
        self.progress_bar = None

        if not gmail.available:
            card = self.card(self.content)
            card.pack(fill="x")
            ttk.Label(
                card, text=MISSING_PACKAGES_HINT, style="MutedSurface.TLabel", wraplength=620
            ).pack(anchor="w")
            return

        matches = self.store.pending_email_matches()

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
        if matches:
            ttk.Label(toolbar, text=f"{len(matches)} pending", style="Muted.TLabel").pack(
                side="left", padx=(14, 0)
            )

        self.render_classifier_card()

        if not matches:
            card = self.card(self.content)
            card.pack(fill="x")
            ttk.Label(
                card,
                text="No pending matches. Use Check for replies to scan your inbox.",
                style="MutedSurface.TLabel",
            ).pack(anchor="w")
            return

        column = self.scroll_area()
        for match in matches:
            self.render_match_card(column, match)

    # AI classification -----------------------------------------------------

    def render_classifier_card(self):
        """Progress, state, and the controls for the classification cycle."""
        runner = self.app.classifier
        if not runner.available:
            return

        card = self.card(self.content, padding=(18, 14))
        card.pack(fill="x", pady=(0, 12))

        row = ttk.Frame(card, style="Surface.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="AI classification", style="CardTitle.TLabel").pack(side="left")

        if not runner.is_configured():
            ttk.Label(
                card,
                text=(
                    "No Groq API key found. Add GROQ_API_KEY to .env, or store it in "
                    "Credential Manager from Settings."
                ),
                style="MutedSurface.TLabel",
                wraplength=760,
            ).pack(anchor="w", pady=(6, 0))
            ttk.Button(
                card, text="Open Settings", command=lambda: self.show_page("settings")
            ).pack(anchor="w", pady=(10, 0))
            return

        if runner.state == llm_client.RUNNING:
            ttk.Button(row, text="Stop", command=runner.stop).pack(side="right")
        elif runner.state in (llm_client.RATE_LIMITED, llm_client.STOPPED, llm_client.ERROR):
            ttk.Button(
                row,
                text="Resume classification",
                style="Primary.TButton",
                command=runner.resume,
            ).pack(side="right")
        else:
            waiting = runner.pending_count()
            ttk.Button(
                row,
                text=f"Classify {waiting} message(s)" if waiting else "Classify with AI",
                style="Primary.TButton" if waiting else "TButton",
                command=runner.start,
            ).pack(side="right")

        self.progress_label = ttk.Label(
            card,
            text=runner.progress_text() or "Idle.",
            style="MutedSurface.TLabel",
            wraplength=760,
        )
        self.progress_label.pack(anchor="w", pady=(8, 0))

        if runner.state == llm_client.RUNNING:
            self.progress_bar = ttk.Progressbar(
                card,
                orient="horizontal",
                mode="determinate",
                maximum=max(runner.total, 1),
                value=runner.processed,
                style="Horizontal.TProgressbar",
            )
            self.progress_bar.pack(fill="x", pady=(8, 0))
        elif runner.state == llm_client.RATE_LIMITED:
            # A filled warning-coloured bar shows how far the cycle got before
            # Groq cut it off, so Resume has visible context.
            self.progress_bar = ttk.Progressbar(
                card,
                orient="horizontal",
                mode="determinate",
                maximum=max(runner.total, 1),
                value=runner.processed,
                style="Paused.Horizontal.TProgressbar",
            )
            self.progress_bar.pack(fill="x", pady=(8, 0))

    def on_classification_update(self, final=False):
        """Called by the runner on the main thread as the cycle progresses.

        Mid-cycle updates only touch the two progress widgets. A full redraw is
        reserved for terminal states, where the buttons and the cards' AI badges
        both change.
        """
        if self.app.active_page != self.name:
            return
        live = (
            not final
            and self.progress_label is not None
            and self.progress_label.winfo_exists()
        )
        if not live:
            self.show_page(self.name)
            return
        runner = self.app.classifier
        self.progress_label.configure(text=runner.progress_text())
        if self.progress_bar is not None and self.progress_bar.winfo_exists():
            self.progress_bar.configure(value=runner.processed)

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

        self.render_ai_badge(card, match)
        body = self.build_body(card, match)
        if match_id in self.expanded:
            body.pack(fill="both", expand=True, pady=(0, 12))

        actions = ttk.Frame(card, style="Surface.TFrame")
        actions.pack(fill="x")
        ttk.Label(actions, text="Set status to", style="Surface.TLabel").pack(side="left")
        # The model's label pre-fills the dropdown when it maps to a real status.
        # Acknowledgement and Unclear deliberately do not.
        suggested = match["ai_status"] if match["ai_status"] in STATUSES else "Interview"
        status_var = tk.StringVar(value=suggested)
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

    def render_ai_badge(self, card, match):
        """Show what the model concluded, and whether it acted on it."""
        label = match["ai_status"]
        if not label:
            return

        row = ttk.Frame(card, style="Surface.TFrame")
        row.pack(fill="x", pady=(0, 6))
        ttk.Label(
            row,
            text=f"AI: {label} · {(match['ai_confidence'] or 0.0):.0%}",
            style="Badge.TLabel",
        ).pack(side="left")

        if match["ai_applied"]:
            previous = match["ai_previous_status"] or "unset"
            ttk.Label(
                row,
                text=f"Applied automatically, replacing {previous}.",
                style="MutedSurface.TLabel",
            ).pack(side="left", padx=(10, 0))
            ttk.Button(row, text="Undo", command=lambda: self.undo(match["id"])).pack(
                side="left", padx=(10, 0)
            )

        if match["ai_reason"]:
            ttk.Label(
                card,
                text=match["ai_reason"],
                style="MutedSurface.TLabel",
                wraplength=760,
            ).pack(anchor="w", pady=(0, 8))

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

    def undo(self, match_id):
        """Revert a status the classifier applied on its own."""
        self.store.undo_ai_status(match_id)
        self.show_page("email_matches")
