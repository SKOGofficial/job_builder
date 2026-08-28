"""Where the pipeline's time goes, and what is waiting on it.

The pipeline could not answer questions about itself. There was no timing
instrumentation anywhere outside the tests, `provider_usage` recorded what a
call cost and how it ended but never how long it took, and a stage that failed
on every single cycle showed nothing in the interface - `last_error` only ever
held what reached the top of `PipelineCycle.run`.

The queue table is the other half. Two honest counts of the classification
backlog disagreed by 264, because "unclassified" meant different things to the
badge and to an ad-hoc query, and neither said which. Every number here comes
from `MailStore.queue_depths`, which defines each queue with the predicate the
stage draining it uses, so the page and the pipeline cannot drift apart.

Filtered-out mail is shown separately and labelled as a decision rather than a
backlog. Putting it in the same table as the queues is what created the
confusion in the first place.
"""

import logging
from datetime import datetime, timedelta

from nicegui import ui

from web.shell import card, page_shell
from web.state import get_state

log = logging.getLogger(__name__)

#: Queues in the order a message moves through them, so the table reads as a
#: pipeline rather than as an alphabetised list of numbers.
QUEUE_ORDER = [
    ("awaiting_filter", "Waiting for a filter verdict"),
    ("awaiting_body", "Passed, waiting for a body"),
    ("awaiting_rules", "Waiting for the rule tier"),
    ("awaiting_classification", "Waiting for the model"),
    ("awaiting_handling_job_alert", "Alerts waiting for extraction"),
    ("awaiting_handling_job_update", "Updates waiting for extraction"),
    ("awaiting_handling_job_acknowledgement", "Acknowledgements waiting"),
    ("dead_lettered", "Retired - cannot be classified"),
]

WINDOWS = [("24 hours", 24), ("7 days", 168), ("30 days", 720)]


def spell_ms(value):
    """
    Summary:
        Format a millisecond duration for display.

    Parameters:
        value (int | None): Milliseconds.

    Returns:
        str: Seconds past a second, milliseconds below it.
    """
    if not value:
        return "0 ms"
    return f"{value / 1000:.1f} s" if value >= 1000 else f"{int(value)} ms"


@ui.page("/diagnostics")
def diagnostics_page():
    state = get_state()
    mail = state.mail
    chosen = {"hours": 24}

    with page_shell(
        "Diagnostics",
        "What the pipeline is waiting on, how long its stages take, and which "
        "providers are actually serving. Everything here is measured, not "
        "estimated.",
        active="/diagnostics",
    ):

        def choose(hours):
            chosen["hours"] = hours
            timings_card.refresh()
            providers_card.refresh()

        def since():
            return (datetime.now() - timedelta(hours=chosen["hours"])).isoformat(
                timespec="seconds")

        # Queues ---------------------------------------------------------------

        @ui.refreshable
        def queues_card():
            with card():
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Queues").classes("text-base font-semibold")
                    ui.button(icon="refresh", on_click=refresh_all).props(
                        "flat round dense").tooltip("Refresh")

                try:
                    depths = mail.queue_depths()
                    dropped = mail.filtered_out()
                except Exception as exc:
                    log.exception("Could not read queue depths")
                    ui.label(f"Could not read the queues: {exc}").classes(
                        "text-sm text-red-500")
                    return

                rows = [{"queue": label, "waiting": depths.get(key, 0)}
                        for key, label in QUEUE_ORDER]
                ui.table(
                    columns=[
                        {"name": "queue", "label": "Queue", "field": "queue",
                         "align": "left"},
                        {"name": "waiting", "label": "Waiting",
                         "field": "waiting", "align": "right"},
                    ],
                    rows=rows, row_key="queue",
                ).props("flat dense").classes("w-full")

                if dropped:
                    total = sum(dropped.values())
                    ui.label(
                        f"{total} message(s) were dropped before classification."
                    ).classes("text-sm font-medium pt-3")
                    ui.label(
                        "Not a backlog. These were judged on their headers, "
                        "never had a body fetched, and were never sent to a "
                        "model. They will not clear, because there is nothing "
                        "waiting to happen to them."
                    ).classes("text-xs opacity-70")
                    with ui.column().classes("gap-0 pt-1"):
                        for verdict, count in dropped.items():
                            ui.label(f"{verdict}: {count}").classes(
                                "text-xs opacity-70")

        # Stage timings --------------------------------------------------------

        @ui.refreshable
        def timings_card():
            with card():
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Stage timings").classes("text-base font-semibold")
                    with ui.row().classes("items-center gap-1"):
                        for label, hours in WINDOWS:
                            ui.button(
                                label, on_click=lambda h=hours: choose(h)
                            ).props(
                                ("unelevated" if chosen["hours"] == hours
                                 else "flat") + " no-caps dense"
                            )

                try:
                    timings = mail.stage_timings(since())
                    errors = mail.last_stage_errors()
                except Exception as exc:
                    log.exception("Could not read stage timings")
                    ui.label(f"Could not read the timings: {exc}").classes(
                        "text-sm text-red-500")
                    return

                if not timings:
                    ui.label(
                        "Nothing recorded in this window. The scheduler runs "
                        "inside this app, so nothing is measured while it is "
                        "closed - run `python cli.py sync` to drain without it."
                    ).classes("text-sm opacity-70")
                    return

                ui.table(
                    columns=[
                        {"name": "stage", "label": "Stage", "field": "stage",
                         "align": "left"},
                        {"name": "runs", "label": "Runs", "field": "runs",
                         "align": "right"},
                        {"name": "processed", "label": "Items",
                         "field": "processed", "align": "right"},
                        {"name": "median", "label": "Median",
                         "field": "median", "align": "right"},
                        {"name": "p95", "label": "p95", "field": "p95",
                         "align": "right"},
                        {"name": "max", "label": "Slowest", "field": "max",
                         "align": "right"},
                        {"name": "failures", "label": "Failed",
                         "field": "failures", "align": "right"},
                    ],
                    rows=[{
                        "stage": row["stage"],
                        "runs": row["runs"],
                        "processed": row["processed"],
                        "median": spell_ms(row["median_ms"]),
                        "p95": spell_ms(row["p95_ms"]),
                        "max": spell_ms(row["max_ms"]),
                        "failures": row["failures"],
                    } for row in timings],
                    row_key="stage",
                ).props("flat dense").classes("w-full")
                ui.label(
                    "Slowest median first. Percentiles are measured values, "
                    "not interpolations."
                ).classes("text-xs opacity-60")

                if errors:
                    ui.label("Last failure per stage").classes(
                        "text-sm font-medium pt-3")
                    for stage, entry in sorted(errors.items()):
                        ui.label(
                            f"{stage} · {entry['started_at']} · "
                            f"{entry['detail'] or entry['outcome']}"
                        ).classes("text-xs text-red-500")

        # Providers ------------------------------------------------------------

        @ui.refreshable
        def providers_card():
            with card():
                ui.label("Providers").classes("text-base font-semibold")
                try:
                    usage = mail.provider_usage_since(since())
                except Exception as exc:
                    log.exception("Could not read provider usage")
                    ui.label(f"Could not read provider usage: {exc}").classes(
                        "text-sm text-red-500")
                    return

                if not usage:
                    ui.label("No model calls in this window.").classes(
                        "text-sm opacity-70")
                    return

                rows = []
                for provider, entry in sorted(usage.items()):
                    requests = entry["requests"] or 0
                    failures = entry["failures"] or 0
                    rows.append({
                        "provider": provider,
                        "model": entry["model"] or "-",
                        "calls": requests,
                        "failed": (f"{failures} ({failures / requests:.0%})"
                                   if requests else str(failures)),
                        "tokens": f"{entry['tokens']:,}",
                    })
                ui.table(
                    columns=[
                        {"name": "provider", "label": "Provider",
                         "field": "provider", "align": "left"},
                        {"name": "model", "label": "Model", "field": "model",
                         "align": "left"},
                        {"name": "calls", "label": "Calls", "field": "calls",
                         "align": "right"},
                        {"name": "failed", "label": "Refused or failed",
                         "field": "failed", "align": "right"},
                        {"name": "tokens", "label": "Tokens",
                         "field": "tokens", "align": "right"},
                    ],
                    rows=rows, row_key="provider",
                ).props("flat dense").classes("w-full")
                ui.label(
                    "A provider refusing most of its calls is not a provider. "
                    "Check the routing for that task in Settings."
                ).classes("text-xs opacity-60")

                research_spend()

        def research_spend():
            """The one spend the app measures in the units it is billed in.

            Summary:
                Show research output-token spend against its daily ceiling.

            Note:
                No dollar figures anywhere on this page, deliberately. Two of
                the four providers cannot be priced per token at all - the CLI
                bills a subscription and Groq's tier is free - so a price table
                would put a confident number next to the two it does not
                describe, and go stale besides. Research output tokens are the
                exception: `SpendLimiter` already enforces a ceiling in exactly
                those units, so the number is real and already load-bearing.
            """
            from clients.research_client import SpendLimiter

            try:
                limiter = SpendLimiter(mail)
                spent = limiter.spent_today()
            except Exception:
                log.debug("Research spend unavailable", exc_info=True)
                return

            output = spent["output_tokens"]
            ceiling = limiter.ceiling
            ui.label("Research budget").classes("text-sm font-medium pt-3")
            ui.label(
                f"{output:,} of {ceiling:,} output tokens used in the last 24 "
                f"hours, across {spent['calls']} call(s)."
            ).classes("text-xs opacity-70")
            if ceiling:
                bar = ui.linear_progress(
                    value=min(1.0, output / ceiling), show_value=False
                ).props("rounded")
                if output >= ceiling * 0.9:
                    bar.props("color=red")
                elif output >= ceiling * 0.5:
                    bar.props("color=orange")
            ui.label(
                "Raise it with RESEARCH_DAILY_OUTPUT_TOKENS. Only calls whose "
                "result was stored are counted, so a call that failed before "
                "answering is not billed here even though it was billed by the "
                "provider."
            ).classes("text-xs opacity-60")

        def refresh_all():
            queues_card.refresh()
            timings_card.refresh()
            providers_card.refresh()

        queues_card()
        timings_card()
        providers_card()
