import hashlib
import sqlite3
import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import messagebox, ttk
from urllib.parse import urlparse, urlunparse


DB_PATH = "job_applications.sqlite3"


THEMES = {
    "light": {
        "bg": "#f7f8fb",
        "surface": "#ffffff",
        "surface_2": "#edf1f7",
        "text": "#17202a",
        "muted": "#64748b",
        "border": "#d7dee8",
        "primary": "#2563eb",
        "primary_hover": "#1d4ed8",
        "accent": "#0f766e",
        "warning": "#b45309",
        "danger": "#b91c1c",
        "chart": "#0891b2",
    },
    "dark": {
        "bg": "#101318",
        "surface": "#171c24",
        "surface_2": "#202735",
        "text": "#eff4fb",
        "muted": "#a7b3c4",
        "border": "#303948",
        "primary": "#60a5fa",
        "primary_hover": "#93c5fd",
        "accent": "#2dd4bf",
        "warning": "#fbbf24",
        "danger": "#f87171",
        "chart": "#22d3ee",
    },
}


STATUS_COLORS = {
    "Pending": "#eab308",
    "OA Received": "#a855f7",
    "Interview": "#f97316",
    "Offer": "#22c55e",
    "Rejected": "#ef4444",
    "Withdrawn": "#9ca3af",
}

TIME_RANGES = [("7d", 7), ("14d", 14), ("30d", 30), ("90d", 90), ("All time", None)]

JOB_TYPES = ["Internship", "Full time", "Part time", "Contract", "Unpaid"]
STATUSES = ["Pending", "Applied", "OA Received", "Interview", "Offer", "Rejected", "Withdrawn"]
PAY_PERIODS = ["Per hour", "Monthly", "Annual", "Unspecified"]


def normalize_url(url):
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        parsed = urlparse(f"https://{url.strip()}")
    hostname = (parsed.hostname or "").lower()
    netloc = hostname
    if parsed.port:
        netloc = f"{hostname}:{parsed.port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), netloc, path, "", parsed.query, ""))


def url_hash(url):
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:12].upper()


def today_iso():
    return date.today().isoformat()


class JobStore:
    def __init__(self, db_path=DB_PATH):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.init_db()

    def init_db(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL UNIQUE,
                url_hash TEXT NOT NULL,
                posting_url TEXT NOT NULL,
                position_title TEXT NOT NULL,
                company TEXT,
                job_type TEXT NOT NULL,
                requires_oa INTEGER NOT NULL DEFAULT 0,
                completed_oa INTEGER NOT NULL DEFAULT 0,
                received_references INTEGER NOT NULL DEFAULT 0,
                payment_amount TEXT,
                payment_period TEXT,
                status TEXT NOT NULL,
                application_date TEXT NOT NULL,
                response_date TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS profile (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_url_hash ON jobs(url_hash);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_application_date ON jobs(application_date);
            """
        )
        self.conn.commit()

    def duplicate_jobs(self, posting_url):
        h = url_hash(posting_url)
        return self.conn.execute(
            "SELECT * FROM jobs WHERE url_hash = ? ORDER BY created_at DESC", (h,)
        ).fetchall()

    def next_job_id(self, posting_url):
        base = url_hash(posting_url)
        rows = self.conn.execute(
            "SELECT job_id FROM jobs WHERE url_hash = ? ORDER BY job_id", (base,)
        ).fetchall()
        if not rows:
            return base
        return f"{base}-{len(rows) + 1}"

    def create_job(self, data):
        now = datetime.now().isoformat(timespec="seconds")
        job_id = self.next_job_id(data["posting_url"])
        self.conn.execute(
            """
            INSERT INTO jobs (
                job_id, url_hash, posting_url, position_title, company, job_type,
                requires_oa, completed_oa, received_references, payment_amount,
                payment_period, status, application_date, response_date, notes,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                url_hash(data["posting_url"]),
                normalize_url(data["posting_url"]),
                data["position_title"],
                data["company"],
                data["job_type"],
                int(data["requires_oa"]),
                int(data["completed_oa"]),
                int(data["received_references"]),
                data["payment_amount"],
                data["payment_period"],
                data["status"],
                data["application_date"],
                data["response_date"],
                data["notes"],
                now,
                now,
            ),
        )
        self.conn.commit()
        return job_id

    def update_status(self, row_id, status):
        now = datetime.now().isoformat(timespec="seconds")
        response_date = today_iso() if status in {"Interview", "Offer", "Rejected"} else None
        self.conn.execute(
            """
            UPDATE jobs
            SET status = ?,
                response_date = COALESCE(response_date, ?),
                updated_at = ?
            WHERE id = ?
            """,
            (status, response_date, now, row_id),
        )
        self.conn.commit()

    def list_jobs(self):
        return self.conn.execute(
            "SELECT * FROM jobs ORDER BY application_date DESC, created_at DESC"
        ).fetchall()

    def stats(self):
        rows = self.list_jobs()
        total = len(rows)
        heard_back = sum(1 for r in rows if r["status"] in {"OA Received", "Interview", "Offer", "Rejected"})
        offers = sum(1 for r in rows if r["status"] == "Offer")
        pending = sum(1 for r in rows if r["status"] in {"Pending", "Applied"})
        return {"total": total, "heard_back": heard_back, "offers": offers, "pending": pending}

    def daily_counts(self, days=14):
        if days is None:
            earliest = self.conn.execute(
                "SELECT MIN(application_date) AS earliest FROM jobs"
            ).fetchone()["earliest"]
            start = date.fromisoformat(earliest) if earliest else date.today()
            days = (date.today() - start).days + 1
        else:
            start = date.today() - timedelta(days=days - 1)
        labels = [(start + timedelta(days=i)).isoformat() for i in range(days)]
        counts = dict.fromkeys(labels, 0)
        rows = self.conn.execute(
            """
            SELECT application_date, COUNT(*) AS count
            FROM jobs
            WHERE application_date >= ?
            GROUP BY application_date
            """,
            (start.isoformat(),),
        ).fetchall()
        for row in rows:
            counts[row["application_date"]] = row["count"]
        return list(counts.items())

    def cumulative_counts(self, days=14):
        daily = self.daily_counts(days)
        if not daily:
            return daily
        base = self.conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE application_date < ?", (daily[0][0],)
        ).fetchone()["count"]
        result = []
        running = base
        for day, count in daily:
            running += count
            result.append((day, running))
        return result

    def status_counts(self):
        rows = self.list_jobs()
        buckets = {
            "Pending": 0,
            "OA Received": 0,
            "Interview": 0,
            "Offer": 0,
            "Rejected": 0,
            "Withdrawn": 0,
        }
        for row in rows:
            status = row["status"]
            if status in {"Pending", "Applied"}:
                buckets["Pending"] += 1
            elif status in buckets:
                buckets[status] += 1
        return buckets

    def save_profile_value(self, key, value):
        self.conn.execute(
            """
            INSERT INTO profile (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def get_profile_value(self, key, default=""):
        row = self.conn.execute("SELECT value FROM profile WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


class JobTrackerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Job Board Tracker")
        self.geometry("1180x760")
        self.minsize(980, 640)
        self.store = JobStore()
        self.theme_name = self.store.get_profile_value("theme", "light")
        self.theme = THEMES[self.theme_name]
        self.active_page = "add"
        self.dashboard_range_days = 14
        self.drawer_visible = False
        self.form_vars = {}
        self.configure(bg=self.theme["bg"])
        self.setup_styles()
        self.build_shell()
        self.show_page("add")

    def setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            ".",
            background=self.theme["bg"],
            foreground=self.theme["text"],
            fieldbackground=self.theme["surface"],
            font=("Segoe UI", 10),
        )
        style.configure(
            "TFrame",
            background=self.theme["bg"],
            bordercolor=self.theme["border"],
        )
        style.configure(
            "Surface.TFrame",
            background=self.theme["surface"],
            bordercolor=self.theme["border"],
        )
        style.configure(
            "TLabel",
            background=self.theme["bg"],
            foreground=self.theme["text"],
        )
        style.configure(
            "Muted.TLabel",
            background=self.theme["bg"],
            foreground=self.theme["muted"],
        )
        style.configure(
            "Surface.TLabel",
            background=self.theme["surface"],
            foreground=self.theme["text"],
        )
        style.configure(
            "MutedSurface.TLabel",
            background=self.theme["surface"],
            foreground=self.theme["muted"],
        )
        style.configure(
            "Title.TLabel",
            background=self.theme["bg"],
            foreground=self.theme["text"],
            font=("Segoe UI Semibold", 22),
        )
        style.configure(
            "CardTitle.TLabel",
            background=self.theme["surface"],
            foreground=self.theme["text"],
            font=("Segoe UI Semibold", 14),
        )
        style.configure(
            "TButton",
            background=self.theme["surface_2"],
            foreground=self.theme["text"],
            bordercolor=self.theme["border"],
            focusthickness=0,
            padding=(14, 8),
        )
        style.map(
            "TButton",
            background=[("active", self.theme["border"])],
            foreground=[("active", self.theme["text"])],
        )
        style.configure(
            "Primary.TButton",
            background=self.theme["primary"],
            foreground="#ffffff" if self.theme_name == "light" else "#08111f",
            bordercolor=self.theme["primary"],
        )
        style.map(
            "Primary.TButton",
            background=[("active", self.theme["primary_hover"])],
        )
        style.configure(
            "Tab.TButton",
            background=self.theme["surface"],
            foreground=self.theme["text"],
            bordercolor=self.theme["border"],
            relief="flat",
            padding=(14, 10),
        )
        style.map(
            "Tab.TButton",
            background=[("active", self.theme["surface_2"])],
        )
        style.configure(
            "ActiveTab.TButton",
            background=self.theme["bg"],
            foreground=self.theme["text"],
            bordercolor=self.theme["border"],
            relief="flat",
            padding=(14, 10),
        )
        style.map(
            "ActiveTab.TButton",
            background=[("active", self.theme["surface_2"])],
        )
        style.configure(
            "Range.TButton",
            background=self.theme["surface_2"],
            foreground=self.theme["muted"],
            bordercolor=self.theme["border"],
            relief="flat",
            padding=(10, 4),
            font=("Segoe UI", 8),
        )
        style.map(
            "Range.TButton",
            background=[("active", self.theme["surface"])],
        )
        style.configure(
            "RangeActive.TButton",
            background=self.theme["primary"],
            foreground="#ffffff" if self.theme_name == "light" else "#08111f",
            bordercolor=self.theme["primary"],
            relief="flat",
            padding=(10, 4),
            font=("Segoe UI", 8),
        )
        style.map(
            "RangeActive.TButton",
            background=[("active", self.theme["primary_hover"])],
        )
        style.configure(
            "TEntry",
            bordercolor=self.theme["border"],
            lightcolor=self.theme["border"],
            darkcolor=self.theme["border"],
            padding=(8, 8),
        )
        style.configure(
            "TCombobox",
            bordercolor=self.theme["border"],
            lightcolor=self.theme["border"],
            darkcolor=self.theme["border"],
            padding=(8, 8),
        )
        style.configure(
            "Treeview",
            background=self.theme["surface"],
            foreground=self.theme["text"],
            fieldbackground=self.theme["surface"],
            bordercolor=self.theme["border"],
            rowheight=34,
        )
        style.configure(
            "Treeview.Heading",
            background=self.theme["surface_2"],
            foreground=self.theme["text"],
            font=("Segoe UI Semibold", 10),
        )

    def build_shell(self):
        self.topbar = ttk.Frame(self, padding=(18, 14), style="TFrame")
        self.topbar.pack(fill="x")

        ttk.Button(self.topbar, text="☰", width=3, command=self.toggle_drawer).pack(side="left")
        ttk.Label(self.topbar, text="Job Board Tracker", style="Title.TLabel").pack(side="left", padx=(16, 0))
        ttk.Label(
            self.topbar,
            text="SQLite-backed application history",
            style="Muted.TLabel",
        ).pack(side="left", padx=(14, 0), pady=(7, 0))
        ttk.Button(self.topbar, text="Dark / Light", command=self.toggle_theme).pack(side="right")

        self.body = ttk.Frame(self, style="TFrame")
        self.body.pack(fill="both", expand=True)

        self.drawer = ttk.Frame(self.body, padding=(16, 18), style="Surface.TFrame")
        self.main = ttk.Frame(self.body, padding=(22, 10, 22, 24), style="TFrame")
        self.main.pack(side="left", fill="both", expand=True)

        self.nav = ttk.Frame(self.main, style="TFrame")
        self.nav.pack(fill="x", pady=(0, 18))
        self.nav_buttons = {}
        tabs = [
            ("All jobs", "jobs"),
            ("Add application", "add"),
            ("Dashboard", "dashboard"),
        ]
        for index, (label, page) in enumerate(tabs):
            button_style = "ActiveTab.TButton" if page == self.active_page else "Tab.TButton"
            self.nav_buttons[page] = ttk.Button(
                self.nav,
                text=label,
                style=button_style,
                command=lambda p=page: self.show_page(p),
            )
            self.nav_buttons[page].pack(side="left", padx=(0 if index == 0 else 6, 0))

        self.content = ttk.Frame(self.main, style="TFrame")
        self.content.pack(fill="both", expand=True)

    def toggle_drawer(self):
        self.drawer_visible = not self.drawer_visible
        if self.drawer_visible:
            self.drawer.pack(side="left", fill="y", before=self.main)
            self.render_drawer()
        else:
            self.drawer.pack_forget()

    def render_drawer(self):
        self.clear(self.drawer)
        ttk.Label(self.drawer, text="Menu", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 14))
        entries = [
            ("Settings", "settings"),
            ("Profile", "profile"),
            ("Resume & Experiences", "resume"),
        ]
        for label, page in entries:
            ttk.Button(self.drawer, text=label, command=lambda p=page: self.show_page(p)).pack(
                fill="x", pady=5
            )
        ttk.Label(
            self.drawer,
            text="These sections store local profile context for future automation features.",
            style="MutedSurface.TLabel",
            wraplength=190,
        ).pack(anchor="w", pady=(22, 0))

    def toggle_theme(self):
        self.theme_name = "dark" if self.theme_name == "light" else "light"
        self.theme = THEMES[self.theme_name]
        self.store.save_profile_value("theme", self.theme_name)
        for child in self.winfo_children():
            child.destroy()
        self.configure(bg=self.theme["bg"])
        self.setup_styles()
        self.build_shell()
        if self.drawer_visible:
            self.drawer_visible = False
            self.toggle_drawer()
        self.show_page(self.active_page)

    def update_nav_tabs(self):
        if hasattr(self, "nav_buttons"):
            for page, button in self.nav_buttons.items():
                button.configure(
                    style="ActiveTab.TButton" if page == self.active_page else "Tab.TButton"
                )

    def show_page(self, page):
        self.active_page = page
        self.update_nav_tabs()
        self.clear(self.content)
        self.content.update_idletasks()
        if page == "add":
            self.render_add_page()
        elif page == "jobs":
            self.render_jobs_page()
        elif page == "dashboard":
            self.render_dashboard_page()
        elif page == "settings":
            self.render_settings_page()
        elif page == "profile":
            self.render_profile_page()
        elif page == "resume":
            self.render_resume_page()

    def clear(self, frame):
        for child in frame.winfo_children():
            child.destroy()

    def card(self, parent, padding=(22, 20)):
        frame = ttk.Frame(parent, padding=padding, style="Surface.TFrame")
        frame.configure(borderwidth=1, relief="solid")
        return frame

    def render_add_page(self):
        header = ttk.Frame(self.content, style="TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="Add a job application", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Start with the company posting URL. Duplicate URLs are detected before the form is saved.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 18))

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
            ttk.Checkbutton(checks, text=label, variable=self.form_vars[key]).pack(side="left", padx=(0, 20))
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
        ttk.Button(actions, text="Clear", command=self.render_add_page).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Save Application", style="Primary.TButton", command=self.save_job).pack(side="left")

        card.columnconfigure(1, weight=1)
        card.columnconfigure(2, weight=1)

    def field(self, parent, label, key, row, column=0, width=34):
        ttk.Label(parent, text=label, style="Surface.TLabel").grid(
            row=row * 2, column=column, sticky="w", pady=(8, 4), padx=(0, 12)
        )
        entry = ttk.Entry(parent, textvariable=self.form_vars[key], width=width)
        entry.grid(row=row * 2 + 1, column=column, columnspan=2 if column == 0 else 1, sticky="ew", padx=(0, 12))
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

    def render_jobs_page(self):
        ttk.Label(self.content, text="All job postings", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            self.content,
            text="Track pending applications, responses, OA progress, references, and offers.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 14))

        toolbar = ttk.Frame(self.content, style="TFrame")
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Button(toolbar, text="Refresh", command=lambda: self.show_page("jobs")).pack(side="left")
        ttk.Button(toolbar, text="+ Add", style="Primary.TButton", command=lambda: self.show_page("add")).pack(
            side="left", padx=8
        )

        columns = ("job_id", "title", "company", "type", "status", "oa", "refs", "payment", "date")
        tree = ttk.Treeview(self.content, columns=columns, show="headings", selectmode="browse")
        headings = {
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
        widths = {
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
        for col in columns:
            tree.heading(col, text=headings[col])
            tree.column(col, width=widths[col], anchor="w")
        tree.pack(fill="both", expand=True)

        rows = self.store.list_jobs()
        row_lookup = {}
        for row in rows:
            oa = "Done" if row["completed_oa"] else ("Required" if row["requires_oa"] else "No")
            refs = "Yes" if row["received_references"] else "No"
            payment = " ".join(part for part in [row["payment_amount"], row["payment_period"]] if part)
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
        win = tk.Toplevel(self)
        win.title(row["position_title"])
        win.geometry("620x430")
        win.configure(bg=self.theme["bg"])
        frame = self.card(win)
        frame.pack(fill="both", expand=True, padx=18, pady=18)

        ttk.Label(frame, text=row["position_title"], style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(frame, text=f"{row['job_id']} · {row['posting_url']}", style="MutedSurface.TLabel", wraplength=560).pack(
            anchor="w", pady=(4, 12)
        )
        ttk.Label(frame, text=f"Company: {row['company'] or 'Not specified'}", style="Surface.TLabel").pack(anchor="w")
        ttk.Label(frame, text=f"Applied: {row['application_date']}", style="Surface.TLabel").pack(anchor="w", pady=(3, 0))
        ttk.Label(frame, text="Notes", style="Surface.TLabel").pack(anchor="w", pady=(14, 4))
        notes = tk.Text(frame, height=8, wrap="word", bg=self.theme["surface"], fg=self.theme["text"], relief="solid", bd=1)
        notes.insert("1.0", row["notes"] or "")
        notes.configure(state="disabled")
        notes.pack(fill="both", expand=True)

        status_var = tk.StringVar(value=row["status"])
        status_frame = ttk.Frame(frame, style="Surface.TFrame")
        status_frame.pack(fill="x", pady=(16, 0))
        ttk.Label(status_frame, text="Response status", style="Surface.TLabel").pack(side="left")
        ttk.Combobox(status_frame, textvariable=status_var, values=STATUSES, state="readonly", width=18).pack(
            side="left", padx=10
        )
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

    def render_dashboard_page(self):
        ttk.Label(self.content, text="Application dashboard", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            self.content,
            text="Aggregate progress across applications, responses, offers, and daily submission volume.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 16))

        stats = self.store.stats()
        stats_frame = ttk.Frame(self.content, style="TFrame")
        stats_frame.pack(fill="x", pady=(0, 18))
        for label, key in [
            ("Jobs applied", "total"),
            ("Heard back", "heard_back"),
            ("Offers received", "offers"),
            ("Pending", "pending"),
        ]:
            box = self.card(stats_frame, padding=(18, 16))
            box.pack(side="left", fill="x", expand=True, padx=(0, 12))
            ttk.Label(box, text=str(stats[key]), style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(box, text=label, style="MutedSurface.TLabel").pack(anchor="w", pady=(4, 0))

        charts_row = ttk.Frame(self.content, style="TFrame")
        charts_row.pack(fill="both", expand=True)

        chart_card = self.card(charts_row)
        chart_card.pack(side="left", fill="both", expand=True, padx=(0, 12))
        chart_header = ttk.Frame(chart_card, style="Surface.TFrame")
        chart_header.pack(fill="x")
        ttk.Label(chart_header, text="Total applications over time", style="CardTitle.TLabel").pack(side="left")
        range_frame = ttk.Frame(chart_header, style="Surface.TFrame")
        range_frame.pack(side="right")
        for label, days in TIME_RANGES:
            style = "RangeActive.TButton" if days == self.dashboard_range_days else "Range.TButton"
            ttk.Button(
                range_frame,
                text=label,
                style=style,
                command=lambda d=days: self.set_dashboard_range(d),
            ).pack(side="left", padx=(6, 0))
        canvas = tk.Canvas(
            chart_card,
            height=300,
            bg=self.theme["surface"],
            highlightthickness=0,
        )
        canvas.pack(fill="both", expand=True, pady=(14, 0))
        canvas.bind("<Configure>", lambda event: self.draw_line_chart(canvas))
        self.draw_line_chart(canvas)

        pie_card = self.card(charts_row)
        pie_card.pack(side="left", fill="both", expand=True)
        ttk.Label(pie_card, text="Applications by status", style="CardTitle.TLabel").pack(anchor="w")
        pie_body = ttk.Frame(pie_card, style="Surface.TFrame")
        pie_body.pack(fill="both", expand=True, pady=(14, 0))
        pie_canvas = tk.Canvas(
            pie_body,
            width=220,
            height=220,
            bg=self.theme["surface"],
            highlightthickness=0,
        )
        pie_canvas.pack(side="left", fill="y")
        legend = ttk.Frame(pie_body, style="Surface.TFrame")
        legend.pack(side="left", fill="both", expand=True, padx=(18, 0))
        pie_canvas.bind("<Configure>", lambda event: self.draw_status_pie(pie_canvas))
        self.draw_status_pie(pie_canvas)
        self.render_status_legend(legend)

    def set_dashboard_range(self, days):
        self.dashboard_range_days = days
        self.show_page("dashboard")

    def draw_line_chart(self, canvas):
        canvas.delete("all")
        data = self.store.cumulative_counts(self.dashboard_range_days)
        width = max(canvas.winfo_width(), 640)
        height = max(canvas.winfo_height(), 280)
        pad_x, pad_y = 46, 36
        chart_w = width - pad_x * 2
        chart_h = height - pad_y * 2
        max_count = max([count for _, count in data] + [1])
        axis_color = self.theme["border"]
        text_color = self.theme["muted"]
        canvas.create_line(pad_x, height - pad_y, width - pad_x, height - pad_y, fill=axis_color)
        canvas.create_line(pad_x, pad_y, pad_x, height - pad_y, fill=axis_color)
        step = chart_w / max(len(data) - 1, 1)
        label_stride = max(1, len(data) // 12)
        points = []
        for index, (day, count) in enumerate(data):
            x = pad_x + index * step
            y = height - pad_y - (count / max_count * chart_h)
            points.append((x, y))
            if index % label_stride == 0 or index == len(data) - 1:
                label = datetime.fromisoformat(day).strftime("%m/%d")
                canvas.create_text(x, height - 15, text=label, fill=text_color, font=("Segoe UI", 8))
        if len(points) > 1:
            canvas.create_line(*[coord for point in points for coord in point], fill=self.theme["chart"], width=2, smooth=True)
        for index, ((x, y), (_, count)) in enumerate(zip(points, data)):
            radius = 3
            canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=self.theme["chart"], outline="")
            if index == len(points) - 1:
                canvas.create_text(x, y - 12, text=str(count), fill=self.theme["text"], font=("Segoe UI", 9))

    def draw_status_pie(self, canvas):
        canvas.delete("all")
        buckets = self.store.status_counts()
        total = sum(buckets.values())
        size = min(max(canvas.winfo_width(), 180), max(canvas.winfo_height(), 180))
        pad = 10
        x0, y0, x1, y1 = pad, pad, size - pad, size - pad
        if total == 0:
            canvas.create_oval(x0, y0, x1, y1, outline=self.theme["border"], width=1)
            canvas.create_text(
                size / 2, size / 2, text="No data", fill=self.theme["muted"], font=("Segoe UI", 9)
            )
            return
        start_angle = 90.0
        for status, count in buckets.items():
            if not count:
                continue
            extent = -360.0 * (count / total)
            canvas.create_arc(
                x0, y0, x1, y1,
                start=start_angle,
                extent=extent,
                fill=STATUS_COLORS[status],
                outline=self.theme["surface"],
                width=2,
                style="pieslice",
            )
            start_angle += extent

    def render_status_legend(self, legend):
        buckets = self.store.status_counts()
        total = sum(buckets.values())
        for status, count in buckets.items():
            row = ttk.Frame(legend, style="Surface.TFrame")
            row.pack(fill="x", pady=3)
            swatch = tk.Canvas(row, width=12, height=12, highlightthickness=0, bg=self.theme["surface"])
            swatch.pack(side="left")
            swatch.create_rectangle(0, 0, 12, 12, fill=STATUS_COLORS[status], outline="")
            pct = f"{(count / total * 100):.0f}%" if total else "0%"
            ttk.Label(
                row, text=f"{status} — {count} ({pct})", style="MutedSurface.TLabel"
            ).pack(side="left", padx=(8, 0))

    def render_settings_page(self):
        ttk.Label(self.content, text="Settings", style="Title.TLabel").pack(anchor="w")
        card = self.card(self.content)
        card.pack(fill="x", pady=(16, 0))
        ttk.Label(card, text="Appearance", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(card, text=f"Current theme: {self.theme_name.title()}", style="MutedSurface.TLabel").pack(
            anchor="w", pady=(5, 12)
        )
        ttk.Button(card, text="Toggle Dark / Light", style="Primary.TButton", command=self.toggle_theme).pack(anchor="w")

    def render_profile_page(self):
        self.render_text_storage_page(
            title="Profile",
            key="profile_text",
            hint="Store contact details, target roles, location preferences, and search notes.",
        )

    def render_resume_page(self):
        self.render_text_storage_page(
            title="Resume & Experiences",
            key="resume_text",
            hint="Store experience bullets, project details, resume notes, and CV context for future resume builder work.",
        )

    def render_text_storage_page(self, title, key, hint):
        ttk.Label(self.content, text=title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(self.content, text=hint, style="Muted.TLabel").pack(anchor="w", pady=(4, 14))
        card = self.card(self.content)
        card.pack(fill="both", expand=True)
        text = tk.Text(
            card,
            wrap="word",
            bg=self.theme["surface"],
            fg=self.theme["text"],
            insertbackground=self.theme["text"],
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground=self.theme["border"],
            font=("Segoe UI", 10),
        )
        text.insert("1.0", self.store.get_profile_value(key, ""))
        text.pack(fill="both", expand=True)
        ttk.Button(
            card,
            text="Save",
            style="Primary.TButton",
            command=lambda: self.save_text_value(key, text.get("1.0", "end").strip()),
        ).pack(anchor="e", pady=(14, 0))

    def save_text_value(self, key, value):
        self.store.save_profile_value(key, value)
        messagebox.showinfo("Saved", "Your information was saved locally.")


if __name__ == "__main__":
    app = JobTrackerApp()
    app.mainloop()
