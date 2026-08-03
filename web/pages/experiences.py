"""Experiences: the structured bullets a tailored resume is selected from.

The free-text resume page cannot serve generation. Tailoring means picking and
ordering *specific* bullets against a posting's keywords, and that needs each
bullet as its own row with its own tags - not one blob to re-parse every time.

Tags are the matching surface, so they are worth filling in properly: the
selector scores a bullet against the extracted posting keywords, and an
untagged bullet is effectively invisible to it.
"""

from nicegui import ui

from web.shell import card, page_shell
from web.state import get_state

KINDS = ["work", "project", "education", "volunteering", "award"]

KIND_LABELS = {
    "work": "Work",
    "project": "Projects",
    "education": "Education",
    "volunteering": "Volunteering",
    "award": "Awards",
}


def date_range(row):
    start, end = row["start_date"], row["end_date"]
    if not start and not end:
        return ""
    return f"{start or '?'} – {end or 'present'}"


@ui.page("/experiences")
def experiences_page():
    mail = get_state().mail

    with page_shell(
        "Experiences",
        "Individual resume bullets with tags. Tailored resumes are built by selecting from "
        "these, so each bullet needs to stand on its own.",
        active="/experiences",
    ):

        def add():
            text = (bullet.value or "").strip()
            if not text:
                ui.notify("A bullet needs some text.", type="warning")
                return
            mail.add_experience({
                "kind": kind.value,
                "organisation": (organisation.value or "").strip() or None,
                "role": (role.value or "").strip() or None,
                "start_date": (start.value or "").strip() or None,
                "end_date": (end.value or "").strip() or None,
                "bullet": text,
                "tags": (tags.value or "").strip() or None,
                "impact": (impact.value or "").strip() or None,
            })
            for field in (organisation, role, start, end, bullet, tags, impact):
                field.value = ""
            entry_list.refresh()
            ui.notify("Experience added.", type="positive")

        def remove(experience_id):
            mail.delete_experience(experience_id)
            entry_list.refresh()
            ui.notify("Experience removed.")

        with card():
            ui.label("Add an experience").classes("text-base font-semibold")
            with ui.row().classes("w-full gap-3"):
                kind = ui.select(KINDS, value="work", label="Kind").props(
                    "dense outlined"
                ).classes("w-40")
                organisation = ui.input("Organisation").props("dense outlined").classes("grow")
                role = ui.input("Role").props("dense outlined").classes("grow")
            with ui.row().classes("w-full gap-3"):
                start = ui.input("Start date", placeholder="2024-01").props(
                    "dense outlined"
                ).classes("w-44")
                end = ui.input("End date", placeholder="blank for present").props(
                    "dense outlined"
                ).classes("w-44")
                impact = ui.input("Impact", placeholder="cut build time 40%").props(
                    "dense outlined"
                ).classes("grow")
            bullet = ui.textarea(
                "Bullet", placeholder="Built the ingest pipeline that mirrors and classifies mail."
            ).props("outlined autogrow").classes("w-full")
            tags = ui.input(
                "Tags", placeholder="python, sqlite, async — comma separated"
            ).props("dense outlined").classes("w-full")
            ui.button("Add experience", on_click=add).props(
                "unelevated no-caps"
            ).classes("self-end")

        @ui.refreshable
        def entry_list():
            rows = mail.list_experiences()
            if not rows:
                with card():
                    ui.label(
                        "No experiences stored yet. Resume generation has nothing to select "
                        "from until there are bullets here."
                    ).classes("text-sm opacity-70")
                return

            ui.label(f"{len(rows)} bullet(s)").classes("text-xs opacity-60")
            for kind_name in KINDS:
                group = [row for row in rows if row["kind"] == kind_name]
                if group:
                    kind_section(kind_name, group)

        def kind_section(kind_name, rows):
            with card("p-5"):
                ui.label(KIND_LABELS.get(kind_name, kind_name)).classes(
                    "text-base font-semibold"
                )
                for row in rows:
                    entry_row(row)

        def entry_row(row):
            with ui.column().classes("w-full gap-1 border-t pt-3 first:border-t-0"):
                with ui.row().classes("w-full items-start justify-between gap-3"):
                    with ui.column().classes("gap-1 grow"):
                        heading = " · ".join(
                            part for part in [row["role"], row["organisation"]] if part
                        )
                        if heading:
                            ui.label(heading).classes("text-sm font-medium")
                        span = date_range(row)
                        if span:
                            ui.label(span).classes("text-xs opacity-60")
                    ui.button(
                        icon="delete", on_click=lambda i=row["id"]: remove(i)
                    ).props("flat round dense").tooltip("Remove")

                ui.label(row["bullet"]).classes("text-sm")
                if row["impact"]:
                    ui.label(f"Impact: {row['impact']}").classes("text-xs opacity-70")
                if row["tags"]:
                    with ui.row().classes("items-center gap-1 flex-wrap"):
                        for tag in [t.strip() for t in row["tags"].split(",") if t.strip()]:
                            ui.label(tag).classes(
                                "text-xs px-2 py-0.5 rounded-full bg-black/5 dark:bg-white/10"
                            )

        entry_list()
