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
    return conn.execute("PRAGMA user_version").fetchone()[0]


def set_version(conn, version):
    # PRAGMA does not accept bound parameters, hence the f-string. `version` is
    # an int from our own MIGRATIONS table, never user input.
    conn.execute(f"PRAGMA user_version = {int(version)}")


def backup_before_migrating(db_path):
    """Copy the database aside before a structural change. Returns the path.

    Uses the sqlite backup API rather than a file copy, so it is safe even if
    another connection is mid-write. In-memory databases have nothing to back
    up and are skipped - that is the test path.
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
    """Widen `jobs` for the identity model and backfill `identity_key`."""
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
    """
    if "list_unsubscribe" not in column_names(conn, "messages"):
        conn.execute("ALTER TABLE messages ADD COLUMN list_unsubscribe TEXT")


MIGRATIONS = [
    (1, migrate_v1),
    (2, migrate_v2),
]


def pending_migrations(conn):
    version = current_version(conn)
    return [(v, fn) for v, fn in MIGRATIONS if v > version]


def apply_migrations(conn, db_path=None):
    """Run every migration newer than the database's recorded version.

    Each runs in its own transaction, so a failure halfway through a sequence
    leaves the database at the last version that fully succeeded rather than in
    a half-migrated state.
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
