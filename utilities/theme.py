"""Palettes, domain vocabulary, and ttk style setup.

Kept separate so pages can read colors and option lists without importing the
application shell.
"""

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

# Fixed per status, so the pie chart reads the same in both themes.
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


def apply_styles(root, theme, theme_name):
    """Configure every ttk style used by the app for the active theme."""
    from tkinter import ttk

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(
        ".",
        background=theme["bg"],
        foreground=theme["text"],
        fieldbackground=theme["surface"],
        font=("Segoe UI", 10),
    )
    style.configure("TFrame", background=theme["bg"], bordercolor=theme["border"])
    style.configure("Surface.TFrame", background=theme["surface"], bordercolor=theme["border"])
    style.configure("TLabel", background=theme["bg"], foreground=theme["text"])
    style.configure("Muted.TLabel", background=theme["bg"], foreground=theme["muted"])
    style.configure("Surface.TLabel", background=theme["surface"], foreground=theme["text"])
    style.configure("MutedSurface.TLabel", background=theme["surface"], foreground=theme["muted"])
    style.configure(
        "Title.TLabel",
        background=theme["bg"],
        foreground=theme["text"],
        font=("Segoe UI Semibold", 22),
    )
    style.configure(
        "CardTitle.TLabel",
        background=theme["surface"],
        foreground=theme["text"],
        font=("Segoe UI Semibold", 14),
    )
    style.configure(
        "TButton",
        background=theme["surface_2"],
        foreground=theme["text"],
        bordercolor=theme["border"],
        focusthickness=0,
        padding=(14, 8),
    )
    style.map(
        "TButton",
        background=[("active", theme["border"])],
        foreground=[("active", theme["text"])],
    )

    on_primary = "#ffffff" if theme_name == "light" else "#08111f"
    style.configure(
        "Primary.TButton",
        background=theme["primary"],
        foreground=on_primary,
        bordercolor=theme["primary"],
    )
    style.map("Primary.TButton", background=[("active", theme["primary_hover"])])

    style.configure(
        "Tab.TButton",
        background=theme["surface"],
        foreground=theme["text"],
        bordercolor=theme["border"],
        relief="flat",
        padding=(14, 10),
    )
    style.map("Tab.TButton", background=[("active", theme["surface_2"])])
    style.configure(
        "ActiveTab.TButton",
        background=theme["bg"],
        foreground=theme["text"],
        bordercolor=theme["border"],
        relief="flat",
        padding=(14, 10),
    )
    style.map("ActiveTab.TButton", background=[("active", theme["surface_2"])])

    style.configure(
        "Range.TButton",
        background=theme["surface_2"],
        foreground=theme["muted"],
        bordercolor=theme["border"],
        relief="flat",
        padding=(10, 4),
        font=("Segoe UI", 8),
    )
    style.map("Range.TButton", background=[("active", theme["surface"])])
    style.configure(
        "RangeActive.TButton",
        background=theme["primary"],
        foreground=on_primary,
        bordercolor=theme["primary"],
        relief="flat",
        padding=(10, 4),
        font=("Segoe UI", 8),
    )
    style.map("RangeActive.TButton", background=[("active", theme["primary_hover"])])

    for widget in ("TEntry", "TCombobox"):
        style.configure(
            widget,
            bordercolor=theme["border"],
            lightcolor=theme["border"],
            darkcolor=theme["border"],
            padding=(8, 8),
        )

    style.configure(
        "Treeview",
        background=theme["surface"],
        foreground=theme["text"],
        fieldbackground=theme["surface"],
        bordercolor=theme["border"],
        rowheight=34,
    )
    style.configure(
        "Treeview.Heading",
        background=theme["surface_2"],
        foreground=theme["text"],
        font=("Segoe UI Semibold", 10),
    )
