"""Command line access to the pipeline.

Exists so the pipeline can be driven without the UI: a first backfill, a
one-off sync while debugging, a retention pass from a cron job, and a look at
what the rough filter is actually dropping.

Safe to run alongside the app. WAL mode plus a busy timeout (set in
`JobStore.configure_connection`) is what makes concurrent access work rather
than deadlock.

    python cli.py status
    python cli.py diagnostics --hours 24
    python cli.py sync --once
    python cli.py backfill --days 365 --max 2000
    python cli.py filter-stats
    python cli.py prune --days 30
    python cli.py prepare --lead 12
"""

import argparse
import asyncio
import json
import logging
import sys

from pipeline.orchestrator import PipelineCycle
from pipeline.sync import CURSOR_HISTORY_ID, CURSOR_LAST_SYNC, MailboxSync
from utilities.mailstore import MailStore
from utilities.store import JobStore

log = logging.getLogger("cli")


def open_stores(db_path=None):
    store = JobStore(db_path)
    return store, MailStore(store.conn)


def cmd_status(args):
    store, mail = open_stores(args.db)
    jobs = store.stats()
    print("Applications")
    print(f"  total {jobs['total']}   pending {jobs['pending']}   "
          f"heard back {jobs['heard_back']}   offers {jobs['offers']}")

    leads = mail.list_leads(None)
    by_status = {}
    for lead in leads:
        by_status[lead["status"]] = by_status.get(lead["status"], 0) + 1
    print("\nLeads")
    print("  " + ("   ".join(f"{status} {count}"
                             for status, count in sorted(by_status.items()))
                  or "none"))

    print("\nMailbox")
    print(f"  last sync   {mail.get_cursor(CURSOR_LAST_SYNC) or 'never'}")
    print(f"  history id  {mail.get_cursor(CURSOR_HISTORY_ID) or 'unset'}")
    # The same call the Settings badge makes. These two used to be different
    # queries that disagreed by 264; `cli.py diagnostics` breaks the number
    # down when it matters.
    print(f"  unclassified {mail.count_awaiting_classification()}")

    print("\nFilter verdicts")
    for verdict, count in mail.filter_stats().items():
        print(f"  {verdict:<32} {count}")
    print("\nCategories")
    for category, count in mail.category_stats().items():
        print(f"  {category:<32} {count}")
    store.close()
    return 0


#: Queue keys in the order a message actually moves through them, so the
#: report reads as a pipeline rather than as an alphabetised list of numbers.
QUEUE_ORDER = (
    ("awaiting_filter", "waiting for a filter verdict"),
    ("awaiting_body", "passed, waiting for a body"),
    ("awaiting_rules", "waiting for the rule tier"),
    ("awaiting_classification", "waiting for the model"),
    ("awaiting_handling_job_alert", "alerts waiting for extraction"),
    ("awaiting_handling_job_update", "updates waiting for extraction"),
    ("awaiting_handling_job_acknowledgement", "acknowledgements waiting"),
    ("dead_lettered", "retired - cannot be classified"),
)


def cmd_diagnostics(args):
    """Where the pipeline's time goes, and what is waiting on it.

    Summary:
        Print queue depths, stage timings, and provider outcomes.

    Parameters:
        args (argparse.Namespace): Parsed arguments; uses `db` and `hours`.

    Returns:
        int: 0.

    Note:
        Deliberately a CLI command and not only a page. The scheduler lives
        inside the NiceGUI process, so the moment worth asking "is anything
        draining" is exactly the moment the app is closed and the page is
        unreachable.
    """
    from datetime import datetime, timedelta

    store, mail = open_stores(args.db)
    since = (datetime.now() - timedelta(hours=args.hours)).isoformat(
        timespec="seconds")

    print(f"Queues  (as of {datetime.now().isoformat(timespec='seconds')})")
    depths = mail.queue_depths()
    for key, label in QUEUE_ORDER:
        print(f"  {label:<38} {depths.get(key, 0)}")

    dropped = mail.filtered_out()
    if dropped:
        total = sum(dropped.values())
        print(f"\nFiltered out before classification  ({total} total)")
        print("  Not a backlog: dropped on headers, no body fetched, never "
              "sent to a model.")
        for verdict, count in dropped.items():
            print(f"  {verdict:<38} {count}")

    print(f"\nStage timings  (last {args.hours}h)")
    timings = mail.stage_timings(since)
    if not timings:
        print("  Nothing recorded yet - the pipeline has not run in this "
              "window.")
    else:
        print(f"  {'stage':<12} {'runs':>5} {'done':>6} {'median':>9} "
              f"{'p95':>9} {'max':>9} {'fails':>6}")
        for row in timings:
            print(f"  {row['stage']:<12} {row['runs']:>5} "
                  f"{row['processed']:>6} {_ms(row['median_ms']):>9} "
                  f"{_ms(row['p95_ms']):>9} {_ms(row['max_ms']):>9} "
                  f"{row['failures']:>6}")

    errors = mail.last_stage_errors()
    if errors:
        print("\nLast failure per stage")
        for stage, entry in sorted(errors.items()):
            print(f"  {stage:<12} {entry['started_at']}  {entry['detail']}")

    print(f"\nProviders  (last {args.hours}h)")
    usage = mail.provider_usage_since(since)
    if not usage:
        print("  No model calls in this window.")
    for provider, entry in sorted(usage.items()):
        share = ""
        if entry["requests"]:
            share = f"  {entry['failures'] / entry['requests']:.0%} failed"
        print(f"  {provider:<12} {entry['requests']:>5} calls  "
              f"{entry['tokens']:>9} tokens{share}   {entry['model'] or ''}")

    store.close()
    return 0


def _ms(value):
    """
    Summary:
        Format a millisecond duration for a fixed-width column.

    Parameters:
        value (int | None): Milliseconds.

    Returns:
        str: Seconds with one decimal past a second, milliseconds below it.
    """
    if not value:
        return "0ms"
    return f"{value / 1000:.1f}s" if value >= 1000 else f"{int(value)}ms"


def cmd_sync(args):
    store, mail = open_stores(args.db)
    cycle = PipelineCycle(store, mail)
    result = asyncio.run(cycle.run())
    print(json.dumps(result, indent=2, default=str))
    store.close()
    return 0 if "error" not in result else 1


def cmd_backfill(args):
    """Seed the mirror from an existing mailbox.

    Bounded by a date cutoff and a message cap, and resumable: progress lives
    in the database, so an interrupted run picks up where it stopped rather
    than starting over.
    """
    store, mail = open_stores(args.db)
    sync = MailboxSync(mail, backfill_days=args.days)
    if args.restart:
        sync.reset_cursor()
        print("History cursor cleared; forcing a full walk.")

    stored = asyncio.run(sync.run(args.max))
    print(f"Stored {stored} new message(s).")

    if not args.headers_only:
        cycle = PipelineCycle(store, mail)
        print("Applying the rough filter...")
        print(json.dumps(cycle.apply_filter(), indent=2))
        print("\nRun 'python cli.py sync --once' repeatedly, or start the app, "
              "to fetch bodies and classify in paced batches.")
    store.close()
    return 0


def cmd_filter_stats(args):
    store, mail = open_stores(args.db)
    stats = mail.filter_stats()
    total = sum(stats.values()) or 1
    print(f"{'verdict':<34} {'count':>7}  share")
    for verdict, count in sorted(stats.items(), key=lambda kv: -kv[1]):
        print(f"{verdict:<34} {count:>7}  {count / total:6.1%}")

    if args.samples:
        print(f"\nMost recent {args.samples} dropped:")
        rows = mail.conn.execute(
            "SELECT sender, subject, filter_verdict FROM messages "
            "WHERE filter_verdict IS NOT NULL AND filter_verdict != 'passed' "
            "ORDER BY received_ts DESC LIMIT ?", (args.samples,)
        ).fetchall()
        for row in rows:
            print(f"  [{row['filter_verdict']}] {row['sender']} - {row['subject']}")
    store.close()
    return 0


def cmd_prune(args):
    store, mail = open_stores(args.db)
    cleared = mail.prune_bodies(args.days)
    print(f"Cleared {cleared} message bod{'y' if cleared == 1 else 'ies'}.")
    if cleared:
        mail.conn.execute("VACUUM")
        print("Vacuumed.")
    store.close()
    return 0


def cmd_prepare(args):
    from pipeline.prepare import LeadPreparer

    from clients.providers.pool import ProviderPool

    store, mail = open_stores(args.db)
    pool = ProviderPool(mail=mail)
    pool.begin_cycle()
    for state in pool.status():
        if not state["configured"]:
            print(f"{state['display']} unavailable: {state['last_error']}")

    # No sleep budget is passed: the CLI is not driving a UI, so the pool's
    # own thread check applies and a paced call may wait the longer bound.
    scorer = pool.for_task("score_relevance")
    research = pool.for_task("research")
    if scorer is None:
        print("No model is configured for scoring; leads will not be scored.")
    if research is None:
        print("No model is configured for research.")

    # The preparation stage is async so the web UI's event loop is not blocked
    # by it. The CLI has no loop to protect, so it just drives one.
    preparer = LeadPreparer(store, mail, scorer, research)
    try:
        if args.lead:
            ok = asyncio.run(preparer.prepare_now(args.lead))
            print("Prepared." if ok else "Could not prepare that lead.")
            return 0 if ok else 1

        print(json.dumps(
            asyncio.run(preparer.run(prepare_limit=args.max)), indent=2))
        return 0
    finally:
        pool.flush()
        store.close()


def cmd_requeue(args):
    """Put back messages a handler retired without ever really trying.

    Summary:
        Clear `handled_at` on classified mail that was marked handled but
        produced no link, so the handlers pick it up again.

    Parameters:
        args (argparse.Namespace): Carries `db`, `apply`, and `category`.

    Returns:
        int: Process exit status, 0 on success.

    Note:
        Written for a specific incident and kept because the shape recurs.
        `GROQ_MODEL` named a decommissioned model, so extraction returned 404
        on every call; `parse_alert` reported that as an empty digest and the
        alert handler stamped `handled_at`, permanently retiring 70 real job
        alerts. The code path is fixed - a failed call no longer counts as an
        attempt - but the rows it already wrote need putting back.

        Dry by default. Clearing `handled_at` is safe in itself (the worst
        case is one wasted extraction on a digest that really was empty), but
        it is still a write to the user's database, so it takes an explicit
        `--apply`.
    """
    store, mail = open_stores(args.db)
    rows = mail.messages_handled_without_result(args.category)
    if not rows:
        print("Nothing to requeue: every handled message produced a link.")
        store.close()
        return 0

    print(f"{len(rows)} message(s) marked handled but linked to nothing:")
    for row in rows[:20]:
        print(f"  {row['handled_at']}  {(row['subject'] or '(no subject)')[:64]}")
    if len(rows) > 20:
        print(f"  ... and {len(rows) - 20} more")

    if not args.apply:
        print("\nDry run. Re-run with --apply to clear handled_at on these.")
        store.close()
        return 0

    cleared = mail.requeue_handled_without_result(args.category)
    print(f"\nRequeued {cleared} message(s). The next cycle will process them.")
    store.close()
    return 0


def cmd_deny(args):
    store, mail = open_stores(args.db)
    mail.deny_sender(args.domain, args.reason)
    print(f"{args.domain} will be dropped by the rough filter.")
    store.close()
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="Job Board Tracker pipeline")
    parser.add_argument("--db", help="database path (defaults to the app's)")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="counts and cursors").set_defaults(func=cmd_status)

    diagnostics = sub.add_parser(
        "diagnostics", help="queue depths, stage timings, provider outcomes")
    diagnostics.add_argument(
        "--hours", type=int, default=24,
        help="how far back to summarise timings and usage (default 24)")
    diagnostics.set_defaults(func=cmd_diagnostics)

    sync = sub.add_parser("sync", help="run one pipeline cycle")
    sync.add_argument("--once", action="store_true", help="accepted for clarity")
    sync.set_defaults(func=cmd_sync)

    backfill = sub.add_parser("backfill", help="seed from an existing mailbox")
    backfill.add_argument("--days", type=int, default=365)
    backfill.add_argument("--max", type=int, default=2000)
    backfill.add_argument("--restart", action="store_true",
                          help="ignore the stored history cursor")
    backfill.add_argument("--headers-only", action="store_true")
    backfill.set_defaults(func=cmd_backfill)

    stats = sub.add_parser("filter-stats", help="what the rough filter dropped")
    stats.add_argument("--samples", type=int, default=0)
    stats.set_defaults(func=cmd_filter_stats)

    prune = sub.add_parser("prune", help="drop bodies of irrelevant old mail")
    prune.add_argument("--days", type=int, default=30)
    prune.set_defaults(func=cmd_prune)

    prepare = sub.add_parser("prepare", help="score and prepare leads")
    prepare.add_argument("--lead", type=int, help="prepare one lead, skipping the gate")
    prepare.add_argument("--max", type=int, default=5)
    prepare.set_defaults(func=cmd_prepare)

    requeue = sub.add_parser(
        "requeue",
        help="put back mail marked handled that produced nothing",
    )
    requeue.add_argument("--category", default="job_alert",
                         help="category to requeue (default: job_alert)")
    requeue.add_argument("--apply", action="store_true",
                         help="actually clear handled_at (default: dry run)")
    requeue.set_defaults(func=cmd_requeue)

    deny = sub.add_parser("deny", help="mark a sender domain as not job related")
    deny.add_argument("domain")
    deny.add_argument("--reason")
    deny.set_defaults(func=cmd_deny)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
