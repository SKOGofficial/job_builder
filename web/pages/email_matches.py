"""Email matches: review suggested Gmail replies and the labels Groq applied.

Confirming or dismissing is the only path a user-driven status write takes, and
a classification the model applied on its own always carries an Undo.

Live progress uses two mechanisms deliberately. The message and the bar are
*bound* to the worker's attributes, so they animate without rebuilding anything.
The surrounding controls are *refreshed*, but only when a worker changes state,
because that is when which buttons exist changes.
"""

from nicegui import ui

from clients import llm_client
from clients.gmail_client import MISSING_PACKAGES_HINT
from utilities.theme import STATUSES
from web.shell import badge, card, page_shell, page_timer
from web.state import get_state


def _column(row, name, default=None):
    """Read a column that may predate the current schema.

    A row fetched through a cached statement can come back without a column
    added by a later migration, so this degrades instead of raising - the same
    guard `pipeline/orchestrator.py` uses for `list_unsubscribe`.

    Summary:
        Read a possibly-absent column off a row.

    Parameters:
        row (sqlite3.Row): The row to read.
        name (str): The column name.
        default: Returned when the column is absent or empty.

    Returns:
        The column value, or `default`.
    """
    try:
        return row[name] or default
    except (IndexError, KeyError):
        return default

NO_BODY_HINT = (
    "No message text stored for this match. Matches recorded before message bodies "
    "were saved show nothing here; run Check for replies again to fetch it."
)

#: Groq labels that map onto a job status. Acknowledgement and Unclear do not,
#: so they never pre-fill the dropdown.
BADGE_COLORS = {
    "Rejected": "#ef4444",
    "Offer": "#22c55e",
    "Interview": "#f97316",
    "OA Received": "#a855f7",
    "Acknowledgement": "#64748b",
    "Unclear": "#94a3b8",
}


def fraction(done, total):
    return (done / total) if total else 0.0


@ui.page("/email-matches")
def email_matches_page():
    state = get_state()
    store, scanner, classifier = state.store, state.scanner, state.classifier
    seen = {"scanner": scanner.state, "classifier": classifier.state}

    with page_shell(
        "Email matches",
        "Suggested replies matched to open applications. Nothing is applied until you confirm it.",
        active="",
    ):
        if not scanner.available:
            with card():
                ui.label(MISSING_PACKAGES_HINT).classes("text-sm opacity-70")
            return

        async def scan_now():
            found = await scanner.scan()
            refresh_all()
            # Only notify if we're still in a valid context (not navigated away)
            try:
                ui.notify(scanner.message, type="negative" if scanner.state == "error" else "positive")
            except RuntimeError:
                pass
            # New matches are exactly what the classifier exists to label, so a
            # scan that found something rolls straight into a cycle.
            if found and classifier.available and classifier.is_configured():
                await classifier.run()
                refresh_all()

        async def classify_now():
            await classifier.run()
            refresh_all()

        # Gmail --------------------------------------------------------------

        @ui.refreshable
        def gmail_card():
            with card("p-5"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Gmail").classes("text-base font-semibold")
                    if scanner.busy:
                        ui.spinner(size="sm")
                    elif scanner.is_connected():
                        ui.button("Check for replies", on_click=scan_now).props(
                            "unelevated no-caps dense"
                        )
                    else:
                        ui.button(
                            "Connect Gmail in Settings",
                            on_click=lambda: ui.navigate.to("/settings"),
                        ).props("flat no-caps dense")

                ui.label().classes("text-sm opacity-70").bind_text_from(
                    scanner, "state", backward=lambda _s: scanner.progress_text() or "Idle."
                )
                if scanner.busy:
                    ui.linear_progress(show_value=False).props("rounded").bind_value_from(
                        scanner, "checked", backward=lambda n: fraction(n, scanner.total)
                    )

        # Groq ---------------------------------------------------------------

        @ui.refreshable
        def classifier_card():
            if not classifier.available:
                return
            with card("p-5"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("AI classification").classes("text-base font-semibold")
                    classifier_controls()

                if not classifier.is_configured():
                    ui.label(
                        "No Groq API key found. Add GROQ_API_KEY to .env, or store it in "
                        "your credential manager from Settings."
                    ).classes("text-sm opacity-70")
                    ui.button(
                        "Open Settings", on_click=lambda: ui.navigate.to("/settings")
                    ).props("flat no-caps dense").classes("self-start")
                    return

                ui.label().classes("text-sm opacity-70").bind_text_from(
                    classifier, "state", backward=lambda _s: classifier.progress_text() or "Idle."
                )
                if classifier.state in (llm_client.RUNNING, llm_client.RATE_LIMITED):
                    bar = ui.linear_progress(show_value=False).props("rounded")
                    bar.bind_value_from(
                        classifier,
                        "processed",
                        backward=lambda n: fraction(n, classifier.total),
                    )
                    if classifier.state == llm_client.RATE_LIMITED:
                        # Amber rather than the running colour: the cycle stalled
                        # part way rather than finishing.
                        bar.props("color=warning")

        def classifier_controls():
            if not classifier.is_configured():
                return
            if classifier.busy:
                ui.button("Stop", on_click=classifier.stop).props("flat no-caps dense")
                return
            if classifier.state in (
                llm_client.RATE_LIMITED, llm_client.STOPPED, llm_client.ERROR
            ):
                ui.button("Resume classification", on_click=classify_now).props(
                    "unelevated no-caps dense"
                )
                return
            waiting = classifier.pending_count()
            ui.button(
                f"Classify {waiting} message(s)" if waiting else "Classify with AI",
                on_click=classify_now,
            ).props(("unelevated" if waiting else "flat") + " no-caps dense")

        # Matches --------------------------------------------------------------

        @ui.refreshable
        def match_list():
            matches = store.pending_email_matches()
            if not matches:
                with card():
                    ui.label(
                        "No pending matches. Use Check for replies to scan your inbox."
                    ).classes("text-sm opacity-70")
                return
            ui.label(f"{len(matches)} pending").classes("text-xs opacity-60")
            for match in matches:
                match_card(match)

        def match_card(match):
            with card("p-0"):
                title = (
                    f"{match['position_title']} at {match['company'] or 'Unknown company'}"
                )
                with ui.expansion(title).classes("w-full").props("dense-toggle"):
                    with ui.column().classes("w-full gap-2 px-4 pb-4"):
                        ui.label(f"From: {match['sender']}").classes("text-xs opacity-70 break-all")
                        ui.label(f"Subject: {match['subject']}").classes("text-sm")
                        ui.label(
                            f"Received: {match['received_date']}  ·  "
                            f"Current status: {match['job_status']}"
                        ).classes("text-xs opacity-70")

                        ai_badge(match)

                        body = (match["body_text"] or "").strip()
                        if body:
                            ui.label(body).classes(
                                "w-full whitespace-pre-wrap text-sm rounded p-3 "
                                "max-h-80 overflow-auto bg-black/5 dark:bg-white/5"
                            )
                        else:
                            ui.label(NO_BODY_HINT).classes("text-xs opacity-60")

                        actions(match)

        def ai_badge(match):
            label = match["ai_status"]
            if not label:
                return
            with ui.row().classes("items-center gap-2 flex-wrap"):
                badge(
                    f'AI: {label} · {(match["ai_confidence"] or 0):.0%}',
                    BADGE_COLORS.get(label, "#64748b"),
                )
                model = _column(match, "ai_model")
                if model:
                    # Which model said so. With more than one provider and a
                    # shared confidence threshold, this is the only way to tell
                    # afterwards whether a bad label came from the primary or
                    # the fallback.
                    ui.label(f"via {model}").classes("text-xs opacity-60")
                if match["ai_applied"]:
                    previous = match["ai_previous_status"] or "unset"
                    ui.label(f"Applied automatically, replacing {previous}.").classes(
                        "text-xs opacity-70"
                    )
                    ui.button("Undo", on_click=lambda m=match: undo(m["id"])).props(
                        "flat dense no-caps"
                    ).classes("text-xs")
            if match["ai_reason"]:
                ui.label(match["ai_reason"]).classes("text-xs opacity-60 italic")

        def actions(match):
            # The model's label pre-fills the dropdown only when it maps to a
            # real status; Acknowledgement and Unclear deliberately do not.
            suggested = match["ai_status"] if match["ai_status"] in STATUSES else "Interview"
            with ui.row().classes("items-center gap-2 pt-1"):
                ui.label("Set status to").classes("text-sm")
                choice = ui.select(STATUSES, value=suggested).props("dense outlined").classes("w-44")
                ui.button(
                    "Confirm", on_click=lambda m=match: confirm(m["id"], choice.value)
                ).props("unelevated no-caps dense")
                ui.button("Dismiss", on_click=lambda m=match: dismiss(m["id"])).props(
                    "flat no-caps dense"
                )

        # Row actions ----------------------------------------------------------

        def confirm(match_id, status):
            store.confirm_email_match(match_id, status)
            match_list.refresh()
            classifier_card.refresh()
            ui.notify(f"Status set to {status}.", type="positive")

        def dismiss(match_id):
            store.dismiss_email_match(match_id)
            match_list.refresh()
            classifier_card.refresh()
            ui.notify("Match dismissed.")

        def undo(match_id):
            store.undo_ai_status(match_id)
            match_list.refresh()
            ui.notify("Reverted the status the classifier applied.", type="warning")

        def refresh_all():
            gmail_card.refresh()
            classifier_card.refresh()
            match_list.refresh()

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
        classifier_card()
        match_list()
        page_timer(0.4, watch)
