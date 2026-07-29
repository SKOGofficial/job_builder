"""Add application page: the job intake form."""

import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from pages.base import BasePage
from store import today_iso, url_hash
from theme import JOB_TYPES, PAY_PERIODS, STATUSES


class AddApplicationPage(BasePage):
    name = "add"
    title = "Add a job application"
    subtitle = "Start with the company posting URL. Duplicate URLs are detected before the form is saved."

    def __init__(self, app):
        super().__init__(app)
        self.form_vars = {}
        self.notes = None

    def render(self):
        header = ttk.Frame(self.content, style="TFrame")
        header.pack(fill="x")
        ttk.Label(header, text=self.title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text=self.subtitle, style="Muted.TLabel").pack(anchor="w", pady=(4, 18))

        card = self.card(self.content)
        card.pack(fill="x")

        self.form_vars = {
            "posting_url": tk.StringVar(),
            "position_title": tk.StringVar(),
            "company": tk.StringVar(),
            "job_type": tk.StringVar(value=JOB_TYPES[0]),
            "requires_oa": tk.BooleanVar(),
            "completed_oa": tk.BooleanVar(),
            "received_references": tk.BooleanVar(),
            "payment_amount": tk.StringVar(),
            "payment_period": tk.StringVar(value=PAY_PERIODS[0]),
            "status": tk.StringVar(value="Applied"),
            "application_date": tk.StringVar(value=today_iso()),
            "response_date": tk.StringVar(),
        }

        # Each logical row occupies two grid rows: label then widget. The
        # counter is shared by every widget below so nothing overlaps.
        row = 0
        self.field(card, "Job posting URL", "posting_url", row, width=72)
        ttk.Button(card, text="Check URL", command=self.check_url).grid(
            row=row * 2 + 1, column=2, padx=(12, 0), sticky="ew"
        )
        row += 1

        self.field(card, "Position title", "position_title", row)
        self.field(card, "Company", "company", row, column=2)
        row += 1

        self.combo(card, "Type", "job_type", JOB_TYPES, row)
        self.combo(card, "Status", "status", STATUSES, row, column=2)
        row += 1

        self.field(card, "Payment amount", "payment_amount", row)
        self.combo(card, "Payment period", "payment_period", PAY_PERIODS, row, column=2)
        row += 1

        self.field(card, "Application date", "application_date", row)
        self.field(card, "Response date", "response_date", row, column=2)
        row += 1

        checks = ttk.Frame(card, style="Surface.TFrame")
        checks.grid(row=row * 2, rowspan=2, column=0, columnspan=3, sticky="w", pady=(16, 4))
        for label, key in [
            ("Requires OA", "requires_oa"),
            ("Completed OA", "completed_oa"),
            ("Received references", "received_references"),
        ]:
            ttk.Checkbutton(checks, text=label, variable=self.form_vars[key]).pack(
                side="left", padx=(0, 20)
            )
        row += 1

        ttk.Label(card, text="Notes", style="Surface.TLabel").grid(
            row=row * 2, column=0, sticky="w", pady=(14, 5)
        )
        self.notes = tk.Text(
            card,
            height=5,
            wrap="word",
            bg=self.theme["surface"],
            fg=self.theme["text"],
            insertbackground=self.theme["text"],
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground=self.theme["border"],
        )
        self.notes.grid(row=row * 2 + 1, column=0, columnspan=3, sticky="ew")
        row += 1

        actions = ttk.Frame(card, style="Surface.TFrame")
        actions.grid(row=row * 2, column=0, columnspan=3, sticky="e", pady=(18, 0))
        ttk.Button(actions, text="Clear", command=self.render).pack(side="left", padx=(0, 8))
        ttk.Button(
            actions, text="Save Application", style="Primary.TButton", command=self.save_job
        ).pack(side="left")

        card.columnconfigure(1, weight=1)
        card.columnconfigure(2, weight=1)

    # Field builders --------------------------------------------------------

    def field(self, parent, label, key, row, column=0, width=34):
        ttk.Label(parent, text=label, style="Surface.TLabel").grid(
            row=row * 2, column=column, sticky="w", pady=(8, 4), padx=(0, 12)
        )
        entry = ttk.Entry(parent, textvariable=self.form_vars[key], width=width)
        entry.grid(
            row=row * 2 + 1,
            column=column,
            columnspan=2 if column == 0 else 1,
            sticky="ew",
            padx=(0, 12),
        )
        return entry

    def combo(self, parent, label, key, options, row, column=0, width=None):
        ttk.Label(parent, text=label, style="Surface.TLabel").grid(
            row=row * 2, column=column, sticky="w", pady=(8, 4), padx=(0, 12)
        )
        combo = ttk.Combobox(
            parent, textvariable=self.form_vars[key], values=options, state="readonly", width=width
        )
        combo.grid(
            row=row * 2 + 1,
            column=column,
            columnspan=2 if column == 0 else 1,
            sticky="ew",
            padx=(0, 12),
        )
        return combo

    # Actions ---------------------------------------------------------------

    def check_url(self):
        posting_url = self.form_vars["posting_url"].get().strip()
        if not posting_url:
            messagebox.showinfo("Missing URL", "Enter a job posting URL first.")
            return
        duplicates = self.store.duplicate_jobs(posting_url)
        if not duplicates:
            messagebox.showinfo("No duplicate found", f"Generated Job ID: {url_hash(posting_url)}")
            return
        lines = [f"{row['job_id']} · {row['position_title']} · {row['status']}" for row in duplicates]
        messagebox.showwarning(
            "Duplicate URL detected",
            "This URL already exists in the tracker:\n\n"
            + "\n".join(lines)
            + "\n\nYou can still save if this URL represents a distinct posting.",
        )

    def save_job(self):
        data = {k: v.get() for k, v in self.form_vars.items()}
        data["notes"] = self.notes.get("1.0", "end").strip()
        required = ["posting_url", "position_title", "job_type", "status", "application_date"]
        missing = [name.replace("_", " ") for name in required if not str(data[name]).strip()]
        if missing:
            messagebox.showerror("Missing information", "Please complete: " + ", ".join(missing))
            return
        try:
            datetime.fromisoformat(data["application_date"])
            if data["response_date"]:
                datetime.fromisoformat(data["response_date"])
        except ValueError:
            messagebox.showerror("Invalid date", "Dates should use YYYY-MM-DD.")
            return

        duplicates = self.store.duplicate_jobs(data["posting_url"])
        if duplicates:
            proceed = messagebox.askyesno(
                "Duplicate URL detected",
                "This URL is already tracked. Save as a distinct job posting with a correlated Job ID?",
            )
            if not proceed:
                return

        job_id = self.store.create_job(data)
        messagebox.showinfo("Saved", f"Application saved with Job ID {job_id}.")
        self.show_page("jobs")
