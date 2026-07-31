"""Settings: appearance, Gmail connection, and Groq classification.

Both automations can be driven from here as well as from the Email matches
page, because this is where you land after connecting Gmail or adding a key and
the natural next step is to run the thing you just configured.

The cards are defined inside the page function rather than at module level so
each client gets its own refreshable targets.
"""

from nicegui import run, ui

from clients import gmail_client, llm_client
from utilities import credentials
from web.shell import card, page_shell, page_timer
from web.state import get_state


@ui.page("/settings")
def settings_page():
    state = get_state()
    scanner, classifier = state.scanner, state.classifier
    seen = {"scanner": scanner.state, "classifier": classifier.state}

    with page_shell("Settings", active=""):

        # Appearance ---------------------------------------------------------

        with card():
            ui.label("Appearance").classes("text-base font-semibold")
            dark = ui.dark_mode(value=state.dark)

            def set_dark(event):
                dark.value = event.value
                state.save_dark(event.value)

            ui.switch("Dark mode", value=state.dark, on_change=set_dark)
            ui.label(
                "The choice is stored locally and applies the next time you open the app."
            ).classes("text-sm opacity-70")

        # Automation handlers -------------------------------------------------

        async def scan_now():
            found = await scanner.scan()
            refresh_all()
            try:
                ui.notify(
                    scanner.message,
                    type="negative" if scanner.state == gmail_client.ERROR else "positive",
                    multi_line=True,
                    close_button=True,
                )
            except RuntimeError:
                pass
            if found and classifier.available and classifier.is_configured():
                await classifier.run()
                refresh_all()

        async def classify_now():
            await classifier.run()
            refresh_all()
            try:
                ui.notify(classifier.message, multi_line=True, close_button=True)
            except RuntimeError:
                pass

        # Gmail ---------------------------------------------------------------

        @ui.refreshable
        def gmail_card():
            with card():
                ui.label("Gmail").classes("text-base font-semibold")
                if not scanner.available:
                    ui.label(gmail_client.MISSING_PACKAGES_HINT).classes("text-sm opacity-70")
                    return

                connected = scanner.is_connected()
                ui.label(f"Status: {'Connected' if connected else 'Not connected'}").classes(
                    "text-sm opacity-70"
                )
                ui.label(
                    "Read-only access is used to spot replies about your applications. The app "
                    "never sends, deletes, or changes mail. Headers decide what matches; the "
                    "message text is then saved for matched mail only, so you can read it on the "
                    "Email matches page. Sign-in happens in your browser."
                ).classes("text-sm opacity-70")

                if connected and scanner.busy:
                    ui.label().classes("text-sm").bind_text_from(
                        scanner, "checked", backward=lambda _n: scanner.progress_text()
                    )
                    ui.linear_progress(show_value=False).props("rounded").bind_value_from(
                        scanner,
                        "checked",
                        backward=lambda n: (n / scanner.total) if scanner.total else 0.0,
                    )

                with ui.row().classes("gap-2 pt-2"):
                    if connected:
                        ui.button("Check for replies", on_click=scan_now).props(
                            "unelevated no-caps"
                        )
                        ui.button("Disconnect", on_click=disconnect_gmail).props("flat no-caps")
                    else:
                        ui.button("Connect Gmail", on_click=connect_gmail).props(
                            "unelevated no-caps"
                        )

        async def connect_gmail():
            """Run the consent flow off the event loop.

            run_auth_flow starts its own local server and blocks until the
            browser redirect arrives, so awaiting it directly would freeze every
            page.
            """
            ui.notify("Opening your browser for consent…")
            try:
                await run.io_bound(gmail_client.run_auth_flow)
            except gmail_client.GmailNotConfigured as exc:
                ui.notify(str(exc), type="negative", multi_line=True, close_button=True)
                return
            except Exception as exc:
                ui.notify(f"Could not connect: {exc}", type="negative", multi_line=True,
                          close_button=True)
                return
            ui.notify("Gmail is connected with read-only access.", type="positive")
            gmail_card.refresh()

        async def disconnect_gmail():
            if not await confirm(
                "Disconnect Gmail",
                "Revoke this app's access to your Gmail account and remove the stored token?",
            ):
                return
            try:
                await run.io_bound(gmail_client.disconnect)
            except Exception as exc:
                ui.notify(f"Could not disconnect: {exc}", type="negative", multi_line=True,
                          close_button=True)
                return
            ui.notify("Access was revoked and the token removed.", type="positive")
            gmail_card.refresh()

        # Groq ----------------------------------------------------------------

        @ui.refreshable
        def groq_card():
            with card():
                ui.label("AI classification (Groq)").classes("text-base font-semibold")
                if not classifier.available:
                    ui.label(llm_client.MISSING_PACKAGES_HINT).classes("text-sm opacity-70")
                    return

                in_keyring = bool(llm_client.stored_api_key())
                configured = classifier.is_configured()
                has_store = credentials.backend_available()
                if in_keyring:
                    source = "stored in your credential manager"
                elif configured:
                    source = "read from .env"
                else:
                    source = "not set"
                if not has_store:
                    source += " (no credential store on this machine)"
                ui.label(f"API key: {source}").classes("text-sm opacity-70")

                if configured:
                    ui.label(
                        f"Model: {llm_client.model_name()}    "
                        f"Pace: {llm_client.requests_per_minute()} requests/min    "
                        f"Auto-apply at: {llm_client.confidence_threshold():.0%} confidence"
                    ).classes("text-sm opacity-70")

                ui.label(
                    "Matched replies are labelled as a rejection, offer, interview, online "
                    "assessment, acknowledgement, or unclear. A label at or above the confidence "
                    "threshold applies the job status automatically and can be undone from the "
                    "Email matches page, which records the status it replaced. Anything below "
                    "the threshold only pre-fills the dropdown. Requests are paced to stay under "
                    "the free tier's limits, and a rate limit pauses the cycle rather than "
                    "retrying."
                ).classes("text-sm opacity-70")

                if configured:
                    ui.label().classes("text-sm").bind_text_from(
                        classifier,
                        "processed",
                        backward=lambda _n: classifier.progress_text() or "Idle.",
                    )
                    if classifier.state in (llm_client.RUNNING, llm_client.RATE_LIMITED):
                        bar = ui.linear_progress(show_value=False).props("rounded")
                        bar.bind_value_from(
                            classifier,
                            "processed",
                            backward=lambda n: (n / classifier.total) if classifier.total else 0.0,
                        )
                        if classifier.state == llm_client.RATE_LIMITED:
                            bar.props("color=warning")

                with ui.row().classes("gap-2 pt-2 flex-wrap"):
                    if configured:
                        ui.button("Test connection", on_click=test_groq).props("flat no-caps")
                        classification_control()
                    # Offering to move the key somewhere that cannot store it
                    # would only produce an error, so the button appears once a
                    # backend answers.
                    if configured and not in_keyring and has_store:
                        ui.button(
                            "Move key to credential manager", on_click=move_groq_key
                        ).props("flat no-caps")
                    if in_keyring:
                        ui.button("Forget stored key", on_click=forget_groq_key).props(
                            "flat no-caps"
                        )

        def classification_control():
            if classifier.busy:
                ui.button("Stop", on_click=classifier.stop).props("flat no-caps")
                return
            if classifier.state in (
                llm_client.RATE_LIMITED, llm_client.STOPPED, llm_client.ERROR
            ):
                ui.button("Resume classification", on_click=classify_now).props(
                    "unelevated no-caps"
                )
                return
            waiting = classifier.pending_count()
            ui.button(
                f"Classify {waiting} message(s)" if waiting else "Classify with AI",
                on_click=classify_now,
            ).props(("unelevated" if waiting else "flat") + " no-caps")

        async def test_groq():
            """Classify one synthetic message so a misconfiguration shows up here."""
            sample = {
                "sender": "careers@example.com",
                "subject": "Thank you for applying",
                "body": "We received your application and will be in touch soon.",
                "company": "Example",
                "position_title": "Engineer",
            }
            try:
                client = llm_client.GroqClient.from_config()
                result = await run.io_bound(client.classify, sample)
            except llm_client.GroqRateLimited as exc:
                ui.notify(
                    f"Groq is rate limiting requests. Try again in about {exc.retry_after}s.",
                    type="warning", multi_line=True, close_button=True,
                )
                return
            except Exception as exc:
                ui.notify(f"Groq test failed: {exc}", type="negative", multi_line=True,
                          close_button=True)
                return
            ui.notify(
                f"Test message classified as {result['label']} "
                f"({result['confidence']:.0%}). {result['reason']}",
                type="positive", multi_line=True, close_button=True,
            )

        def move_groq_key():
            try:
                llm_client.save_api_key(llm_client.api_key())
            except Exception as exc:
                ui.notify(f"Could not store the key: {exc}", type="negative", multi_line=True,
                          close_button=True)
                return
            ui.notify(
                "The Groq key is now in your credential manager, which takes precedence over "
                ".env. You can remove GROQ_API_KEY from .env when you are ready; this app does "
                "not edit that file for you.",
                type="positive", multi_line=True, close_button=True,
            )
            groq_card.refresh()

        async def forget_groq_key():
            if not await confirm(
                "Forget stored key",
                "Remove the Groq key from your credential manager? If GROQ_API_KEY is still set "
                "in .env, that value will be used instead.",
            ):
                return
            llm_client.forget_api_key()
            ui.notify("The stored Groq key was deleted.", type="positive")
            groq_card.refresh()

        # Wiring ---------------------------------------------------------------

        def refresh_all():
            gmail_card.refresh()
            groq_card.refresh()

        def watch():
            """Redraw when a worker changes state.

            Progress numbers are bound, so this only has to catch the moments
            where the available controls change.
            """
            current = (scanner.state, classifier.state)
            if current != (seen["scanner"], seen["classifier"]):
                seen["scanner"], seen["classifier"] = current
                refresh_all()

        gmail_card()
        groq_card()
        page_timer(0.4, watch)


async def confirm(title, question):
    with ui.dialog() as dialog, ui.card().classes("gap-3 p-6 max-w-md"):
        ui.label(title).classes("text-lg font-semibold")
        ui.label(question).classes("text-sm opacity-80")
        with ui.row().classes("justify-end gap-2 w-full"):
            ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat no-caps")
            ui.button("Continue", on_click=lambda: dialog.submit(True)).props(
                "unelevated no-caps"
            )
    return await dialog
