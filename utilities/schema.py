"""The current database shape, in one place.

`SCHEMA_SQL` is what a brand new database gets. `utilities/migrations.py` is
how an existing one is brought up to the same shape. Keeping the two apart
means a fresh install never runs migration code, and the migrations never have
to reproduce the whole schema - only the deltas.

Anything added here needs a matching migration, or existing databases will be
missing it. `SCHEMA_VERSION` must be bumped in lockstep.
"""

#: Bump whenever a migration is added. A fresh database is stamped with this
#: value directly and skips the migration path entirely.
SCHEMA_VERSION = 2

SCHEMA_SQL = """
-- Applications the user has actually applied to. -----------------------------
--
-- `identity_key` is the real identity (see utilities/identity.py). `job_id`
-- stays the stable handle other tables reference, and stays URL-derived on
-- rows that predate the identity rework.
--
-- `posting_url` and `url_hash` are nullable: a job created from an
-- acknowledgement email may never have had a URL.
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

-- Many boards, one position. --------------------------------------------------
--
-- `board_job_id` is the board's own stable identifier, pulled out of the URL.
-- It dedupes far better than the tracking wrapper around it and is what 4.4
-- rebuilds a canonical apply link from.
CREATE TABLE IF NOT EXISTS job_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    url TEXT,
    board TEXT,
    board_job_id TEXT,
    first_seen TEXT NOT NULL,
    UNIQUE(job_id, url)
);

-- Local mailbox mirror. -------------------------------------------------------
--
-- `filter_verdict` records why a message was dropped by the rough filter. It
-- is not bookkeeping: without it, "why didn't I see that recruiter email" is
-- unanswerable.
CREATE TABLE IF NOT EXISTS messages (
    gmail_message_id TEXT PRIMARY KEY,
    thread_id TEXT,
    sender TEXT,
    subject TEXT,
    received_date TEXT,
    received_ts INTEGER,
    labels TEXT,
    -- Presence of this header marks automated bulk mail. Stored rather than
    -- only read at fetch time, because the rough filter runs as a separate
    -- pass and cannot re-request headers.
    list_unsubscribe TEXT,
    snippet TEXT,
    body_text TEXT,
    filter_verdict TEXT,
    category TEXT,
    category_confidence REAL,
    category_reason TEXT,
    classified_at TEXT,
    fetched_at TEXT NOT NULL,
    body_fetched_at TEXT
);

-- Which job(s) a message is about. --------------------------------------------
--
-- Keyed on identity_key rather than a row id so a lead that is later promoted
-- to a real application keeps every email already attached to it. Many-to-many
-- because one alert email carries 5-10 postings.
CREATE TABLE IF NOT EXISTS message_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_message_id TEXT NOT NULL,
    identity_key TEXT NOT NULL,
    link_type TEXT NOT NULL,
    confidence REAL,
    resolved_by TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(gmail_message_id, identity_key)
);

-- Roles surfaced from alerts that have not been applied to yet. ---------------
CREATE TABLE IF NOT EXISTS job_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT NOT NULL UNIQUE,
    identity_scheme TEXT,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    apply_url TEXT,
    tracking_url TEXT,
    board TEXT,
    board_job_id TEXT,
    source_message_id TEXT,
    relevance_score REAL,
    relevance_reason TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    prepare_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Company/role research, keyed on identity so it survives lead promotion. -----
CREATE TABLE IF NOT EXISTS job_research (
    identity_key TEXT PRIMARY KEY,
    summary TEXT,
    payload TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    fetched_at TEXT NOT NULL
);

-- Generated resume/CV artifacts, also keyed on identity. ----------------------
CREATE TABLE IF NOT EXISTS job_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    model TEXT,
    generated_at TEXT NOT NULL,
    UNIQUE(identity_key, kind)
);

-- Structured experience bullets that resumes are assembled from. --------------
CREATE TABLE IF NOT EXISTS experiences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    organisation TEXT,
    role TEXT,
    start_date TEXT,
    end_date TEXT,
    bullet TEXT NOT NULL,
    tags TEXT,
    impact TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Domains the user has marked as never job related (rough filter rule 3). -----
CREATE TABLE IF NOT EXISTS sender_denylist (
    domain TEXT PRIMARY KEY,
    added_at TEXT NOT NULL,
    reason TEXT
);

-- Small key/value store: profile text, Gmail historyId, ingest cursors. -------
CREATE TABLE IF NOT EXISTS profile (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Gmail replies matched to a job by the legacy per-job scanner. ---------------
CREATE TABLE IF NOT EXISTS email_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    gmail_message_id TEXT NOT NULL,
    sender TEXT,
    subject TEXT,
    received_date TEXT,
    snippet TEXT,
    body_text TEXT,
    ai_status TEXT,
    ai_confidence REAL,
    ai_reason TEXT,
    ai_classified_at TEXT,
    ai_applied INTEGER NOT NULL DEFAULT 0,
    ai_previous_status TEXT,
    ai_previous_response_date TEXT,
    reviewed INTEGER NOT NULL DEFAULT 0,
    dismissed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, gmail_message_id)
);
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_jobs_url_hash ON jobs(url_hash);
CREATE INDEX IF NOT EXISTS idx_jobs_identity ON jobs(identity_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_application_date ON jobs(application_date);
CREATE INDEX IF NOT EXISTS idx_job_sources_job_id ON job_sources(job_id);
CREATE INDEX IF NOT EXISTS idx_job_sources_board ON job_sources(board, board_job_id);
CREATE INDEX IF NOT EXISTS idx_messages_category ON messages(category);
CREATE INDEX IF NOT EXISTS idx_messages_verdict ON messages(filter_verdict);
CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_ts);
CREATE INDEX IF NOT EXISTS idx_links_identity ON message_links(identity_key);
CREATE INDEX IF NOT EXISTS idx_links_message ON message_links(gmail_message_id);
CREATE INDEX IF NOT EXISTS idx_leads_status ON job_leads(status);
CREATE INDEX IF NOT EXISTS idx_email_matches_job_id ON email_matches(job_id);
"""


def create_tables(conn):
    """Create every table that does not already exist.

    Safe to run on an existing database: it adds what is missing and leaves
    what is there alone. It does *not* reshape an existing table - that is what
    the migrations are for.
    """
    conn.executescript(SCHEMA_SQL)


def create_indexes(conn):
    """Create every index that does not already exist.

    Kept separate from `create_tables` and always run *after* migrations: an
    index names columns, and on a not-yet-migrated database those columns do
    not exist yet. Running the two together fails on `idx_jobs_identity`.
    """
    conn.executescript(INDEX_SQL)


def create_schema(conn):
    """Tables and indexes together. For fresh databases and tests only."""
    create_tables(conn)
    create_indexes(conn)


def table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def column_names(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
