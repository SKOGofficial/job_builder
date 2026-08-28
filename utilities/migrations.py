"""Bringing an existing database up to the current schema.

Gated on `PRAGMA user_version`, which SQLite stores in the file header - no
bookkeeping table needed, and it cannot drift out of sync with the file it
describes.

Two shapes of change appear here:

- **Additive** (new table, new column). Cheap and safe; `ADD COLUMN` or the
  `CREATE TABLE IF NOT EXISTS` in `schema.py` covers it.
- **Structural** (dropping NOT NULL, removing a column). SQLite cannot do these
  with `ALTER TABLE`, so the table has to be rebuilt: create the new shape,
  copy the rows, drop the old, rename. `rebuild_table` does that, and it runs
  inside the caller's transaction so a failure leaves the original intact.

`AGENTS.md` forbids touching `job_applications.sqlite3` without a clear backup
path. `backup_before_migrating` is that path, and `apply_migrations` calls it
before doing anything structural.
"""

import logging
import os
import shutil
from datetime import datetime

from utilities.identity import identity_key, identity_scheme
from utilities.schema import (
    SCHEMA_VERSION,
    column_names,
    create_indexes,
    create_tables,
    table_exists,
)

log = logging.getLogger(__name__)


def current_version(conn):
    """
    Summary:
        Read the schema version SQLite has stored in the file header.

    Parameters:
        conn (sqlite3.Connection): The connection to query.

    Returns:
        int: The `PRAGMA user_version` value. 0 for a database that has never
            been stamped.

    Raises:
        sqlite3.Error: If the pragma cannot be read.
    """
    return conn.execute("PRAGMA user_version").fetchone()[0]


def set_version(conn, version):
    """
    Summary:
        Stamp the schema version into the database file header.

    Parameters:
        conn (sqlite3.Connection): The connection to update.
        version (int): The version to stamp. Coerced with `int()` before
            interpolation.

    Raises:
        sqlite3.Error: If the pragma cannot be set.

    Note:
        `PRAGMA` does not accept bound parameters, hence the f-string.
        `version` must only ever come from `MIGRATIONS` or `SCHEMA_VERSION`,
        never from user input.
    """
    # PRAGMA does not accept bound parameters, hence the f-string. `version` is
    # an int from our own MIGRATIONS table, never user input.
    conn.execute(f"PRAGMA user_version = {int(version)}")


def backup_before_migrating(db_path):
    """Copy the database aside before a structural change. Returns the path.

    Uses the sqlite backup API rather than a file copy, so it is safe even if
    another connection is mid-write. In-memory databases have nothing to back
    up and are skipped - that is the test path.

    Summary:
        Copy the database file aside before a structural migration runs.

    Parameters:
        db_path (str | None): Path to the live database. An in-memory
            database, an empty path, or a path that does not yet exist skips
            the backup.

    Returns:
        str | None: The backup file's path, or None when there was nothing to
            back up.

    Raises:
        sqlite3.Error: If the backup connection or the copy fails.
        OSError: If the `backups/` directory cannot be created.

    Note:
        Uses the SQLite backup API rather than a plain file copy, so it is
        consistent even if another connection is mid-write.
    """
    if not db_path or db_path == ":memory:" or not os.path.exists(db_path):
        return None
    import sqlite3

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = os.path.join(os.path.dirname(db_path), "backups")
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(
        directory, f"{os.path.basename(db_path)}.pre-migration-{stamp}"
    )
    source = sqlite3.connect(db_path)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    log.info("Backed up %s to %s before migrating", db_path, target)
    return target


def rebuild_table(conn, table, create_sql, columns):
    """Rebuild a table into a new shape, preserving `columns`.

    SQLite cannot drop a NOT NULL constraint in place, so this is the only way
    to widen `jobs.posting_url`. Runs inside the caller's transaction.

    `columns` are copied across by name; anything in the new shape but not in
    the list gets its default.

    Summary:
        Rebuild a table into a new shape, keeping the listed columns' data.

    Parameters:
        conn (sqlite3.Connection): The connection to operate on.
        table (str): The table to rebuild.
        create_sql (str): A `CREATE TABLE IF NOT EXISTS {table} (...)`
            statement for the new shape. The table name is substituted with a
            temporary one internally.
        columns (list[str]): Column names present in both the old and new
            shape, copied across by name in the order given.

    Raises:
        sqlite3.Error: If any step fails. Runs inside the caller's
            transaction, so a failure here leaves the original table intact
            rather than partially rebuilt.

    Note:
        SQLite cannot drop a NOT NULL constraint or otherwise restructure a
        table in place; this create-copy-drop-rename sequence is the only way.
        A column in `create_sql` but absent from `columns` gets its schema
        default rather than a copied value.
    """
    temporary = f"{table}__migrating"
    conn.executescript(create_sql.replace(f"TABLE IF NOT EXISTS {table}",
                                          f"TABLE {temporary}", 1))
    shared = ", ".join(columns)
    conn.execute(f"INSERT INTO {temporary} ({shared}) SELECT {shared} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {temporary} RENAME TO {table}")


# --- v1: identity rework -----------------------------------------------------

JOBS_V1 = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    identity_key TEXT,
    identity_scheme TEXT,
    url_hash TEXT,
    posting_url TEXT,
    position_title TEXT NOT NULL,
    company TEXT,
    location TEXT,
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
"""

#: Columns carried across from the pre-v1 `jobs` table. `location`,
#: `identity_key`, and `identity_scheme` are new and are filled in afterwards.
JOBS_V0_COLUMNS = [
    "id", "job_id", "url_hash", "posting_url", "position_title", "company",
    "job_type", "requires_oa", "completed_oa", "received_references",
    "payment_amount", "payment_period", "status", "application_date",
    "response_date", "notes", "created_at", "updated_at",
]


def migrate_v1(conn):
    """Widen `jobs` for the identity model and backfill `identity_key`.

    Summary:
        Migrate a pre-v1 `jobs` table to the identity-key schema.

    Parameters:
        conn (sqlite3.Connection): The connection to migrate. Runs inside the
            caller's transaction.

    Returns:
        list[tuple[int, int, str]]: Collisions found while backfilling. See
            `backfill_identity_keys`.

    Raises:
        sqlite3.Error: If the rebuild or the backfill fails.

    Note:
        A database created fresh by `schema.py` already has the new columns
        and is left untouched - the rebuild only runs when `identity_key` is
        absent.
    """
    existing = column_names(conn, "jobs")

    # Only rebuild if this is genuinely a pre-v1 table. A database created
    # fresh by schema.py already has the new shape and must be left alone.
    if "identity_key" not in existing:
        carried = [c for c in JOBS_V0_COLUMNS if c in existing]
        rebuild_table(conn, "jobs", JOBS_V1, carried)
        # Indexes went with the dropped table. `initialise` recreates them
        # after every migration has run.

    backfill_identity_keys(conn)


def backfill_identity_keys(conn):
    """Give every job an identity key derived from its title/company/location.

    Legacy rows have no location, so they get the title+company scheme. A lead
    that later arrives *with* a location still finds them, because
    `candidate_keys` tries the bare key as a fallback.

    Duplicates are expected here - the same role logged twice from two boards
    now collides on one key. They are reported, not merged: a wrong merge
    destroys application history and the user cannot see that it happened.

    Summary:
        Compute and store an identity key for every job row that lacks one.

    Parameters:
        conn (sqlite3.Connection): The connection to update.

    Returns:
        list[tuple[int, int, str]]: One `(first_job_id, second_job_id,
            identity_key)` triple per collision detected, in the order found.
            Empty when every row got a distinct key.

    Raises:
        sqlite3.Error: If a read or write fails.

    Note:
        Never merges a collision - only logs it, at warning level, pointing at
        `/duplicates` for manual review. A legacy row with no location gets
        the title+company scheme; `candidate_keys` is what makes it still
        reachable once locations exist elsewhere.
    """
    rows = conn.execute(
        "SELECT id, position_title, company, location FROM jobs WHERE identity_key IS NULL"
    ).fetchall()
    seen = {}
    collisions = []
    for row in rows:
        key = identity_key(row["position_title"], row["company"], row["location"])
        scheme = identity_scheme(row["location"])
        if key in seen:
            collisions.append((seen[key], row["id"], key))
        else:
            seen[key] = row["id"]
        conn.execute(
            "UPDATE jobs SET identity_key = ?, identity_scheme = ? WHERE id = ?",
            (key, scheme, row["id"]),
        )
    if collisions:
        log.warning(
            "Identity backfill found %d job(s) sharing a key with an earlier row. "
            "Nothing was merged. Review them at /duplicates: %s",
            len(collisions),
            ", ".join(f"#{a} vs #{b} ({k})" for a, b, k in collisions),
        )
    return collisions


def migrate_v2(conn):
    """Store the List-Unsubscribe header.

    v1 fetched this header but never persisted it, so the rough filter's
    bulk-mail rule read a hardcoded empty string and could never fire. On a
    real mailbox that left the filter passing 97% of everything through to the
    classifier, which then hit the free-tier rate limit.

    Existing rows keep NULL. They were already filtered under the old
    behaviour; re-filtering them would mean re-fetching headers for the whole
    mirror to gain nothing - the classifier has already seen them.

    Summary:
        Add the `list_unsubscribe` column to `messages` on a pre-v2 database.

    Parameters:
        conn (sqlite3.Connection): The connection to migrate.

    Raises:
        sqlite3.Error: If the column check or the `ALTER TABLE` fails.

    Note:
        Additive only. No existing row is re-fetched or reprocessed to
        backfill the new column; it stays NULL for anything mirrored before
        this migration ran.
    """
    if "list_unsubscribe" not in column_names(conn, "messages"):
        conn.execute("ALTER TABLE messages ADD COLUMN list_unsubscribe TEXT")


def migrate_v3(conn):
    """Record which model produced a message's category.

    Two providers classify now, and the confidence threshold is deliberately
    global rather than per-provider, so the name of the model is the only thing
    that can attribute a label afterwards. Without it, "the fallback is
    mislabelling alerts" is a hypothesis nobody can test. `job_research.model`
    and `job_artifacts.model` already set the precedent for storing it.

    Existing rows keep NULL. Re-classifying the mirror to backfill would spend a
    day of free-tier quota to learn what is already known - everything written
    before this migration came from Groq, because it was the only provider.

    Summary:
        Add the `category_model` column to `messages` on a pre-v3 database.

    Parameters:
        conn (sqlite3.Connection): The connection to migrate.

    Raises:
        sqlite3.Error: If the column check or the `ALTER TABLE` fails.

    Note:
        Only `messages` is handled here. The matching `email_matches.ai_model`
        belongs to `ensure_email_match_columns`, which runs with no version
        gate and so also reaches databases that predate one. Two owners for one
        concept would mean two places to forget.
    """
    if "category_model" not in column_names(conn, "messages"):
        conn.execute("ALTER TABLE messages ADD COLUMN category_model TEXT")


def migrate_v4(conn):
    """Store what a document is made of instead of where a copy of it landed.

    `job_artifacts.path` pointed at files under `generated/`. Those files went
    stale the moment an experience bullet was edited, and nothing noticed - the
    recorded path still resolved, to a document making a claim the user had
    since rewritten. Four files per lead, all of it derivable from rows the
    database already held.

    What replaces it is the recipe: the ordered experience ids a resume was
    built from, and the letter text for a covering letter. Rendering happens on
    demand, so an edited bullet or an edited master shows up in the next
    download with nothing to invalidate.

    Existing rows are dropped rather than converted. A path cannot be turned
    back into the selection that produced it, and every one of them points at a
    Markdown or HTML file that was never the deliverable - no PDF was ever
    produced, because no engine was installed.

    Leads sitting at `ready` are returned to `new` in the same breath. `ready`
    means "documents are waiting", and after the drop theirs are not; left
    alone they would keep that promise on the page for ever, because
    `leads_awaiting_preparation` only ever looks at `new` and so would never
    revisit them. Sending them back is what makes "the next preparation pass
    rebuilds them" true rather than merely hopeful.

    Summary:
        Rebuild `job_artifacts` around selections, and requeue the leads whose
        documents it discarded.

    Parameters:
        conn (sqlite3.Connection): The connection to migrate.

    Raises:
        sqlite3.Error: If the table rebuild or the status reset fails.

    Note:
        A rebuild rather than a sequence of `ALTER TABLE`s because `path` is
        `NOT NULL` and sqlite cannot drop a column on older versions. The
        `UNIQUE(identity_key, kind)` constraint has to survive, so the table is
        recreated from the current schema rather than patched.
    """
    if "path" not in column_names(conn, "job_artifacts"):
        return
    conn.execute("DROP TABLE job_artifacts")
    conn.execute(
        """
        CREATE TABLE job_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identity_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            bullet_ids TEXT,
            letter_json TEXT,
            mapping_json TEXT,
            keywords TEXT,
            master_fingerprint TEXT,
            model TEXT,
            generated_at TEXT NOT NULL,
            UNIQUE(identity_key, kind)
        )
        """
    )
    conn.execute(
        "UPDATE job_leads SET status = 'new', prepare_error = NULL "
        "WHERE status = 'ready'"
    )


def migrate_v5(conn):
    """Let a handler record that it tried, even when it found nothing.

    Handlers selected their backlog as "in my category and linked to nothing".
    A job-board digest that yields no parseable posting - a careers-site advert,
    a "welcome to our job board" mail - never gets a link, so it matched that
    query again on the next cycle, and the one after, and was re-extracted at
    full model cost for ever. The same held for an acknowledgement the resolver
    could not place.

    Backfilled for messages that already have a link: those demonstrably were
    handled, and leaving them NULL would send the whole processed history back
    round the handlers once.

    Summary:
        Add `messages.handled_at` and backfill it for already-linked messages.

    Parameters:
        conn (sqlite3.Connection): The connection to migrate.

    Raises:
        sqlite3.Error: If the column check, the `ALTER TABLE`, or the backfill
            fails.

    Note:
        Messages in a job category with no link stay NULL on purpose. They are
        the genuine backlog, and one more pass over them - now with the rule
        classifier having relabelled the board marketing among them - is what
        clears them properly.
    """
    if "handled_at" in column_names(conn, "messages"):
        return
    conn.execute("ALTER TABLE messages ADD COLUMN handled_at TEXT")
    conn.execute(
        """
        UPDATE messages
        SET handled_at = COALESCE(classified_at, fetched_at)
        WHERE gmail_message_id IN (SELECT gmail_message_id FROM message_links)
        """
    )


def migrate_v6(conn):
    """Record when a lead's posting was advertised, not when we read the email.

    The to-apply list is ordered newest-first and drops anything older than a
    fortnight, and `created_at` can support neither. It records when this
    pipeline reached the alert email: a backfill stamped 127 leads with one
    creation minute even though the postings behind them spanned three weeks,
    which would order the list arbitrarily and then expire the whole thing in a
    single day.

    The alert email's own received time is the honest answer. It is not the
    posting date exactly - a board may re-advertise an older role - but it is
    the date the role was put in front of the user, which is what "still fresh
    enough to be worth applying to" actually means.

    Summary:
        Add `job_leads.posted_ts` and backfill it from each lead's source alert
        email.

    Parameters:
        conn (sqlite3.Connection): The connection to migrate.

    Raises:
        sqlite3.Error: If the column check, the `ALTER TABLE`, or the backfill
            fails.

    Note:
        A lead whose source message has since been deleted falls back to
        `created_at`, parsed as a timestamp. Leaving it NULL would make the row
        immortal - the expiry query cannot judge a row with no date - and an
        approximate date is a better failure than a lead that can never leave.
    """
    if "posted_ts" in column_names(conn, "job_leads"):
        return
    conn.execute("ALTER TABLE job_leads ADD COLUMN posted_ts INTEGER")
    conn.execute(
        """
        UPDATE job_leads
        SET posted_ts = COALESCE(
            (SELECT m.received_ts FROM messages m
             WHERE m.gmail_message_id = job_leads.source_message_id),
            CAST(strftime('%s', created_at) AS INTEGER)
        )
        """
    )


def migrate_v7(conn):
    """Make the pipeline's own behaviour measurable, and its failures terminal.

    Three columns, one purpose between them: the pipeline could not answer
    "how long did that take" or "why is this message still here" about itself.

    `provider_usage.duration_ms` is the missing half of that table. It already
    recorded what a call cost and how it ended; it never recorded how long it
    took, so a cycle that ran twenty minutes and one that ran two looked
    identical afterwards.

    `messages.classify_attempts` and `classify_error` end an unbounded retry.
    A failure specific to one message left it unclassified, and because the
    model queue is oldest-first that message was retried first on every cycle,
    for ever, spending quota each time with no record that it had ever failed.

    Summary:
        Add `provider_usage.duration_ms`, `messages.classify_attempts`, and
        `messages.classify_error`.

    Parameters:
        conn (sqlite3.Connection): The connection to migrate.

    Raises:
        sqlite3.Error: If a column check or an `ALTER TABLE` fails.

    Note:
        Nothing is backfilled. A duration cannot be reconstructed for a call
        that has already happened, and an attempt count invented for history
        would be a number the pipeline never actually observed - which is worse
        than NULL, because it reads as measurement. The new columns describe
        what happens from here.

        The stage_runs table these are read alongside needs no entry here:
        `create_tables` runs `CREATE TABLE IF NOT EXISTS` on every initialise,
        before the version gate.
    """
    if "duration_ms" not in column_names(conn, "provider_usage"):
        conn.execute("ALTER TABLE provider_usage ADD COLUMN duration_ms INTEGER")

    message_columns = column_names(conn, "messages")
    if "classify_attempts" not in message_columns:
        conn.execute(
            "ALTER TABLE messages "
            "ADD COLUMN classify_attempts INTEGER NOT NULL DEFAULT 0"
        )
    if "classify_error" not in message_columns:
        conn.execute("ALTER TABLE messages ADD COLUMN classify_error TEXT")


def migrate_v8(conn):
    """Let a saved route hold a whole chain, not just a first and second pick.

    `provider_settings` had `primary_provider` and `fallback_provider` and
    nothing else, so a task routed through three providers in `.env` - which is
    every classification task here - lost its third the moment anyone touched
    that task in Settings. The loss was silent and the UI, which only ever drew
    two dropdowns, could not show that it had happened.

    Summary:
        Add `provider_settings.chain` and backfill it from the existing pair.

    Parameters:
        conn (sqlite3.Connection): The connection to migrate.

    Raises:
        sqlite3.Error: If the column check, the `ALTER TABLE`, or the backfill
            fails.

    Note:
        Backfilled, unlike v7 - and for the opposite reason. A duration cannot
        be reconstructed after the fact, but a chain can: the pair already
        recorded is exactly the chain the user chose under the old shape, so
        writing it here changes nothing about what runs and only moves it to
        where the code now reads it.
    """
    if "chain" in column_names(conn, "provider_settings"):
        return
    conn.execute("ALTER TABLE provider_settings ADD COLUMN chain TEXT")
    conn.execute(
        """
        UPDATE provider_settings
        SET chain = TRIM(
            COALESCE(primary_provider, '') ||
            CASE
                WHEN fallback_provider IS NOT NULL AND fallback_provider <> ''
                THEN ',' || fallback_provider
                ELSE ''
            END,
            ','
        )
        """
    )


MIGRATIONS = [
    (1, migrate_v1),
    (2, migrate_v2),
    (3, migrate_v3),
    (4, migrate_v4),
    (5, migrate_v5),
    (6, migrate_v6),
    (7, migrate_v7),
    (8, migrate_v8),
]


def pending_migrations(conn):
    """
    Summary:
        List migrations newer than the database's recorded version.

    Parameters:
        conn (sqlite3.Connection): The connection to check.

    Returns:
        list[tuple[int, Callable]]: `(version, function)` pairs still to run,
            in ascending version order. Empty when the database is current.

    Raises:
        sqlite3.Error: Propagated from `current_version`.
    """
    version = current_version(conn)
    return [(v, fn) for v, fn in MIGRATIONS if v > version]


def apply_migrations(conn, db_path=None):
    """Run every migration newer than the database's recorded version.

    Each runs in its own transaction, so a failure halfway through a sequence
    leaves the database at the last version that fully succeeded rather than in
    a half-migrated state.

    Summary:
        Run every pending migration in order, stamping the version after each
        one succeeds.

    Parameters:
        conn (sqlite3.Connection): The connection to migrate.
        db_path (str | None): The database file path, passed through to
            `backup_before_migrating`.

    Returns:
        list[int]: The versions that were successfully applied, in order.
            Empty when nothing was pending.

    Raises:
        Exception: Whatever the failing migration function raised, after
            logging which version failed and which version the database was
            left at. Re-raised rather than swallowed so the caller cannot
            mistake a failed migration for success.

    Note:
        A backup is taken once, before the first migration in the batch, not
        once per migration.
    """
    pending = pending_migrations(conn)
    if not pending:
        return []

    backup_before_migrating(db_path)
    applied = []
    for version, migration in pending:
        log.info("Applying schema migration v%d", version)
        try:
            with conn:
                migration(conn)
                set_version(conn, version)
        except Exception:
            log.exception("Migration v%d failed; database left at v%d",
                          version, current_version(conn))
            raise
        applied.append(version)
    return applied


def ensure_email_match_columns(conn):
    """Additive columns on `email_matches` that later versions introduced.

    Predates the version gate, and has to keep working for databases that never
    recorded a version. `CREATE TABLE IF NOT EXISTS` leaves an existing table
    alone, so a database made before message bodies were stored keeps the old
    column set until it is widened here.

    Summary:
        Add any `email_matches` columns a pre-version-gate database is missing.

    Parameters:
        conn (sqlite3.Connection): The connection to migrate. A missing
            `email_matches` table is a no-op, since a fresh database gets the
            full column set from `schema.py` instead.

    Raises:
        sqlite3.Error: If a column check or `ALTER TABLE` fails.

    Note:
        Runs on every call to `initialise`, not just once, because it has no
        version to gate on. Each column is only added when absent, so
        repeated calls are cheap no-ops once the schema has caught up.
    """
    if not table_exists(conn, "email_matches"):
        return
    existing = column_names(conn, "email_matches")
    added = [
        ("snippet", "TEXT"),
        ("body_text", "TEXT"),
        ("ai_status", "TEXT"),
        ("ai_confidence", "REAL"),
        ("ai_reason", "TEXT"),
        # Which model produced the label. Added here rather than in a numbered
        # migration so it also reaches a database whose recorded version is
        # already past the one that would have carried it.
        ("ai_model", "TEXT"),
        ("ai_classified_at", "TEXT"),
        ("ai_applied", "INTEGER NOT NULL DEFAULT 0"),
        ("ai_previous_status", "TEXT"),
        ("ai_previous_response_date", "TEXT"),
    ]
    for column, declaration in added:
        if column not in existing:
            conn.execute(f"ALTER TABLE email_matches ADD COLUMN {column} {declaration}")


def initialise(conn, db_path=None):
    """Bring a connection's database to the current schema.

    A fresh database gets the current shape from `schema.py` and is stamped at
    `SCHEMA_VERSION` directly - it never runs migration code. An existing one
    gets the missing tables added, then the migrations reshape what is already
    there.

    Summary:
        Bring a database connection's schema up to the current version,
        migrating in place if needed.

    Parameters:
        conn (sqlite3.Connection): The connection to initialise.
        db_path (str | None): The database file path, used for the
            pre-migration backup when a migration is structural.

    Returns:
        list[int]: Migration versions applied. Empty for a fresh database or
            one already current.

    Raises:
        sqlite3.Error: If table creation, a migration, or index creation
            fails.

    Note:
        Commits at the end. Indexes are created last on purpose - a migration
        that rebuilds a table drops its indexes with it, so rebuilding them
        earlier would be wasted work.
    """
    fresh = not table_exists(conn, "jobs")
    create_tables(conn)
    ensure_email_match_columns(conn)

    if fresh:
        set_version(conn, SCHEMA_VERSION)
        applied = []
    else:
        applied = apply_migrations(conn, db_path)

    # Last, so indexes are built against the final column set. A migration that
    # rebuilds a table drops its indexes with it.
    create_indexes(conn)
    conn.commit()
    return applied
