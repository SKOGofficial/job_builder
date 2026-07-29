"""All jobs page: the application table and the per-job detail dialog."""

import tkinter as tk
from tkinter import ttk

from pages.base import BasePage
from utilities.theme import STATUSES

COLUMNS = ("job_id", "title", "company", "type", "status", "oa", "refs", "payment", "date")

HEADINGS = {
    "job_id": "Job ID",
    "title": "Position",
    "company": "Company",
    "type": "Type",
    "status": "Status",
    "oa": "OA",
    "refs": "References",
    "payment": "Payment",
    "date": "Applied",
}

WIDTHS = {
    "job_id": 110,
    "title": 220,
    "company": 150,
    "type": 100,
    "status": 110,
    "oa": 110,
    "refs": 95,
    "payment": 130,
    "date": 95,
}


class AllJobsPage(BasePage):
    name = "jobs"
    title = "All job postings"
    subtitle = "Track pending applications, responses, OA progress, references, and offers."

    def render(self):
        ttk.Label(self.content, text=self.title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(self.content, text=self.subtitle, style="Muted.TLabel").pack(
            anchor="w", pady=(4, 14)
        )

        toolbar = ttk.Frame(self.content, style="TFrame")
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Button(toolbar, text="Refresh", command=lambda: self.show_page("jobs")).pack(side="left")
        ttk.Button(
            toolbar, text="+ Add", style="Primary.TButton", command=lambda: self.show_page("add")
        ).pack(side="left", padx=8)

        tree = ttk.Treeview(self.content, columns=COLUMNS, show="headings", selectmode="browse")
        for col in COLUMNS:
            tree.heading(col, text=HEADINGS[col])
            tree.column(col, width=WIDTHS[col], anchor="w")
        tree.pack(fill="both", expand=True)

        row_lookup = {}
        for row in self.store.list_jobs():
            oa = "Done" if row["completed_oa"] else ("Required" if row["requires_oa"] else "No")
            refs = "Yes" if row["received_references"] else "No"
            payment = " ".join(
                part for part in [row["payment_amount"], row["payment_period"]] if part
            )
            item = tree.insert(
                "",
                "end",
                values=(
                    row["job_id"],
                    row["position_title"],
                    row["company"] or "",
                    row["job_type"],
                    row["status"],
                    oa,
                    refs,
                    payment,
                    row["application_date"],
                ),
            )
            row_lookup[item] = row

        def open_detail(_event):
            selection = tree.selection()
            if selection:
                self.show_job_detail(row_lookup[selection[0]])

        tree.bind("<Double-1>", open_detail)

        ttk.Label(
            self.content,
            text="Double-click a row to inspect the posting URL, notes, and update the response status.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(10, 0))

    def show_job_detail(self, row):
        win = tk.Toplevel(self.app)
        win.title(row["position_title"])
        win.geometry("620x430")
        win.configure(bg=self.theme["bg"])
        frame = self.card(win)
        frame.pack(fill="both", expand=True, padx=18, pady=18)

        ttk.Label(frame, text=row["position_title"], style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text=f"{row['job_id']} · {row['posting_url']}",
            style="MutedSurface.TLabel",
            wraplength=560,
        ).pack(anchor="w", pady=(4, 12))
        ttk.Label(
            frame, text=f"Company: {row['company'] or 'Not specified'}", style="Surface.TLabel"
        ).pack(anchor="w")
        ttk.Label(
            frame, text=f"Applied: {row['application_date']}", style="Surface.TLabel"
        ).pack(anchor="w", pady=(3, 0))
        ttk.Label(frame, text="Notes", style="Surface.TLabel").pack(anchor="w", pady=(14, 4))
        notes = tk.Text(
            frame,
            height=8,
            wrap="word",
            bg=self.theme["surface"],
            fg=self.theme["text"],
            relief="solid",
            bd=1,
        )
        notes.insert("1.0", row["notes"] or "")
        notes.configure(state="disabled")
        notes.pack(fill="both", expand=True)

        status_var = tk.StringVar(value=row["status"])
        status_frame = ttk.Frame(frame, style="Surface.TFrame")
        status_frame.pack(fill="x", pady=(16, 0))
        ttk.Label(status_frame, text="Response status", style="Surface.TLabel").pack(side="left")
        ttk.Combobox(
            status_frame, textvariable=status_var, values=STATUSES, state="readonly", width=18
        ).pack(side="left", padx=10)
        ttk.Button(
            status_frame,
            text="Update",
            style="Primary.TButton",
            command=lambda: self.update_status_and_close(row["id"], status_var.get(), win),
        ).pack(side="right")

    def update_status_and_close(self, row_id, status, window):
        self.store.update_status(row_id, status)
        window.destroy()
        self.show_page("jobs")
