"""Settings page: appearance, Gmail connection, and Groq classification."""

from tkinter import messagebox, ttk

from clients import llm_client
from clients.gmail_client import MISSING_PACKAGES_HINT
from pages.base import BasePage


class SettingsPage(BasePage):
    name = "settings"
    title = "Settings"

    def render(self):
        ttk.Label(self.content, text=self.title, style="Title.TLabel").pack(anchor="w")
        self.render_appearance_card()
        self.render_gmail_card()
        self.render_groq_card()

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

    # Groq --------------------------------------------------------------------

    def render_groq_card(self):
        runner = self.app.classifier
        card = self.card(self.content)
        card.pack(fill="x", pady=(16, 0))
        ttk.Label(card, text="AI classification (Groq)", style="CardTitle.TLabel").pack(
            anchor="w"
        )

        if not runner.available:
            ttk.Label(
                card,
                text=llm_client.MISSING_PACKAGES_HINT,
                style="MutedSurface.TLabel",
                wraplength=620,
            ).pack(anchor="w", pady=(5, 0))
            return

        in_keyring = bool(llm_client.stored_api_key())
        configured = runner.is_configured()
        if in_keyring:
            source = "stored in Windows Credential Manager"
        elif configured:
            source = "read from .env"
        else:
            source = "not set"
        ttk.Label(
            card, text=f"API key: {source}", style="MutedSurface.TLabel"
        ).pack(anchor="w", pady=(5, 2))

        if configured:
            ttk.Label(
                card,
                text=(
                    f"Model: {llm_client.model_name()}    "
                    f"Pace: {llm_client.requests_per_minute()} requests/min    "
                    f"Auto-apply at: {llm_client.confidence_threshold():.0%} confidence"
                ),
                style="MutedSurface.TLabel",
            ).pack(anchor="w", pady=(0, 4))

        ttk.Label(
            card,
            text=(
                "Matched replies are labelled as a rejection, offer, interview, online "
                "assessment, acknowledgement, or unclear. A label at or above the confidence "
                "threshold applies the job status automatically and can be undone from the "
                "Email matches page, which records the status it replaced. Anything below the "
                "threshold only pre-fills the dropdown. Requests are paced to stay under the "
                "free tier's limits, and a rate limit pauses the cycle rather than retrying."
            ),
            style="MutedSurface.TLabel",
            wraplength=620,
        ).pack(anchor="w", pady=(0, 12))

        buttons = ttk.Frame(card, style="Surface.TFrame")
        buttons.pack(anchor="w")
        if configured:
            ttk.Button(
                buttons,
                text="Test connection",
                style="Primary.TButton",
                command=self.test_groq,
            ).pack(side="left", padx=(0, 8))
        if configured and not in_keyring:
            ttk.Button(
                buttons, text="Move key to Credential Manager", command=self.move_groq_key
            ).pack(side="left", padx=(0, 8))
        if in_keyring:
            ttk.Button(buttons, text="Forget stored key", command=self.forget_groq_key).pack(
                side="left", padx=(0, 8)
            )
        if runner.state == llm_client.RATE_LIMITED:
            ttk.Button(buttons, text="Resume classification", command=runner.resume).pack(
                side="left"
            )

    def test_groq(self):
        """Classify one synthetic message so a misconfiguration shows up here."""
        try:
            client = llm_client.GroqClient.from_config()
            result = client.classify(
                {
                    "sender": "careers@example.com",
                    "subject": "Thank you for applying",
                    "body": "We received your application and will be in touch soon.",
                    "company": "Example",
                    "position_title": "Engineer",
                }
            )
        except llm_client.GroqRateLimited as exc:
            messagebox.showwarning(
                "Rate limited",
                f"Groq is rate limiting requests. Try again in about {exc.retry_after}s.",
            )
            return
        except Exception as exc:
            messagebox.showerror("Groq test failed", str(exc))
            return
        messagebox.showinfo(
            "Groq is working",
            f"Test message classified as {result['label']} "
            f"({result['confidence']:.0%}).\n\n{result['reason']}",
        )

    def move_groq_key(self):
        try:
            llm_client.save_api_key(llm_client.api_key())
        except Exception as exc:
            messagebox.showerror("Could not store the key", str(exc))
            return
        messagebox.showinfo(
            "Key stored",
            "The Groq key is now in Windows Credential Manager, which takes precedence "
            "over .env. You can remove GROQ_API_KEY from .env when you are ready; this "
            "app does not edit that file for you.",
        )
        self.show_page("settings")

    def forget_groq_key(self):
        if not messagebox.askyesno(
            "Forget stored key",
            "Remove the Groq key from Windows Credential Manager? If GROQ_API_KEY is still "
            "set in .env, that value will be used instead.",
        ):
            return
        llm_client.forget_api_key()
        messagebox.showinfo("Key removed", "The stored Groq key was deleted.")
        self.show_page("settings")
