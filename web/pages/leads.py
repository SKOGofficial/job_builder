"""To-apply: roles surfaced from job-board alerts that you have not applied to.

The list is meant to be acted on directly - open a row, click through to the
portal, apply. So a `ready` lead already carries its generated resume and CV;
there is deliberately no "generate" step in the middle.

Rows sit in one of three visible states. `new` means it has not cleared the
relevance gate that keeps research spend proportional, `preparing` means the
artifacts are being built, and `ready` means it can be acted on now. A lead
whose preparation failed stays out of `ready` and shows the reason, so the list
never contains a row whose resume link is dead.
"""

import os

from nicegui import ui

from pipeline.acknowledgements import AcknowledgementHandler
from pipeline.resolver import JobResolver
from utilities.mailstore import (
    LEAD_APPLIED,
    LEAD_DISMISSED,
    LEAD_NEW,
    LEAD_OPEN_STATUSES,
    LEAD_PREPARING,
    LEAD_READY,
)
from web.shell import card, page_shell
from web.state import get_state

STATUS_COLORS = {
    LEAD_READY: "#22c55e",
    LEAD_PREPARING: "#f97316",
    LEAD_NEW: "#64748b",
    LEAD_DISMISSED: "#9ca3af",
    LEAD_APPLIED: "#2563eb",
}

STATUS_HINTS = {
    LEAD_READY: "Resume and CV are built. Open the posting and apply.",
    LEAD_PREPARING: "Research and resume generation are running.",
    LEAD_NEW: "Waiting on relevance scoring, or scored below the threshold.",
}

FILTERS = [
    ("Open", LEAD_OPEN_STATUSES),
    ("Ready", (LEAD_READY,)),
    ("Dismissed", (LEAD_DISMISSED,)),
    ("Applied", (LEAD_APPLIED,)),
    ("All", None),
]


def score_text(lead):
    if lead["relevance_score"] is None:
        return "Not scored yet"
    return f"Relevance {lead['relevance_score']:.0%}"


@ui.page("/leads")
def leads_page():
    state = get_state()
    store, mail = state.store, state.mail
    chosen = {"statuses": LEAD_OPEN_STATUSES}

    with page_shell(
        "To apply",
        "Roles found in job-board alerts. Ready rows already have a tailored resume attached.",
        active="/leads",
    ):

        def promote(lead):
            """Record that the user applied, by hand.

            Same operation the acknowledgement handler performs when a
            thank-you email arrives, so the identity carries across and every
            email already attached to the lead stays attached.
            """
            handler = AcknowledgementHandler(store, mail, JobResolver(store, mail))
            job_id = handler.promote_lead(lead)
            mail.commit()
            lead_list.refresh()
            ui.notify(f"Moved to applications as {job_id}.", type="positive")

        def dismiss(lead):
            mail.set_lead_status(lead["id"], LEAD_DISMISSED)
            lead_list.refresh()
            ui.notify("Lead dismissed.")

        def restore(lead):
            mail.set_lead_status(lead["id"], LEAD_NEW)
            lead_list.refresh()
            ui.notify("Lead restored to the open list.")

        def choose(statuses):
            chosen["statuses"] = statuses
            lead_list.refresh()

        @ui.refreshable
        def lead_list():
            leads = mail.list_leads(chosen["statuses"])
            with card("p-5"):
                with ui.row().classes("w-full items-center gap-2"):
                    for label, statuses in FILTERS:
                        active = chosen["statuses"] == statuses
                        ui.button(
                            label, on_click=lambda s=statuses: choose(s)
                        ).props(
                            ("unelevated" if active else "flat") + " no-caps dense"
                        )
                    ui.space()
                    ui.button(icon="refresh", on_click=lead_list.refresh).props(
                        "flat round dense"
                    ).tooltip("Refresh")

            if not leads:
                with card():
                    ui.label(
                        "No leads here yet. They are created from job-board alert emails "
                        "once the ingest pipeline has run."
                    ).classes("text-sm opacity-70")
                return

            ui.label(f"{len(leads)} lead(s)").classes("text-xs opacity-60")
            for lead in leads:
                lead_card(lead)

        def lead_card(lead):
            with card("p-5"):
                with ui.row().classes("w-full items-start justify-between gap-4"):
                    with ui.column().classes("gap-1 grow"):
                        ui.label(lead["title"]).classes("text-base font-semibold")
                        where = " · ".join(
                            part for part in [lead["company"], lead["location"]] if part
                        )
                        ui.label(where or "Company not identified").classes(
                            "text-sm opacity-70"
                        )
                    status_badge(lead["status"])

                with ui.row().classes("items-center gap-3 flex-wrap"):
                    ui.label(score_text(lead)).classes("text-xs opacity-70")
                    if lead["board"]:
                        ui.label(f"via {lead['board']}").classes("text-xs opacity-60")
                    ui.label(f"Seen {lead['created_at'][:10]}").classes("text-xs opacity-60")

                if lead["relevance_reason"]:
                    ui.label(lead["relevance_reason"]).classes("text-xs opacity-60 italic")

                hint = STATUS_HINTS.get(lead["status"])
                if hint:
                    ui.label(hint).classes("text-xs opacity-60")

                if lead["prepare_error"]:
                    ui.label(f"Preparation failed: {lead['prepare_error']}").classes(
                        "text-xs text-red-500"
                    )

                research(lead)
                artifacts(lead)
                actions(lead)

        def status_badge(status):
            ui.html(
                f'<span style="background-color:{STATUS_COLORS.get(status, "#64748b")};'
                f'color:#fff;padding:2px 10px;border-radius:9999px;font-size:11px;'
                f'font-weight:600">{status}</span>'
            )

        def research(lead):
            row = mail.research_for(lead["identity_key"])
            if row is None or not row["summary"]:
                return
            with ui.expansion("Company research").classes("w-full").props("dense-toggle"):
                ui.label(row["summary"]).classes("text-sm whitespace-pre-wrap px-1 pb-2")
                ui.label(f"Researched {row['fetched_at'][:10]} with {row['model'] or 'unknown model'}").classes(
                    "text-xs opacity-60 px-1"
                )

        def artifacts(lead):
            rows = mail.artifacts_for(lead["identity_key"])
            if not rows:
                return
            with ui.row().classes("items-center gap-2 flex-wrap"):
                ui.label("Documents:").classes("text-xs opacity-70")
                for row in rows:
                    # A recorded path whose file has since been removed would
                    # otherwise render as a link that downloads nothing.
                    if not os.path.exists(row["path"]):
                        ui.label(f"{row['kind']} (file missing)").classes(
                            "text-xs opacity-60 italic"
                        )
                        continue
                    ui.button(
                        row["kind"],
                        icon="description",
                        on_click=lambda p=row["path"]: ui.download(p),
                    ).props("flat dense no-caps")

        def actions(lead):
            with ui.row().classes("items-center gap-2 pt-1 flex-wrap"):
                url = lead["apply_url"] or lead["tracking_url"]
                if url:
                    ui.button(
                        "Open posting",
                        icon="open_in_new",
                        on_click=lambda u=url: ui.navigate.to(u, new_tab=True),
                    ).props("unelevated no-caps dense")
                else:
                    ui.label("No application link was extracted.").classes(
                        "text-xs opacity-60"
                    )

                if lead["status"] == LEAD_DISMISSED:
                    ui.button("Restore", on_click=lambda l=lead: restore(l)).props(
                        "flat no-caps dense"
                    )
                elif lead["status"] != LEAD_APPLIED:
                    ui.button(
                        "I applied to this", on_click=lambda l=lead: promote(l)
                    ).props("flat no-caps dense")
                    ui.button(
                        "Not interested", on_click=lambda l=lead: dismiss(l)
                    ).props("flat no-caps dense")

        lead_list()
