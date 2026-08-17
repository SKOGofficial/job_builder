"""Command line access to the pipeline.

Exists so the pipeline can be driven without the UI: a first backfill, a
one-off sync while debugging, a retention pass from a cron job, and a look at
what the rough filter is actually dropping.

Safe to run alongside the app. WAL mode plus a busy timeout (set in
`JobStore.configure_connection`) is what makes concurrent access work rather
than deadlock.

    python cli.py status
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
    print(f"  unclassified {mail.count_awaiting_classification()}")

    print("\nFilter verdicts")
    for verdict, count in mail.filter_stats().items():
        print(f"  {verdict:<32} {count}")
    print("\nCategories")
    for category, count in mail.category_stats().items():
        print(f"  {category:<32} {count}")
    store.close()
    return 0


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
