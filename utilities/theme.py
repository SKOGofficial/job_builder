"""Domain vocabulary and the colors charts are drawn with.

The ttk style sheet that used to live here went with the Tkinter UI. What
remains is framework-independent: the option lists the forms offer, and the
status colors. Light and dark rendering is now the front end's job.
"""

#: Fixed per status so a chart reads the same in both light and dark mode.
STATUS_COLORS = {
    "Pending": "#eab308",
    "OA Received": "#a855f7",
    "Interview": "#f97316",
    "Offer": "#22c55e",
    "Rejected": "#ef4444",
    "Withdrawn": "#9ca3af",
}

#: Line colour for the cumulative applications chart.
CHART_COLOR = "#0891b2"

#: Brand blue, carried over from the original palette. Quasar's stock primary is
#: a lighter blue that only reaches 3.1:1 against white text; this reaches
#: 5.2:1, so header labels clear WCAG AA rather than washing out.
PRIMARY_COLOR = "#2563eb"

TIME_RANGES = [("7d", 7), ("14d", 14), ("30d", 30), ("90d", 90), ("All time", None)]

JOB_TYPES = ["Internship", "Full time", "Part time", "Contract", "Unpaid"]
STATUSES = ["Pending", "Applied", "OA Received", "Interview", "Offer", "Rejected", "Withdrawn"]
PAY_PERIODS = ["Per hour", "Monthly", "Annual", "Unspecified"]
