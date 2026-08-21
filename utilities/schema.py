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
#:
#: A whole *table* added below is the one exception to the rule above: it needs
#: no migration entry, because `create_tables` runs `CREATE TABLE IF NOT
#: EXISTS` unconditionally on every `initialise`, before the version gate. Only
#: new columns on existing tables need a migration.
SCHEMA_VERSION = 6

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
    -- Which model produced the category. More than one provider classifies
    -- now, and the confidence threshold is deliberately global rather than
    -- per-provider, so without this there is no way afterwards to tell whether
    -- a bad label came from the primary or from the fallback.
    category_model TEXT,
    classified_at TEXT,
    fetched_at TEXT NOT NULL,
    body_fetched_at TEXT,
    -- When a category handler finished with this message, whatever the
    -- outcome. Handlers used to select their backlog as "in my category and
    -- linked to nothing", which meant a digest containing no parseable posting
    -- - "Welcome to MyGreenhouse", a careers-site advert - was picked up,
    -- charged for, and found empty on every single cycle, for ever. This is
    -- what makes "tried, nothing to link" distinguishable from "not tried".
    handled_at TEXT
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
    -- When the posting was advertised, taken from the received time of the
    -- alert email that carried it. NOT `created_at`: that records when this
    -- pipeline got round to the email, and a backfill gives a hundred leads
    -- spanning three weeks of postings the same creation minute. Ordering and
    -- expiry both key on this, so both would be meaningless without it.
    posted_ts INTEGER,
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
-- Not the documents themselves: what they are made of. A resume is the ordered
-- list of experience rows chosen for it, so a stored application is tens of
-- bytes rather than four files, and re-rendering picks up edited bullets and an
-- edited master automatically. `kind` is `resume` or `cover_letter`.
CREATE TABLE IF NOT EXISTS job_artifacts (
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
    ai_model TEXT,
    ai_classified_at TEXT,
    ai_applied INTEGER NOT NULL DEFAULT 0,
    ai_previous_status TEXT,
    ai_previous_response_date TEXT,
    reviewed INTEGER NOT NULL DEFAULT 0,
    dismissed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, gmail_message_id)
);

-- What each provider actually spent, one row per call. -------------------------
--
-- Rows rather than counters, for the same reason `SpendLimiter` reads
-- `job_research` back instead of keeping a tally in memory: a counter needs a
-- reset job, cannot answer "what did the last hour cost", and resets to zero on
-- exactly the restart that a runaway loop makes most likely.
--
-- This is what makes a per-day ceiling real. The per-minute windows stay in
-- memory in `Pacer` - a restart outlives a sixty-second window, and persisting
-- one would put sqlite in the hot path of every model call.
CREATE TABLE IF NOT EXISTS provider_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    task TEXT NOT NULL,
    model TEXT,
    requests INTEGER NOT NULL DEFAULT 1,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    -- 'ok' | 'rate_limited' | 'denied_day' | 'error'
    outcome TEXT NOT NULL,
    at TEXT NOT NULL
);

-- Per-task provider routing, edited in Settings. -------------------------------
--
-- An absent row means "follow the .env default". Only an explicit edit writes
-- one, so a user who never opens Settings follows their .env, and editing .env
-- is not silently overridden by a stale row someone clicked through months ago.
-- "Reset to defaults" deletes the row rather than writing the current default.
CREATE TABLE IF NOT EXISTS provider_settings (
    task TEXT PRIMARY KEY,
    primary_provider TEXT,
    fallback_provider TEXT,
    updated_at TEXT NOT NULL
);

-- People worth asking for a referral, and where they work. ---------------------
--
-- `company_slug` is stored rather than derived per query because it is the join
-- key: leads carry whatever the board wrote ("Stripe", "Stripe, Inc.") and the
-- contact carries whatever the user typed, so both sides have to be reduced by
-- `utilities.identity.company_slug` before they can meet. Storing it also means
-- the match survives a rename of the display name.
--
-- `last_checked_ts` is what "new since you last looked" keys on. Without it the
-- morning list has no way to distinguish a posting that arrived overnight from
-- one that has been sitting there a week, which is the entire question the page
-- exists to answer.
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT,
    company TEXT NOT NULL,
    company_slug TEXT NOT NULL,
    role TEXT,
    careers_url TEXT,
    notes TEXT,
    last_checked_ts INTEGER,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Referral emails drafted for a contact about a role. -------------------------
--
-- Keyed on `identity_key` rather than a lead row id, for the same reason
-- `job_research` is: a lead that gets promoted to a real application keeps its
-- identity, so the record of having already asked someone survives the promotion
-- rather than being orphaned by it.
--
-- Unique on the pair, so the page can tell "not asked yet" from "asked on
-- Tuesday". A morning list that keeps re-offering an ask you already made is
-- worse than one that shows nothing.
CREATE TABLE IF NOT EXISTS referral_outreach (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL,
    identity_key TEXT NOT NULL,
    subject TEXT,
    body TEXT,
    -- Which model wrote it. Same rule as every other model output in the
    -- schema: without this there is no attributing a bad draft afterwards.
    model TEXT,
    -- 'drafted' | 'sent' | 'skipped'
    status TEXT NOT NULL DEFAULT 'drafted',
    created_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE(contact_id, identity_key)
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
CREATE INDEX IF NOT EXISTS idx_leads_posted ON job_leads(posted_ts);
CREATE INDEX IF NOT EXISTS idx_email_matches_job_id ON email_matches(job_id);
-- Every budget question is "how much has this provider spent since <time>",
-- so the pair is the useful order.
CREATE INDEX IF NOT EXISTS idx_provider_usage_at ON provider_usage(provider, at);
-- The morning lookup is "every company I am watching", so the archived flag
-- belongs in the index rather than as a filter over its results.
CREATE INDEX IF NOT EXISTS idx_contacts_slug ON contacts(company_slug, archived);
CREATE INDEX IF NOT EXISTS idx_outreach_contact ON referral_outreach(contact_id);
"""


def create_tables(conn):
    """Create every table that does not already exist.

    Safe to run on an existing database: it adds what is missing and leaves
    what is there alone. It does *not* reshape an existing table - that is what
    the migrations are for.

    Summary:
        Run `SCHEMA_SQL` against a connection to create any missing tables.

    Parameters:
        conn (sqlite3.Connection): The connection to create tables on.

    Raises:
        sqlite3.Error: If the script fails.
    """
    conn.executescript(SCHEMA_SQL)


def create_indexes(conn):
    """Create every index that does not already exist.

    Kept separate from `create_tables` and always run *after* migrations: an
    index names columns, and on a not-yet-migrated database those columns do
    not exist yet. Running the two together fails on `idx_jobs_identity`.

    Summary:
        Run `INDEX_SQL` against a connection to create any missing indexes.

    Parameters:
        conn (sqlite3.Connection): The connection to create indexes on.

    Raises:
        sqlite3.Error: If the script fails - for example, if called before
            migrations on a database still missing an indexed column.
    """
    conn.executescript(INDEX_SQL)


def create_schema(conn):
    """Tables and indexes together. For fresh databases and tests only.

    Summary:
        Create both tables and indexes in one call.

    Parameters:
        conn (sqlite3.Connection): The connection to build the schema on.

    Raises:
        sqlite3.Error: Propagated from `create_tables` or `create_indexes`.

    Note:
        Only correct for a genuinely fresh database. An existing one must go
        through `utilities/migrations.py`, which runs indexes after
        migrations rather than immediately after tables.
    """
    create_tables(conn)
    create_indexes(conn)


def table_exists(conn, name):
    """
    Summary:
        Report whether a table exists in the database.

    Parameters:
        conn (sqlite3.Connection): The connection to check.
        name (str): The table name.

    Returns:
        bool: True when the table exists.

    Raises:
        sqlite3.Error: If the query fails.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def column_names(conn, table):
    """
    Summary:
        List the column names a table currently has.

    Parameters:
        conn (sqlite3.Connection): The connection to inspect.
        table (str): The table name. Interpolated directly, since `PRAGMA`
            does not accept bound parameters - callers must only pass a
            trusted, known table name.

    Returns:
        set[str]: The table's column names. Empty for a nonexistent table
            rather than an error, since `PRAGMA table_info` on an unknown
            table simply returns no rows.

    Raises:
        sqlite3.Error: If the pragma query fails.
    """
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
