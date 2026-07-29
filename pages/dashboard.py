"""Dashboard page: summary tiles, cumulative line chart, and status pie chart.

Charts are drawn directly on a tkinter Canvas so the tracker keeps working
without any plotting dependency.
"""

import tkinter as tk
from datetime import datetime
from tkinter import ttk

from pages.base import BasePage
from utilities.theme import STATUS_COLORS, TIME_RANGES

STAT_TILES = [
    ("Jobs applied", "total"),
    ("Heard back", "heard_back"),
    ("Offers received", "offers"),
    ("Pending", "pending"),
]


class DashboardPage(BasePage):
    name = "dashboard"
    title = "Application dashboard"
    subtitle = "Aggregate progress across applications, responses, offers, and submission volume."

    def __init__(self, app):
        super().__init__(app)
        self.range_days = 14

    def render(self):
        self.render_heading()
        self.render_stat_tiles()

        charts_row = ttk.Frame(self.content, style="TFrame")
        charts_row.pack(fill="both", expand=True)
        self.render_line_chart_card(charts_row)
        self.render_pie_card(charts_row)

    def render_stat_tiles(self):
        stats = self.store.stats()
        stats_frame = ttk.Frame(self.content, style="TFrame")
        stats_frame.pack(fill="x", pady=(0, 18))
        for label, key in STAT_TILES:
            box = self.card(stats_frame, padding=(18, 16))
            box.pack(side="left", fill="x", expand=True, padx=(0, 12))
            ttk.Label(box, text=str(stats[key]), style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(box, text=label, style="MutedSurface.TLabel").pack(anchor="w", pady=(4, 0))

    def render_line_chart_card(self, parent):
        chart_card = self.card(parent)
        chart_card.pack(side="left", fill="both", expand=True, padx=(0, 12))

        header = ttk.Frame(chart_card, style="Surface.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="Total applications over time", style="CardTitle.TLabel").pack(
            side="left"
        )
        range_frame = ttk.Frame(header, style="Surface.TFrame")
        range_frame.pack(side="right")
        for label, days in TIME_RANGES:
            style = "RangeActive.TButton" if days == self.range_days else "Range.TButton"
            ttk.Button(
                range_frame,
                text=label,
                style=style,
                command=lambda d=days: self.set_range(d),
            ).pack(side="left", padx=(6, 0))

        canvas = tk.Canvas(chart_card, height=300, bg=self.theme["surface"], highlightthickness=0)
        canvas.pack(fill="both", expand=True, pady=(14, 0))
        canvas.bind("<Configure>", lambda event: self.draw_line_chart(canvas))
        self.draw_line_chart(canvas)

    def render_pie_card(self, parent):
        pie_card = self.card(parent)
        pie_card.pack(side="left", fill="both", expand=True)
        ttk.Label(pie_card, text="Applications by status", style="CardTitle.TLabel").pack(anchor="w")

        body = ttk.Frame(pie_card, style="Surface.TFrame")
        body.pack(fill="both", expand=True, pady=(14, 0))
        pie_canvas = tk.Canvas(
            body, width=220, height=220, bg=self.theme["surface"], highlightthickness=0
        )
        pie_canvas.pack(side="left", fill="y")
        legend = ttk.Frame(body, style="Surface.TFrame")
        legend.pack(side="left", fill="both", expand=True, padx=(18, 0))
        pie_canvas.bind("<Configure>", lambda event: self.draw_status_pie(pie_canvas))
        self.draw_status_pie(pie_canvas)
        self.render_status_legend(legend)

    def set_range(self, days):
        self.range_days = days
        self.show_page("dashboard")

    # Drawing ---------------------------------------------------------------

    def draw_line_chart(self, canvas):
        canvas.delete("all")
        data = self.store.cumulative_counts(self.range_days)
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
        # Thin out x labels so a 90 day range stays readable.
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
            canvas.create_line(
                *[coord for point in points for coord in point],
                fill=self.theme["chart"],
                width=2,
                smooth=True,
            )
        for index, ((x, y), (_, count)) in enumerate(zip(points, data)):
            radius = 3
            canvas.create_oval(
                x - radius, y - radius, x + radius, y + radius, fill=self.theme["chart"], outline=""
            )
            # A running total is non-zero nearly everywhere, so only the final
            # value is labelled to avoid a wall of numbers.
            if index == len(points) - 1:
                canvas.create_text(
                    x, y - 12, text=str(count), fill=self.theme["text"], font=("Segoe UI", 9)
                )

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
                x0,
                y0,
                x1,
                y1,
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
            swatch = tk.Canvas(
                row, width=12, height=12, highlightthickness=0, bg=self.theme["surface"]
            )
            swatch.pack(side="left")
            swatch.create_rectangle(0, 0, 12, 12, fill=STATUS_COLORS[status], outline="")
            pct = f"{(count / total * 100):.0f}%" if total else "0%"
            ttk.Label(row, text=f"{status} — {count} ({pct})", style="MutedSurface.TLabel").pack(
                side="left", padx=(8, 0)
            )
