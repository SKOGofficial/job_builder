"""Dashboard: summary tiles, cumulative line chart, and status breakdown.

The charts were previously drawn by hand on a tkinter Canvas. ECharts replaces
that with roughly a tenth of the code and gets tooltips and resizing for free.
"""

from datetime import datetime

from nicegui import ui

from utilities.theme import CHART_COLOR, STATUS_COLORS, TIME_RANGES
from web.shell import card, page_shell
from web.state import get_state

STAT_TILES = [
    ("Jobs applied", "total"),
    ("Heard back", "heard_back"),
    ("Offers received", "offers"),
    ("Pending", "pending"),
]


def short_date(iso_day):
    return datetime.fromisoformat(iso_day).strftime("%m/%d")


def line_options(data):
    return {
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 45, "right": 20, "top": 20, "bottom": 40},
        "xAxis": {
            "type": "category",
            "data": [short_date(day) for day, _count in data],
            "boundaryGap": False,
            # A 90 day range would otherwise stack labels on top of each other.
            "axisLabel": {"interval": max(1, len(data) // 12) - 1},
        },
        "yAxis": {"type": "value", "minInterval": 1},
        "series": [
            {
                "type": "line",
                "smooth": True,
                "showSymbol": len(data) <= 30,
                "data": [count for _day, count in data],
                "itemStyle": {"color": CHART_COLOR},
                "lineStyle": {"color": CHART_COLOR, "width": 2},
                "areaStyle": {"color": CHART_COLOR, "opacity": 0.12},
            }
        ],
    }


def pie_options(buckets):
    return {
        "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
        "legend": {"bottom": 0, "type": "scroll"},
        "series": [
            {
                "type": "pie",
                "radius": ["45%", "70%"],
                "top": -20,
                "label": {"show": False},
                "data": [
                    {
                        "name": status,
                        "value": count,
                        "itemStyle": {"color": STATUS_COLORS[status]},
                    }
                    for status, count in buckets.items()
                    if count
                ],
            }
        ],
    }


@ui.page("/dashboard")
def dashboard_page():
    store = get_state().store
    selected = {"days": 14}

    with page_shell(
        "Application dashboard",
        "Aggregate progress across applications, responses, offers, and submission volume.",
        active="/dashboard",
    ):
        stats = store.stats()
        with ui.row().classes("w-full gap-4 flex-wrap"):
            for label, key in STAT_TILES:
                with ui.card().classes("grow p-5 gap-1 shadow-sm").props("flat bordered"):
                    ui.label(str(stats[key])).classes("text-3xl font-semibold")
                    ui.label(label).classes("text-sm opacity-70")

        with ui.row().classes("w-full gap-4 items-stretch flex-wrap"):
            with ui.column().classes("grow min-w-[22rem] gap-0"):
                with card():
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label("Total applications over time").classes("text-base font-semibold")
                        with ui.row().classes("gap-1"):
                            for label, days in TIME_RANGES:
                                ui.button(
                                    label, on_click=lambda d=days: set_range(d)
                                ).props("flat dense no-caps").classes("text-xs")

                    @ui.refreshable
                    def line_chart():
                        ui.echart(line_options(store.cumulative_counts(selected["days"]))).classes(
                            "w-full h-72"
                        )

                    line_chart()

                    def set_range(days):
                        selected["days"] = days
                        line_chart.refresh()

            with ui.column().classes("grow min-w-[20rem] gap-0"):
                with card():
                    ui.label("Applications by status").classes("text-base font-semibold")
                    buckets = store.status_counts()
                    if sum(buckets.values()):
                        ui.echart(pie_options(buckets)).classes("w-full h-72")
                    else:
                        ui.label("No data yet.").classes("text-sm opacity-70 py-16 text-center")
