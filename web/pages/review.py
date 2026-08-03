"""Review queue: job mail the resolver could not confidently place.

This exists because the alternative is worse than not classifying at all. A
message the pipeline recognised as job-related but could not attach to a job
would otherwise be stored, categorised, and linked to nothing - which looks
exactly like the pipeline working. Anything that lands here needs a human to
pick the job, or to say the sender was never job related in the first place.

Ambiguity is the common case, not an edge case: three open applications at the
same large company and an update email that only says "your application" is
unresolvable by design. The resolver is built to queue rather than guess.
"""

from nicegui import ui

from clients.gmail_client import sender_domain
from pipeline.resolver import JobResolver
from utilities.mailstore import (
    CATEGORY_ACKNOWLEDGEMENT,
    CATEGORY_ALERT,
    CATEGORY_UPDATE,
)
from web.shell import card, page_shell
from web.state import get_state

CATEGORY_LABELS = {
    CATEGORY_ALERT: "Job alert",
    CATEGORY_UPDATE: "Application update",
    CATEGORY_ACKNOWLEDGEMENT: "Acknowledgement",
}


def job_choices(store):
    """Job identities, labelled for a picker.

    Keyed on `identity_key` rather than a row id because that is what a link
    points at - the same reason a lead keeps its email history when promoted.
    """
    choices = {}
    for row in store.list_jobs():
        if not row["identity_key"]:
            continue
        where = " · ".join(
            part for part in [row["company"], row["location"]] if part
        )
        label = f"{row['position_title']}" + (f" — {where}" if where else "")
        choices[row["identity_key"]] = label
    return choices


@ui.page("/review")
def review_page():
    state = get_state()
    store, mail = state.store, state.mail

    with page_shell(
        "Review queue",
        "Job-related mail the pipeline could not attach to an application. Pick the job it "
        "belongs to, or mark the sender as never job related.",
        active="/review",
    ):

        def link(message, identity_key):
            """Place a message by hand.

            Goes through the resolver's own manual path rather than writing the
            link here, so a hand-placed message is stored exactly the way an
            auto-resolved one is - same link type, same commit.
            """
            if not identity_key:
                ui.notify("Choose a job first.", type="warning")
                return
            JobResolver(store, mail).link_manually(
                message["gmail_message_id"],
                identity_key,
                message["category"] or CATEGORY_UPDATE,
            )
            queue.refresh()
            ui.notify("Linked. It now appears on that job's timeline.", type="positive")

        def deny(message):
            """Teach the rough filter to drop this sender in future.

            This is the rule that compounds over time - every domain added here
            is mail the model never has to be paid to reject again.
            """
            domain = sender_domain(message["sender"])
            if not domain:
                ui.notify("No sender domain to block.", type="warning")
                return
            mail.deny_sender(domain, reason="Marked not job related from the review queue")
            queue.refresh()
            ui.notify(f"{domain} will be dropped before classification from now on.")

        @ui.refreshable
        def queue():
            messages = mail.unlinked_messages()
            choices = job_choices(store)

            if not messages:
                with card():
                    ui.label(
                        "Nothing waiting. Job mail the pipeline resolves on its own never "
                        "reaches this queue."
                    ).classes("text-sm opacity-70")
                return

            with ui.row().classes("w-full items-center gap-3"):
                ui.label(f"{len(messages)} message(s) to place").classes("text-xs opacity-60")
                ui.space()
                ui.button(icon="refresh", on_click=queue.refresh).props(
                    "flat round dense"
                ).tooltip("Refresh")

            if not choices:
                with card():
                    ui.label(
                        "No applications to link against yet. Add one first, and these "
                        "messages will still be here."
                    ).classes("text-sm opacity-70")

            for message in messages:
                message_card(message, choices)

        def message_card(message, choices):
            with card("p-0"):
                title = message["subject"] or "(no subject)"
                with ui.expansion(title).classes("w-full").props("dense-toggle"):
                    with ui.column().classes("w-full gap-2 px-4 pb-4"):
                        ui.label(f"From: {message['sender']}").classes(
                            "text-xs opacity-70 break-all"
                        )
                        ui.label(f"Received: {message['received_date']}").classes(
                            "text-xs opacity-70"
                        )
                        category(message)

                        body = (message["body_text"] or message["snippet"] or "").strip()
                        if body:
                            ui.label(body).classes(
                                "w-full whitespace-pre-wrap text-sm rounded p-3 "
                                "max-h-72 overflow-auto bg-black/5 dark:bg-white/5"
                            )

                        with ui.row().classes("items-center gap-2 pt-1 flex-wrap"):
                            picker = ui.select(
                                choices, label="Choose an application", with_input=True
                            ).props("dense outlined").classes("w-80")
                            ui.button(
                                "Link",
                                on_click=lambda m=message, p=picker: link(m, p.value),
                            ).props("unelevated no-caps dense")
                            ui.button(
                                "Not job related",
                                on_click=lambda m=message: deny(m),
                            ).props("flat no-caps dense")

        def category(message):
            label = CATEGORY_LABELS.get(message["category"], message["category"] or "")
            if not label:
                return
            with ui.row().classes("items-center gap-2"):
                ui.label(label).classes("text-xs font-medium")
                if message["category_confidence"] is not None:
                    ui.label(f"{message['category_confidence']:.0%} confident").classes(
                        "text-xs opacity-60"
                    )
            if message["category_reason"]:
                ui.label(message["category_reason"]).classes("text-xs opacity-60 italic")

        queue()
