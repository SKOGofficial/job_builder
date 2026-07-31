"""Add application: the job intake form.

Validation matches the Tkinter version exactly, including the duplicate-URL
check that offers to save a correlated Job ID rather than refusing.
"""

from datetime import datetime

from nicegui import ui

from utilities.store import today_iso, url_hash
from utilities.theme import JOB_TYPES, PAY_PERIODS, STATUSES
from web.shell import card, page_shell
from web.state import get_state

REQUIRED = ["posting_url", "position_title", "job_type", "status", "application_date"]


def validation_error(data):
    """Return the first problem with the form, or None when it is good."""
    missing = [name.replace("_", " ") for name in REQUIRED if not str(data[name]).strip()]
    if missing:
        return "Please complete: " + ", ".join(missing)
    try:
        datetime.fromisoformat(data["application_date"])
        if data["response_date"]:
            datetime.fromisoformat(data["response_date"])
    except ValueError:
        return "Dates should use YYYY-MM-DD."
    return None


@ui.page("/add")
def add_page():
    store = get_state().store
    with page_shell(
        "Add a job application",
        "Start with the company posting URL. Duplicate URLs are detected before the form is saved.",
        active="/add",
    ):
        with card():
            with ui.row().classes("w-full items-end gap-3"):
                posting_url = ui.input("Job posting URL").props("dense outlined").classes("grow")
                ui.button("Check URL", on_click=lambda: check_url(posting_url.value)).props(
                    "flat no-caps"
                )

            with ui.row().classes("w-full gap-3"):
                position_title = ui.input("Position title").props("dense outlined").classes("grow")
                company = ui.input("Company").props("dense outlined").classes("grow")

            with ui.row().classes("w-full gap-3"):
                job_type = ui.select(JOB_TYPES, value=JOB_TYPES[0], label="Type").props(
                    "dense outlined"
                ).classes("grow")
                status = ui.select(STATUSES, value="Applied", label="Status").props(
                    "dense outlined"
                ).classes("grow")

            with ui.row().classes("w-full gap-3"):
                payment_amount = ui.input("Payment amount").props("dense outlined").classes("grow")
                payment_period = ui.select(
                    PAY_PERIODS, value=PAY_PERIODS[0], label="Payment period"
                ).props("dense outlined").classes("grow")

            with ui.row().classes("w-full gap-3"):
                application_date = ui.input(
                    "Application date", value=today_iso()
                ).props("dense outlined").classes("grow")
                response_date = ui.input("Response date").props("dense outlined").classes("grow")

            with ui.row().classes("w-full gap-6 py-1"):
                requires_oa = ui.checkbox("Requires OA")
                completed_oa = ui.checkbox("Completed OA")
                received_references = ui.checkbox("Received references")

            notes = ui.textarea("Notes").props("dense outlined autogrow").classes("w-full")

            def collect():
                return {
                    "posting_url": posting_url.value or "",
                    "position_title": position_title.value or "",
                    "company": company.value or "",
                    "job_type": job_type.value,
                    "requires_oa": requires_oa.value,
                    "completed_oa": completed_oa.value,
                    "received_references": received_references.value,
                    "payment_amount": payment_amount.value or "",
                    "payment_period": payment_period.value,
                    "status": status.value,
                    "application_date": application_date.value or "",
                    "response_date": response_date.value or "",
                    "notes": notes.value or "",
                }

            def clear():
                for field in (
                    posting_url, position_title, company, payment_amount, response_date, notes
                ):
                    field.value = ""
                job_type.value = JOB_TYPES[0]
                payment_period.value = PAY_PERIODS[0]
                status.value = "Applied"
                application_date.value = today_iso()
                for box in (requires_oa, completed_oa, received_references):
                    box.value = False

            async def save():
                data = collect()
                problem = validation_error(data)
                if problem:
                    ui.notify(problem, type="negative")
                    return
                duplicates = store.duplicate_jobs(data["posting_url"])
                if duplicates and not await confirm_duplicate(duplicates):
                    return
                job_id = store.create_job(data)
                ui.notify(f"Saved with Job ID {job_id}.", type="positive")
                clear()
                ui.navigate.to("/")

            with ui.row().classes("w-full justify-end gap-2 pt-2"):
                ui.button("Clear", on_click=clear).props("flat no-caps")
                ui.button("Save application", on_click=save).props("unelevated no-caps")


def check_url(posting_url):
    store = get_state().store
    posting_url = (posting_url or "").strip()
    if not posting_url:
        ui.notify("Enter a job posting URL first.", type="warning")
        return
    duplicates = store.duplicate_jobs(posting_url)
    if not duplicates:
        ui.notify(f"No duplicate found. Job ID would be {url_hash(posting_url)}.", type="positive")
        return
    listed = "\n".join(
        f"- {row['job_id']} · {row['position_title']} · {row['status']}" for row in duplicates
    )
    with ui.dialog() as dialog, ui.card().classes("gap-3 p-6"):
        ui.label("Duplicate URL detected").classes("text-lg font-semibold")
        ui.markdown(f"This URL is already tracked:\n\n{listed}").classes("text-sm")
        ui.label(
            "You can still save if this URL represents a distinct posting."
        ).classes("text-sm opacity-70")
        ui.button("Close", on_click=dialog.close).props("flat no-caps").classes("self-end")
    dialog.open()


async def confirm_duplicate(duplicates):
    """Ask before saving over a URL that is already tracked."""
    listed = "\n".join(
        f"- {row['job_id']} · {row['position_title']} · {row['status']}" for row in duplicates
    )
    with ui.dialog() as dialog, ui.card().classes("gap-3 p-6"):
        ui.label("Duplicate URL detected").classes("text-lg font-semibold")
        ui.markdown(f"This URL is already tracked:\n\n{listed}").classes("text-sm")
        ui.label(
            "Save as a distinct posting with a correlated Job ID?"
        ).classes("text-sm opacity-70")
        with ui.row().classes("justify-end gap-2 w-full"):
            ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat no-caps")
            ui.button("Save anyway", on_click=lambda: dialog.submit(True)).props(
                "unelevated no-caps"
            )
    return await dialog
