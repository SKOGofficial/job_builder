"""The model half of the Settings page: providers, and which one does what.

Split out of `settings.py` because that file already owns appearance, Gmail,
ingest and the denylist, and this adds two more cards with their own handlers.
Not under `web/pages/` on purpose: nothing here registers a route, and anything
listed in `web/pages/__init__.__all__` gets reloaded by the page-test fixture.

Design note, since this is the first new surface in a while: the app already
has a settled vocabulary - bordered cards, `text-base font-semibold` headings,
`text-sm opacity-70` for explanation, `text-xs opacity-60` for metadata - and a
card that looked different here would read as broken rather than considered.
So everything borrows that, and the one genuinely new thing gets the only new
device: a thin meter under any provider with a daily ceiling. "How much Gemini
is left" is the question this feature exists to answer, and a number alone
makes you do arithmetic to find out whether to worry.
"""

from nicegui import run, ui

from clients import llm_client
from clients.providers import gemini
from clients.providers.routing import TASKS, chain_for
from utilities import credentials
from utilities.durations import spell_duration
from web.shell import card

#: Providers whose API key can be managed from this page, in display order.
#: Anthropic is absent deliberately - it has never had a key UI, and adding one
#: is a separate change from making its usage visible.
KEY_MANAGED = (
    ("groq", "Groq", llm_client),
    ("gemini", "Gemini", gemini),
)

#: Providers with no secret to manage, so "no key" would be the wrong thing to
#: say about them. What the CLI provider lacks when unconfigured is the binary.
KEYLESS = frozenset({"claude_cli"})

#: Shown in the routing selects for "do not use a second provider".
NO_FALLBACK = "None"


def key_source(module, configured):
    """Where this provider's key is coming from, in the user's terms.

    Summary:
        Describe the resolved source of a provider's API key.

    Parameters:
        module: The provider module, exposing `stored_api_key`.
        configured (bool): Whether a key resolves at all.

    Returns:
        str: A phrase for the UI, matching the wording Gmail already uses.
    """
    if module.stored_api_key():
        source = "stored in your credential manager"
    elif configured:
        source = "read from .env"
    else:
        source = "not set"
    if not credentials.backend_available():
        source += " (no credential store on this machine)"
    return source


def usage_line(row):
    """One provider's model and spend, as a single readable line.

    Summary:
        Format a provider's model, daily usage, and cooldown for display.

    Parameters:
        row (dict): A `ProviderState.snapshot` entry.

    Returns:
        str: The description line.
    """
    parts = []
    if row.get("model"):
        parts.append(row["model"])
    if row.get("limit"):
        parts.append(f"{row['used']} of {row['limit']} requests today")
    elif row.get("used"):
        parts.append(f"{row['used']} requests today")
    else:
        parts.append("no requests yet today")
    return " · ".join(parts)


def provider_row(row, module, handlers):
    """Render one provider: where its key is, what it has spent, what you can do.

    Summary:
        Draw a single provider's status block and key controls.

    Parameters:
        row (dict): A `ProviderState.snapshot` entry.
        module: The provider module, for key management. None when the
            provider has no key UI.
        handlers (dict): Callbacks keyed `test`, `move`, `forget`.
    """
    with ui.column().classes("w-full gap-1 pt-2"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.label(row["display"]).classes("text-sm font-medium")
            if not row["configured"]:
                # "no key" is wrong for a provider that has no key to be
                # missing; what it lacks is the binary.
                ui.badge(
                    "not installed" if row["name"] in KEYLESS else "no key"
                ).props("color=grey-7")
            elif row["cooling"]:
                ui.badge("cooling down").props("color=orange")

        if module is not None:
            ui.label(f"API key: {key_source(module, row['configured'])}").classes(
                "text-xs opacity-70"
            )
        if not row["configured"]:
            if row.get("last_error"):
                ui.label(row["last_error"]).classes("text-xs opacity-60")
            return

        ui.label(usage_line(row)).classes("text-xs opacity-70")

        # The meter only appears where there is a ceiling to be measured
        # against. Groq and Claude have none, and a full-looking bar for them
        # would imply a limit that does not exist.
        if row.get("limit"):
            share = min(1.0, row["used"] / row["limit"]) if row["limit"] else 0.0
            bar = ui.linear_progress(value=share, show_value=False).props(
                "rounded size=4px"
            ).classes("w-full max-w-xs")
            if share >= 0.9:
                bar.props("color=red")
            elif share >= 0.5:
                # Matches where spread pacing engages, so the colour change
                # explains the slowdown rather than just warning about it.
                bar.props("color=orange")

        if row["cooling"]:
            ui.label(
                f"Rate limited. Trying again in about "
                f"{spell_duration(row['cooldown_seconds']) or 'a moment'}; "
                f"other providers keep working."
            ).classes("text-xs text-orange-600 dark:text-orange-400")

        with ui.row().classes("gap-2 pt-1 flex-wrap"):
            if handlers.get("test"):
                ui.button("Test connection", on_click=handlers["test"]).props(
                    "flat no-caps dense"
                )
            if handlers.get("move"):
                ui.button("Move key to credential manager",
                          on_click=handlers["move"]).props("flat no-caps dense")
            if handlers.get("forget"):
                ui.button("Forget stored key", on_click=handlers["forget"]).props(
                    "flat no-caps dense"
                )


def build_ai_card(state, classifier, controls, notify):
    """Build the refreshable "AI classification" card.

    Summary:
        Create the provider status card for the Settings page.

    Parameters:
        state (AppState): Shared state, for the pool.
        classifier (ClassificationRunner): The legacy per-job classifier.
        controls (Callable): Draws the classify/stop/resume buttons.
        notify (Callable): `(message, kind)` notifier, injected so this module
            never has to decide whether a NiceGUI client is still attached.

    Returns:
        Callable: The refreshable card, with `.refresh()`.
    """

    def test(name, module):
        """Classify one synthetic message, so a misconfiguration shows up here."""

        async def run_test():
            sample = {
                "sender": "careers@example.com",
                "subject": "Thank you for applying",
                "body": "We received your application and will be in touch soon.",
                "company": "Example",
                "position_title": "Engineer",
            }
            try:
                client = module.GeminiClient.from_config() if name == "gemini" \
                    else module.GroqClient.from_config()
                result = await run.io_bound(client.classify, sample)
            except llm_client.GroqRateLimited as exc:
                notify(
                    f"{exc.provider or 'The provider'} is rate limiting requests. "
                    f"Try again in about {exc.retry_after}s.",
                    "warning",
                )
                return
            except Exception as exc:
                notify(f"Test failed: {exc}", "negative")
                return
            notify(
                f"Test message classified as {result['label']} "
                f"({result['confidence']:.0%}). {result['reason']}",
                "positive",
            )

        return run_test

    async def check_cli():
        """Report the CLI's version, which is the whole of its configuration.

        Summary:
            Run `claude --version` and report what came back.
        """
        import subprocess

        from clients.providers import claude_cli

        def probe():
            return subprocess.run(
                [claude_cli.binary_path(), "--version"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30, cwd=claude_cli.workdir(),
            )

        try:
            completed = await run.io_bound(probe)
        except Exception as exc:
            notify(f"The Claude Code CLI could not be run: {exc}", "negative")
            return
        detail = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode != 0:
            notify(f"The Claude Code CLI returned an error: {detail}", "negative")
            return
        notify(f"Claude Code CLI is available: {detail}", "positive")

    def move(name, module):
        def run_move():
            try:
                module.save_api_key(module.api_key())
            except Exception as exc:
                notify(f"Could not store the key: {exc}", "negative")
                return
            notify(
                f"The {name} key is now in your credential manager, which takes "
                f"precedence over .env. You can remove it from .env when you are "
                f"ready; this app does not edit that file for you.",
                "positive",
            )
            ai_card.refresh()

        return run_move

    def forget(name, module, confirm):
        async def run_forget():
            if not await confirm(
                "Forget stored key",
                f"Remove the {name} key from your credential manager? If it is "
                f"still set in .env, that value will be used instead.",
            ):
                return
            module.forget_api_key()
            notify(f"The stored {name} key was deleted.", "positive")
            ai_card.refresh()

        return run_forget

    @ui.refreshable
    def ai_card(confirm=None):
        with card():
            ui.label("AI classification").classes("text-base font-semibold")
            ui.label(
                "Email is labelled by whichever model is routed to the job below. "
                "A label at or above the confidence threshold applies the job "
                "status automatically and can be undone from Email matches, which "
                "records the status it replaced. Requests are paced to stay inside "
                "each free tier, and a provider that runs out hands the work to "
                "the next one rather than stopping."
            ).classes("text-sm opacity-70")

            try:
                rows = {row["name"]: row for row in state.pool.status()}
            except Exception as exc:
                ui.label(f"The model providers could not be loaded: {exc}").classes(
                    "text-sm text-red-500"
                )
                return

            modules = dict((name, module) for name, _label, module in KEY_MANAGED)
            for name, _label, module in KEY_MANAGED:
                row = rows.get(name)
                if row is None:
                    continue
                handlers = {}
                if row["configured"]:
                    handlers["test"] = test(name, module)
                    if not module.stored_api_key() and credentials.backend_available():
                        handlers["move"] = move(name, module)
                if module.stored_api_key():
                    handlers["forget"] = forget(name, module, confirm)
                provider_row(row, module, handlers)

            for name, row in rows.items():
                if name in modules:
                    continue
                # The CLI provider has no key to test, but it does have a
                # binary that can be missing, stale, or signed out - which is
                # the same question "Test connection" answers for the others.
                extras = {"test": check_cli} if name == "claude_cli" and \
                    row["configured"] else {}
                provider_row(row, None, extras)

            ui.separator().classes("my-1")
            ui.label(
                f"Auto-apply at: {llm_client.confidence_threshold():.0%} confidence"
            ).classes("text-xs opacity-70")
            ui.label(
                "The threshold is about the classification, not the model, so it "
                "applies whichever provider produced the label."
            ).classes("text-xs opacity-60")

            if classifier.available and classifier.is_configured():
                ui.label().classes("text-sm").bind_text_from(
                    classifier, "processed",
                    backward=lambda _n: classifier.progress_text() or "Idle.",
                )
                if classifier.state in (llm_client.RUNNING, llm_client.RATE_LIMITED):
                    bar = ui.linear_progress(show_value=False).props("rounded")
                    bar.bind_value_from(
                        classifier, "processed",
                        backward=lambda n: (n / classifier.total)
                        if classifier.total else 0.0,
                    )
                    if classifier.state == llm_client.RATE_LIMITED:
                        bar.props("color=warning")
            with ui.row().classes("gap-2 pt-2 flex-wrap"):
                controls()

    return ai_card


def build_routing_card(state, notify):
    """Build the refreshable "Task routing" card.

    Summary:
        Create the per-task provider routing editor.

    Parameters:
        state (AppState): Shared state, for the pool and the mailstore.
        notify (Callable): `(message, kind)` notifier.

    Returns:
        Callable: The refreshable card, with `.refresh()`.
    """

    def options_for(task, rows):
        """Providers that can serve a task, labelled for a dropdown.

        An unconfigured provider is offered rather than hidden, with its state
        in the label. Hiding it makes the fix undiscoverable: you would pick
        between one option and wonder where the other went.
        """
        options = {}
        for name, row in rows.items():
            if task.shape not in row["shapes"]:
                continue
            label = row["display"]
            if not row["configured"]:
                label += " (no key)"
            options[name] = label
        return options

    def save(task_id, *choices):
        """
        Summary:
            Persist a task's provider chain from the select values.

        Parameters:
            task_id (str): The task being routed.
            *choices (str | None): Select values, in chain order.

        Note:
            Three slots rather than two. The table behind this held only a
            primary and a fallback, so saving here used to drop the third
            provider from a chain the .env had named - silently, and with no
            way for a two-dropdown UI to show that it had.
        """
        chain = [None if value in (NO_FALLBACK, None) else value
                 for value in choices]
        state.mail.set_provider_route(task_id, *chain)
        state.pool.reload_routes()
        notify("Routing saved. It applies from the next cycle.", "positive")
        routing_card.refresh()

    def reset(task_id):
        state.mail.clear_provider_route(task_id)
        state.pool.reload_routes()
        notify("Reset to the default for that job.", "positive")
        routing_card.refresh()

    @ui.refreshable
    def routing_card():
        with card():
            ui.label("Task routing").classes("text-base font-semibold")
            ui.label(
                "Which model does which job, and which one picks up when the "
                "first runs out. A job with no saved choice follows the default "
                "in your .env, so changing that file keeps working."
            ).classes("text-sm opacity-70")

            try:
                rows = {row["name"]: row for row in state.pool.status()}
                saved = state.mail.provider_routes()
            except Exception as exc:
                ui.label(f"Routing could not be loaded: {exc}").classes(
                    "text-sm text-red-500"
                )
                return

            for task_id, task in TASKS.items():
                chain = chain_for(task_id, saved)
                options = options_for(task, rows)
                primary_options = dict(options)
                fallback_options = {NO_FALLBACK: NO_FALLBACK, **options}

                with ui.column().classes("w-full gap-0 pt-3"):
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label(task.label).classes("text-sm font-medium")
                        if task_id in saved:
                            ui.button(
                                "Reset", on_click=lambda t=task_id: reset(t)
                            ).props("flat no-caps dense size=sm")
                    ui.label(task.description).classes("text-xs opacity-60")

                    with ui.row().classes("items-center gap-2 pt-1 flex-wrap"):
                        primary = ui.select(
                            primary_options,
                            value=chain[0] if chain else None,
                        ).props("dense outlined").classes("min-w-[11rem]")
                        ui.label("then").classes("text-xs opacity-60")
                        fallback = ui.select(
                            fallback_options,
                            value=chain[1] if len(chain) > 1 else NO_FALLBACK,
                        ).props("dense outlined").classes("min-w-[11rem]")
                        ui.label("then").classes("text-xs opacity-60")
                        # Third slot, because the .env chains have three and a
                        # two-slot editor could only ever save the first two of
                        # them.
                        third = ui.select(
                            fallback_options,
                            value=chain[2] if len(chain) > 2 else NO_FALLBACK,
                        ).props("dense outlined").classes("min-w-[11rem]")

                    selects = (primary, fallback, third)
                    for element in selects:
                        element.on_value_change(
                            lambda e, t=task_id, s=selects:
                            save(t, *(item.value for item in s))
                        )

    return routing_card
