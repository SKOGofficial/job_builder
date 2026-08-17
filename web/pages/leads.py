"""To-apply: roles surfaced from job-board alerts that you have not applied to.

The list is meant to be acted on directly - open a row, click through to the
portal, apply. A `ready` lead has already had the expensive half done: the
company researched, its resume bullets chosen, its letter written. Clicking
Resume or Cover Letter only renders and delivers, so there is still no
"generate" step waiting on a model.

Rows sit in one of three visible states. `new` means it has not cleared the
relevance gate that keeps research spend proportional, `preparing` means the
work is under way, and `ready` means it can be acted on now. A lead whose
preparation failed stays out of `ready` and shows the reason.

Ordering is by posting date, newest first, and open leads are deleted once the
posting passes `LEAD_FRESHNESS_DAYS`. Both are the same judgement: applying
early is most of what decides whether an application is read at all, so the
freshest role belongs at the top, and a two-month-old posting on a to-apply
list is not a task - it is a role that has already been filled.
"""

import asyncio
import logging
import os
import time

from nicegui import ui

from pipeline.acknowledgements import AcknowledgementHandler
from pipeline.documents import build_document, deliver, document_name
from pipeline.generate import ARTIFACT_COVER_LETTER, ARTIFACT_RESUME
from pipeline.resolver import JobResolver
from utilities.mailstore import (
    LEAD_APPLIED,
    LEAD_DISMISSED,
    LEAD_FRESHNESS_DAYS,
    LEAD_NEW,
    LEAD_OPEN_STATUSES,
    LEAD_PREPARING,
    LEAD_READY,
    PREPARE_WAITING_PREFIX,
)
from web.shell import card, page_shell
from web.state import get_state

log = logging.getLogger(__name__)

STATUS_COLORS = {
    LEAD_READY: "#22c55e",
    LEAD_PREPARING: "#f97316",
    LEAD_NEW: "#64748b",
    LEAD_DISMISSED: "#9ca3af",
    LEAD_APPLIED: "#2563eb",
}

STATUS_HINTS = {
    LEAD_READY: "Resume and cover letter are ready. Download and apply.",
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


def posted_text(lead):
    """How long ago the role was advertised.

    Summary:
        Describe a lead's posting age in words, for the row's metadata line.

    Parameters:
        lead (Mapping): The lead to describe.

    Returns:
        str: A phrase such as "Posted today" or "Posted 6 days ago", or an
            empty string when the lead carries no posting date.

    Note:
        Age rather than a date, because the number that matters is the distance
        from the freshness window - "12 days ago" says the row is about to be
        dropped in a way "5 August" does not.
    """
    posted = _column(lead, "posted_ts")
    if not posted:
        return ""
    days = max(0, int((time.time() - posted) // 86400))
    if days == 0:
        return "Posted today"
    if days == 1:
        return "Posted yesterday"
    return f"Posted {days} days ago"


def _column(row, name, default=None):
    """Read a column that may predate the current schema.

    Summary:
        Read a possibly-absent column off a row.

    Parameters:
        row (Mapping): The row to read.
        name (str): The column name.
        default: Returned when the column is absent or empty.

    Returns:
        The column value, or `default`.

    Note:
        Same guard the orchestrator uses for `list_unsubscribe`: a `sqlite3.Row`
        fetched through a statement cached before a migration comes back
        without the new column and raises rather than returning None.
    """
    try:
        return row[name] or default
    except (IndexError, KeyError):
        return default


@ui.page("/leads")
def leads_page():
    state = get_state()
    store, mail = state.store, state.mail
    chosen = {"statuses": LEAD_OPEN_STATUSES}

    with page_shell(
        "To apply",
        "Roles found in job-board alerts, newest posting first. Ready rows have a "
        f"tailored resume and cover letter waiting. Anything still unapplied after "
        f"{LEAD_FRESHNESS_DAYS} days is dropped automatically.",
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
                    posted = posted_text(lead)
                    if posted:
                        # First on the line, and the only one emphasised: the
                        # list is sorted by it, so it is the number that
                        # explains the row's position.
                        ui.label(posted).classes("text-xs font-medium")
                    ui.label(score_text(lead)).classes("text-xs opacity-70")
                    if lead["board"]:
                        ui.label(f"via {lead['board']}").classes("text-xs opacity-60")

                if lead["relevance_reason"]:
                    ui.label(lead["relevance_reason"]).classes("text-xs opacity-60 italic")

                hint = STATUS_HINTS.get(lead["status"])
                if hint:
                    ui.label(hint).classes("text-xs opacity-60")

                if lead["prepare_error"]:
                    # A pause is not a failure. Saying "Preparation failed" about
                    # a lead that is only waiting out a rate limit reads as
                    # something to fix, when the next cycle handles it.
                    if lead["prepare_error"].startswith(PREPARE_WAITING_PREFIX):
                        ui.label(lead["prepare_error"]).classes(
                            "text-xs text-amber-500"
                        )
                    else:
                        ui.label(
                            f"Preparation failed: {lead['prepare_error']}"
                        ).classes("text-xs text-red-500")

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

        def profile():
            """Contact details for a document header.

            Summary:
                Read the stored profile values a rendered document needs.

            Returns:
                dict: Name, email, phone, location, and website. Missing values
                    come back empty rather than absent.
            """
            get = store.get_profile_value
            return {key: get(key, "") for key in
                    ("name", "email", "phone", "location", "website")}

        async def download(lead, kind):
            """Render one document and put it in the user's Downloads folder.

            Summary:
                Build and deliver a resume or covering letter on demand.

            Parameters:
                lead (Mapping): The lead the document belongs to.
                kind (str): `resume` or `cover_letter`.

            Note:
                Split across the thread boundary the same way every other
                stage is. The database reads and the LaTeX rendering happen
                here, on the thread that owns the sqlite connection; only the
                compile - which shells out and blocks for a second or more -
                goes to a worker. Sending the whole build over would hand a
                connection to a thread that does not own it.
            """
            label = kind.replace("_", " ")
            try:
                tex_text = build_document(mail, profile(), dict(lead), kind)
                path, is_pdf = await asyncio.to_thread(
                    deliver, tex_text, document_name(kind, dict(lead))
                )
            except LookupError as exc:
                ui.notify(str(exc), type="warning")
                return
            except FileNotFoundError as exc:
                # Almost always an unset JOB_BUILDER_RESUME_MASTER, so say so
                # rather than showing a bare path.
                ui.notify(str(exc), type="negative", timeout=0, close_button=True)
                return
            except Exception as exc:
                log.exception("Could not build the %s for lead %s",
                              label, lead["id"])
                ui.notify(f"Could not build the {label}: {exc}",
                          type="negative")
                return

            name = os.path.basename(path)
            if is_pdf:
                ui.notify(f"Saved {name} to your Downloads folder.",
                          type="positive")
            else:
                ui.notify(
                    f"Saved {name} to your Downloads folder. No LaTeX engine "
                    "is installed, so this is the source rather than a PDF - "
                    "it compiles as-is on Overleaf.",
                    type="warning", timeout=0, close_button=True,
                )

        def artifacts(lead):
            records = {r["kind"] for r in mail.selections_for(lead["identity_key"])}
            if not records:
                return
            with ui.row().classes("items-center gap-2 flex-wrap"):
                ui.label("Documents:").classes("text-xs opacity-70")
                for kind, text, icon in (
                    (ARTIFACT_RESUME, "Resume", "description"),
                    (ARTIFACT_COVER_LETTER, "Cover letter", "mail"),
                ):
                    if kind not in records:
                        continue
                    ui.button(
                        text, icon=icon,
                        on_click=lambda l=lead, k=kind: download(l, k),
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
