"""All jobs: the application table and the per-job detail dialog."""

from nicegui import ui

from utilities.theme import STATUS_COLORS, STATUSES
from web.shell import card, page_shell
from web.state import get_state

COLUMNS = [
    {"name": "job_id", "label": "Job ID", "field": "job_id", "sortable": True, "align": "left"},
    {"name": "position_title", "label": "Position", "field": "position_title",
     "sortable": True, "align": "left"},
    {"name": "company", "label": "Company", "field": "company", "sortable": True, "align": "left"},
    {"name": "job_type", "label": "Type", "field": "job_type", "sortable": True, "align": "left"},
    {"name": "status", "label": "Status", "field": "status", "sortable": True, "align": "left"},
    {"name": "oa", "label": "OA", "field": "oa", "align": "left"},
    {"name": "refs", "label": "References", "field": "refs", "align": "left"},
    {"name": "payment", "label": "Payment", "field": "payment", "align": "left"},
    {"name": "application_date", "label": "Applied", "field": "application_date",
     "sortable": True, "align": "left"},
]

#: Renders the status cell as a coloured chip using the same palette as the
#: dashboard, so a status reads identically wherever it appears.
STATUS_CELL = """
<q-td :props="props">
  <q-badge :style="'background-color: ' + props.row.status_color + '; color: #fff'">
    {{ props.row.status }}
  </q-badge>
</q-td>
"""


def table_row(row):
    oa = "Done" if row["completed_oa"] else ("Required" if row["requires_oa"] else "No")
    payment = " ".join(
        part for part in [row["payment_amount"], row["payment_period"]] if part
    )
    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "position_title": row["position_title"],
        "company": row["company"] or "",
        "job_type": row["job_type"],
        "status": row["status"],
        "status_color": STATUS_COLORS.get(row["status"], "#64748b"),
        "oa": oa,
        "refs": "Yes" if row["received_references"] else "No",
        "payment": payment,
        "application_date": row["application_date"],
        "posting_url": row["posting_url"],
        "notes": row["notes"] or "",
    }


@ui.page("/")
def jobs_page():
    store = get_state().store
    with page_shell(
        "All job postings",
        "Track pending applications, responses, OA progress, references, and offers.",
        active="/",
    ):

        @ui.refreshable
        def table_card():
            rows = [table_row(row) for row in store.list_jobs()]
            with card():
                with ui.row().classes("w-full items-center gap-3"):
                    search = ui.input(placeholder="Search jobs").props(
                        "dense outlined clearable"
                    ).classes("grow")
                    ui.button("Add", icon="add", on_click=lambda: ui.navigate.to("/add")).props(
                        "unelevated no-caps"
                    )
                    ui.button(icon="refresh", on_click=table_card.refresh).props(
                        "flat round dense"
                    ).tooltip("Refresh")

                if not rows:
                    ui.label(
                        "No applications recorded yet. Use Add to create the first one."
                    ).classes("text-sm opacity-70 py-4")
                    return

                table = ui.table(
                    columns=COLUMNS, rows=rows, row_key="job_id", pagination=15
                ).classes("w-full")
                table.add_slot("body-cell-status", STATUS_CELL)
                search.bind_value_to(table, "filter")
                table.on("rowClick", lambda event: open_detail(event.args[1], table_card.refresh))
                ui.label("Click a row to see the posting URL and notes, or update its status.").classes(
                    "text-xs opacity-60"
                )

        table_card()


def open_detail(row, on_change):
    """Show one job's detail, with a status update that closes on save."""
    store = get_state().store
    with ui.dialog() as dialog, ui.card().classes("w-[36rem] max-w-full gap-3 p-6"):
        ui.label(row["position_title"]).classes("text-xl font-semibold")
        ui.link(row["posting_url"], row["posting_url"], new_tab=True).classes(
            "text-xs break-all opacity-70"
        )
        ui.label(f"Job ID: {row['job_id']}").classes("text-xs opacity-70")
        with ui.row().classes("gap-6"):
            ui.label(f"Company: {row['company'] or 'Not specified'}").classes("text-sm")
            ui.label(f"Applied: {row['application_date']}").classes("text-sm")

        ui.label("Notes").classes("text-sm font-medium mt-2")
        ui.markdown(row["notes"] or "_No notes recorded._").classes(
            "text-sm w-full max-h-56 overflow-auto rounded p-3 bg-black/5 dark:bg-white/5"
        )

        with ui.row().classes("w-full items-center justify-between mt-2"):
            status = ui.select(STATUSES, value=row["status"], label="Response status").props(
                "dense outlined"
            ).classes("w-52")

            def save():
                store.update_status(row["id"], status.value)
                dialog.close()
                on_change()
                ui.notify(f"Status set to {status.value}.", type="positive")

            with ui.row().classes("gap-2"):
                ui.button("Close", on_click=dialog.close).props("flat no-caps")
                ui.button("Update", on_click=save).props("unelevated no-caps")
    dialog.open()
