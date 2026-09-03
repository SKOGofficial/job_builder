"""To-apply: roles surfaced from job-board alerts that you have not applied to.

The list is a shortlist to choose from, not a queue that has already been
worked. Nothing here costs anything until you press Generate on a specific
role: the pipeline scores every lead, because scoring is free and is what makes
the list rankable, and then stops.

That is a reversal. Documents used to be built unattended for anything scoring
above 0.45, on the theory that the list should be application-ready before it
was opened. What it produced was 363 leads and eleven documents, none of them
asked for - a relevance score is a reasonable way to sort a list and a poor way
to authorise a research call.

Rows sit in one of three visible states. `new` means nothing has been built
yet, which is now the normal resting state rather than a sign of a low score.
`preparing` means a Generate click is under way, and `ready` means the
documents exist. A lead whose preparation failed stays out of `ready` and shows
the reason, so the list never offers a link that does not work.

Ordering defaults to posting date, newest first, and open leads are deleted
once the posting passes `LEAD_FRESHNESS_DAYS` - unless documents have been
built for it, which is proof someone meant to apply. Applying early is most of
what decides whether an application is read at all, so the freshest role
belongs at the top, and a two-month-old posting on a to-apply list is not a
task; it is a role that has already been filled.
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
    LEAD_SORT_POSTED,
    LEAD_SORT_RELEVANCE,
    PREPARE_WAITING_PREFIX,
)
from web.shell import badge, card, page_shell
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
    LEAD_PREPARING: "Research and document generation are running.",
    LEAD_NEW: "No documents yet. Press Generate if you want to apply.",
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
    chosen = {"statuses": LEAD_OPEN_STATUSES, "sort": LEAD_SORT_POSTED}
    #: Lead ids with a Generate click in flight. The status column would nearly
    #: do this on its own, but a lead left at `preparing` by a crashed run
    #: would then be permanently un-clickable; this clears with the page.
    preparing = set()

    with page_shell(
        "To apply",
        "Roles found in job-board alerts, newest posting first. Nothing is "
        "written until you press Generate on a role you want. Anything still "
        f"untouched after {LEAD_FRESHNESS_DAYS} days is dropped automatically; "
        "a lead you have generated documents for is kept.",
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

        def sort_by(key):
            chosen["sort"] = key
            lead_list.refresh()

        async def generate(lead):
            """Research the company and build this lead's documents, on demand.

            Summary:
                Run preparation for one lead and refresh the list.

            Parameters:
                lead (Mapping): The lead row to prepare.

            Note:
                This is the click the pipeline no longer makes on its own.
                `prepare_now` bypasses the relevance threshold on purpose:
                asking for a role *is* the judgement the score was standing in
                for, and a threshold set slightly wrong should not make a role
                unreachable.

                The status is deliberately *not* written here. `prepare` sets
                `preparing` itself and, on a rate limit, restores whatever
                status the lead arrived with - so writing `preparing` first
                made "restore" mean "leave it at preparing", and a lead that
                hit a busy provider stayed stuck in that state for ever with
                its Generate button showing. The in-memory set below is enough
                to render the spinner and to stop a second click.
            """
            from pipeline.prepare import LeadPreparer

            if lead["id"] in preparing:
                return
            preparing.add(lead["id"])
            lead_list.refresh()
            ui.notify(f"Researching {lead['company'] or 'the company'} and "
                      "writing your documents. This takes a minute.")
            try:
                pool = state.pool
                preparer = LeadPreparer(
                    store, mail,
                    pool.for_task("score_relevance"),
                    pool.for_task("research"),
                    letter_client=pool.for_task("write_cover_letter"),
                )
                ready = await preparer.prepare_now(lead["id"])
            except Exception as exc:
                log.exception("Could not prepare lead %s", lead["id"])
                # Only reached for a failure raised before `prepare` takes
                # over - building the pool, say. Written to the lead rather
                # than only to a toast, because `prepare` records its own
                # failures there and the card already renders them, so a
                # failure that lands anywhere else vanishes with the
                # notification.
                #
                # Restores the status it arrived with rather than forcing
                # `new`: a `ready` lead being regenerated still has its old
                # documents, and demoting it would hide working downloads.
                mail.set_lead_status(lead["id"], lead["status"], str(exc))
                ready = False
            finally:
                preparing.discard(lead["id"])
                lead_list.refresh()

            if ready:
                ui.notify("Resume and cover letter are ready.", type="positive")
            else:
                current = mail.lead(lead["id"])
                reason = (current["prepare_error"] if current else "") or \
                    "Nothing was written."
                ui.notify(f"Could not prepare this lead: {reason}",
                          type="negative", multi_line=True, close_button=True)

        @ui.refreshable
        def lead_list():
            leads = mail.list_leads(chosen["statuses"], chosen["sort"])
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
                    # Two questions, two orders: "what arrived this morning"
                    # and "which of these is worth a research call".
                    for label, key in (("Newest", LEAD_SORT_POSTED),
                                       ("Best fit", LEAD_SORT_RELEVANCE)):
                        ui.button(
                            label, on_click=lambda k=key: sort_by(k)
                        ).props(
                            ("unelevated" if chosen["sort"] == key else "flat")
                            + " no-caps dense"
                        )
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
            badge(status, STATUS_COLORS.get(status, "#64748b"), padding="2px 10px")

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
                    # The click that used to be a background job. Offered while
                    # a lead has no documents, and again once it has - a
                    # posting whose description has changed, or a profile that
                    # has been edited, is worth rewriting for.
                    if lead["id"] in preparing:
                        ui.spinner(size="sm")
                        ui.label("Generating…").classes("text-xs opacity-70")
                    else:
                        has_documents = bool(mail.selections_for(
                            lead["identity_key"]))
                        ui.button(
                            "Regenerate" if has_documents
                            else "Generate documents",
                            icon="auto_awesome",
                            on_click=lambda l=lead: generate(l),
                        ).props(
                            ("flat" if has_documents else "unelevated")
                            + " no-caps dense"
                        ).tooltip(
                            "Research this company and write a tailored resume "
                            "and cover letter. Costs a model call."
                        )
                    ui.button(
                        "I applied to this", on_click=lambda l=lead: promote(l)
                    ).props("flat no-caps dense")
                    ui.button(
                        "Not interested", on_click=lambda l=lead: dismiss(l)
                    ).props("flat no-caps dense")

        lead_list()
