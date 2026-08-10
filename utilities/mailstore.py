"""Persistence for the mailbox mirror, leads, and generated artifacts.

Split from `store.py` rather than bolted onto `JobStore` for two reasons: the
tables here belong to the ingest pipeline rather than to application logging,
and keeping them apart means UI work on the applications side and pipeline work
do not keep colliding in one very large module.

Both classes share a single sqlite connection - the split is organisational,
not transactional, so an operation spanning both (promoting a lead into a job)
is still one atomic unit.

The linking model is the part worth understanding before reading further.
`message_links` points at an `identity_key`, never at a `jobs.id` or a
`job_leads.id`. A lead that is later promoted to a real application keeps its
identity, so every email already attached to it stays attached with no
migration and no re-linking pass. That is why the job detail page can show the
alert email that first surfaced the role alongside the rejection that closed
it.
"""

import json
import logging
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

log = logging.getLogger(__name__)

# --- vocabulary --------------------------------------------------------------
#
# Kept here rather than in `theme.py` because these are pipeline states with no
# presentation meaning, and `theme.py` is heavily imported by the UI.

CATEGORY_ALERT = "job_alert"
CATEGORY_UPDATE = "job_update"
CATEGORY_ACKNOWLEDGEMENT = "job_acknowledgement"
CATEGORY_IRRELEVANT = "irrelevant"

#: Categories that describe a real job. `irrelevant` is a common and expected
#: outcome, not an error - the rough filter deliberately passes plenty of
#: non-job mail through for the model to reject.
JOB_CATEGORIES = (CATEGORY_ALERT, CATEGORY_UPDATE, CATEGORY_ACKNOWLEDGEMENT)
CATEGORIES = JOB_CATEGORIES + (CATEGORY_IRRELEVANT,)

VERDICT_PASSED = "passed"

LEAD_NEW = "new"
LEAD_PREPARING = "preparing"
LEAD_READY = "ready"
LEAD_DISMISSED = "dismissed"
LEAD_APPLIED = "applied"
LEAD_STATUSES = (LEAD_NEW, LEAD_PREPARING, LEAD_READY, LEAD_DISMISSED, LEAD_APPLIED)

#: Statuses the to-apply list shows by default. `ready` first - that is the
#: state the user can actually act on.
LEAD_OPEN_STATUSES = (LEAD_READY, LEAD_PREPARING, LEAD_NEW)

#: Marks a `prepare_error` that is a pause rather than a failure. The column
#: holds free text and the Leads page renders it in red as "Preparation
#: failed", which is the wrong thing to say about a lead that is simply waiting
#: for a rate limit to clear. Written by `pipeline/prepare.py`, read by
#: `web/pages/leads.py`; the constant is here because both already import from
#: this module.
PREPARE_WAITING_PREFIX = "Waiting for a model"


def waiting_note(retry_after):
    """
    Summary:
        Phrase the note shown on a lead whose preparation is paused by a rate
        limit.

    Parameters:
        retry_after (float | int | None): Seconds until a provider frees up, as
            carried on `ProviderRateLimited`. None or a non-positive value
            drops the estimate rather than promising "in about 0s".

    Returns:
        str: A note beginning with `PREPARE_WAITING_PREFIX`, which is what
            tells the Leads page to show it as a pause and not a failure.
    """
    try:
        seconds = int(retry_after or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return f"{PREPARE_WAITING_PREFIX}. Retrying next cycle."
    when = f"{seconds}s" if seconds < 90 else f"{round(seconds / 60)}m"
    return f"{PREPARE_WAITING_PREFIX}. Retrying in about {when}."


def _now():
    """
    Summary:
        Current local time as a second-precision ISO-8601 string, the format
        every timestamp column in these tables stores.

    Returns:
        str: The timestamp, for example ``2026-08-03T16:47:09``.
    """
    return datetime.now().isoformat(timespec="seconds")


def parse_received(raw):
    """RFC 2822 Date header to a sortable epoch, or None.

    Gmail hands back whatever the sender wrote, which includes malformed dates
    and exotic timezones. A message with an unparseable date is still worth
    keeping, so this degrades to None rather than raising and losing the row.

    Summary:
        Convert an RFC 2822 Date header into a sortable Unix timestamp.

    Parameters:
        raw (str | None): The raw Date header as the sender wrote it. Empty or
            None is accepted.

    Returns:
        int | None: Seconds since the epoch, or None when the header is
            missing, unparseable, or outside the range the platform can
            represent.

    Note:
        Every failure path returns None instead of raising. A message with an
        unreadable date still belongs in the mirror; it just sorts last.
    """
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    try:
        return int(parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


class MailStore:
    """Pipeline-side persistence. Shares `JobStore`'s connection."""

    def __init__(self, conn):
        """
        Summary:
            Wrap an existing SQLite connection with the pipeline-side tables.

        Parameters:
            conn (sqlite3.Connection): The connection `JobStore` already owns.
                Shared rather than opened separately so an operation spanning
                both stores - promoting a lead into a job - stays atomic.

        Note:
            The schema is not created here. `JobStore.init_db` owns migrations
            for both halves.
        """
        self.conn = conn

    # --- messages ----------------------------------------------------------

    def has_message(self, gmail_message_id):
        """
        Summary:
            Report whether a Gmail message is already in the mirror.

        Parameters:
            gmail_message_id (str): The Gmail message ID to check.

        Returns:
            bool: True when the message has been stored.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            Single-message form. Use `known_message_ids` when checking a whole
            sync page, which is one query instead of one per message.
        """
        row = self.conn.execute(
            "SELECT 1 FROM messages WHERE gmail_message_id = ?", (gmail_message_id,)
        ).fetchone()
        return row is not None

    def known_message_ids(self, candidate_ids):
        """Which of these IDs are already stored.

        Bulk form so a sync pass can skip everything it has seen in one query
        rather than one round trip per message.

        Summary:
            Return which of the given Gmail message IDs are already stored.

        Parameters:
            candidate_ids (Iterable[str]): IDs to test. Consumed once, so a
                generator is fine.

        Returns:
            set[str]: The subset already present. Empty when `candidate_ids`
                is empty or none are known.

        Raises:
            sqlite3.Error: If a query fails.

        Note:
            Queried in chunks of 500 because SQLite caps host parameters at
            999 on older builds, and a sync page can exceed that.
        """
        ids = list(candidate_ids)
        if not ids:
            return set()
        found = set()
        # SQLite caps host parameters (999 on older builds), so chunk.
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ", ".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"SELECT gmail_message_id FROM messages WHERE gmail_message_id IN ({marks})",
                tuple(chunk),
            ).fetchall()
            found.update(row["gmail_message_id"] for row in rows)
        return found

    def upsert_message(self, header):
        """Store a message's headers. Returns True when the row is new.

        Bodies are a separate call (`store_body`) made only after the rough
        filter has passed the message, so a dropped message costs one metadata
        fetch and nothing more.

        Summary:
            Insert a message's headers into the mirror, ignoring one already
            stored.

        Parameters:
            header (dict): Header fields from Gmail. `id` is required;
                `thread_id`, `sender`, `subject`, `date`, `labels`,
                `list_unsubscribe`, and `snippet` are optional and default to
                empty. `labels` is stored as a JSON array.

        Returns:
            bool: True when a new row was inserted, False when the message was
                already mirrored.

        Raises:
            KeyError: If `header` has no `id`.
            sqlite3.Error: If the insert fails.

        Note:
            Does not commit - the sync pass batches many of these into one
            transaction. An unparseable Date is stored as-is with a NULL
            `received_ts`; see `parse_received`.
        """
        received = header.get("date")
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO messages (
                gmail_message_id, thread_id, sender, subject, received_date,
                received_ts, labels, list_unsubscribe, snippet, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                header["id"],
                header.get("thread_id"),
                header.get("sender", ""),
                header.get("subject", ""),
                received,
                parse_received(received),
                json.dumps(header.get("labels") or []),
                header.get("list_unsubscribe", ""),
                header.get("snippet", ""),
                _now(),
            ),
        )
        return cursor.rowcount > 0

    def set_filter_verdict(self, gmail_message_id, verdict):
        """
        Summary:
            Record what the rough filter decided about a message.

        Parameters:
            gmail_message_id (str): The message being marked.
            verdict (str): `VERDICT_PASSED` to send it on for a body fetch, or
                a rule name identifying why it was dropped. The rule names are
                what `filter_stats` counts.

        Raises:
            sqlite3.Error: If the update fails.

        Note:
            Does not commit. An unknown message ID updates nothing and does
            not raise.
        """
        self.conn.execute(
            "UPDATE messages SET filter_verdict = ? WHERE gmail_message_id = ?",
            (verdict, gmail_message_id),
        )

    def messages_awaiting_body(self, limit=50):
        """Passed the rough filter, body not downloaded yet.

        Summary:
            List messages that need their body fetched.

        Parameters:
            limit (int): Maximum rows to return. Defaults to 50, sized for one
                fetch batch.

        Returns:
            list[sqlite3.Row]: Messages with a passing filter verdict and no
                stored body, newest first.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            Newest first here, unlike `messages_awaiting_classification`.
            Recent mail is the mail worth acting on quickly.
        """
        return self.conn.execute(
            """
            SELECT * FROM messages
            WHERE filter_verdict = ? AND body_text IS NULL
            ORDER BY received_ts DESC
            LIMIT ?
            """,
            (VERDICT_PASSED, limit),
        ).fetchall()

    def store_body(self, gmail_message_id, body, snippet=None):
        """
        Summary:
            Attach fetched body text to a mirrored message.

        Parameters:
            gmail_message_id (str): The message to update.
            body (str): The extracted plain-text body.
            snippet (str | None): Refreshed snippet. None keeps the existing
                one rather than clearing it.

        Raises:
            sqlite3.Error: If the update fails.

        Note:
            Does not commit. Setting `body_fetched_at` is what removes the row
            from `messages_awaiting_body`.
        """
        self.conn.execute(
            """
            UPDATE messages
            SET body_text = ?,
                snippet = COALESCE(?, snippet),
                body_fetched_at = ?
            WHERE gmail_message_id = ?
            """,
            (body, snippet, _now(), gmail_message_id),
        )

    def messages_awaiting_classification(self, limit=None):
        """Has a body, has not been classified.

        Ordered oldest first so a resumed backfill makes forward progress
        through the backlog rather than re-walking the newest mail.

        Summary:
            List messages that have a body but no category yet.

        Parameters:
            limit (int | None): Maximum rows to return. None means no limit.

        Returns:
            list[sqlite3.Row]: Unclassified messages with non-empty body text,
                oldest first.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            Messages whose body is empty are excluded rather than skipped
            later - there is nothing for the model to read, so sending them
            would spend quota for a guaranteed non-answer.
        """
        sql = """
            SELECT * FROM messages
            WHERE body_text IS NOT NULL
              AND TRIM(body_text) <> ''
              AND category IS NULL
            ORDER BY received_ts ASC
        """
        if limit is None:
            return self.conn.execute(sql).fetchall()
        return self.conn.execute(sql + " LIMIT ?", (limit,)).fetchall()

    def count_awaiting_classification(self):
        """
        Summary:
            Count the messages waiting to be classified.

        Returns:
            int: The size of the classification backlog, shown as a badge in
                Settings.

        Raises:
            sqlite3.Error: If the query fails.
        """
        return self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM messages
            WHERE body_text IS NOT NULL AND TRIM(body_text) <> '' AND category IS NULL
            """
        ).fetchone()["n"]

    def record_category(self, gmail_message_id, category, confidence, reason,
                        model=None):
        """
        Summary:
            Store the category the model assigned to a message.

        Parameters:
            gmail_message_id (str): The message that was classified.
            category (str): One of `CATEGORIES` - alert, update,
                acknowledgement, or irrelevant.
            confidence (float): Model confidence, 0.0 to 1.0.
            reason (str): The model's short justification.
            model (str | None): Which model produced the category. Optional and
                last so existing callers are unaffected; NULL reads as "written
                before attribution existed, so it was Groq".

        Raises:
            sqlite3.Error: If the update fails.

        Note:
            Does not commit. Stamping `classified_at` is what removes the row
            from `messages_awaiting_classification`, so a cycle interrupted by
            a rate limit resumes rather than repeating.
        """
        self.conn.execute(
            """
            UPDATE messages
            SET category = ?, category_confidence = ?, category_reason = ?,
                category_model = ?, classified_at = ?
            WHERE gmail_message_id = ?
            """,
            (category, confidence, reason, model, _now(), gmail_message_id),
        )

    def message(self, gmail_message_id):
        """
        Summary:
            Fetch one mirrored message by its Gmail ID.

        Parameters:
            gmail_message_id (str): The message to fetch.

        Returns:
            sqlite3.Row | None: The full message row, or None when not
                mirrored.

        Raises:
            sqlite3.Error: If the query fails.
        """
        return self.conn.execute(
            "SELECT * FROM messages WHERE gmail_message_id = ?", (gmail_message_id,)
        ).fetchone()

    def messages_by_category(self, category, limit=100):
        """
        Summary:
            List mirrored messages carrying a given category.

        Parameters:
            category (str): One of `CATEGORIES`.
            limit (int): Maximum rows to return. Defaults to 100.

        Returns:
            list[sqlite3.Row]: Matching messages, newest first.

        Raises:
            sqlite3.Error: If the query fails.
        """
        return self.conn.execute(
            """
            SELECT * FROM messages WHERE category = ?
            ORDER BY received_ts DESC LIMIT ?
            """,
            (category, limit),
        ).fetchall()

    # --- observability -----------------------------------------------------

    def filter_stats(self):
        """Drop counts per rule.

        Worth surfacing: if the denylist rule stops growing, the "not job
        related" button is not discoverable enough, and the LLM is being paid
        to reject the same newsletters every day.

        Summary:
            Count mirrored messages grouped by rough-filter verdict.

        Returns:
            dict[str, int]: Counts keyed by verdict, largest first. Messages
                the filter has not reached yet are counted under
                `unfiltered`.

        Raises:
            sqlite3.Error: If the query fails.
        """
        rows = self.conn.execute(
            """
            SELECT COALESCE(filter_verdict, 'unfiltered') AS verdict, COUNT(*) AS count
            FROM messages GROUP BY verdict ORDER BY count DESC
            """
        ).fetchall()
        return {row["verdict"]: row["count"] for row in rows}

    def category_stats(self):
        """
        Summary:
            Count mirrored messages grouped by assigned category.

        Returns:
            dict[str, int]: Counts keyed by category, largest first. Messages
                with no category yet are counted under `unclassified`.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            A large `irrelevant` count is expected, not a fault. The rough
            filter deliberately passes doubtful mail through for the model to
            reject.
        """
        rows = self.conn.execute(
            """
            SELECT COALESCE(category, 'unclassified') AS category, COUNT(*) AS count
            FROM messages GROUP BY category ORDER BY count DESC
            """
        ).fetchall()
        return {row["category"]: row["count"] for row in rows}

    # --- retention ---------------------------------------------------------

    def prune_bodies(self, older_than_days=30):
        """Drop stored bodies for irrelevant mail past the retention window.

        Keeps the ID, headers, and classification so the message is never
        re-fetched or re-classified - only the bulk goes. Linked messages are
        never pruned regardless of age: they are the per-role timeline the user
        actually reads.

        Returns the number of bodies cleared. Callers should VACUUM afterwards
        or the file never shrinks.

        Summary:
            Clear stored bodies for irrelevant, unlinked mail older than the
            retention window.

        Parameters:
            older_than_days (int): Retention window in days. Defaults to 30.
                A message with no parseable date is treated as old enough.

        Returns:
            int: How many bodies were cleared.

        Raises:
            sqlite3.Error: If the update or the commit fails.

        Note:
            Commits. Only `irrelevant` bodies are eligible, and a message
            linked to any identity is never pruned however old - those are the
            per-role timeline the user reads. Clearing a body does not make
            the message eligible for re-fetch, because `body_fetched_at` and
            the category are both kept.
        """
        cutoff = int((datetime.now() - timedelta(days=older_than_days)).timestamp())
        cursor = self.conn.execute(
            """
            UPDATE messages
            SET body_text = NULL
            WHERE body_text IS NOT NULL
              AND category = ?
              AND (received_ts IS NULL OR received_ts < ?)
              AND gmail_message_id NOT IN (SELECT gmail_message_id FROM message_links)
            """,
            (CATEGORY_IRRELEVANT, cutoff),
        )
        self.conn.commit()
        return cursor.rowcount

    # --- links -------------------------------------------------------------

    def link_message(self, gmail_message_id, identity_key, link_type,
                     confidence=None, resolved_by=None):
        """Attach a message to a job identity. Idempotent.

        Summary:
            Link a mirrored message to a job identity.

        Parameters:
            gmail_message_id (str): The message to attach.
            identity_key (str): The identity to attach it to. Deliberately not
                a `jobs.id` or `job_leads.id` - a lead promoted to a real
                application keeps its identity, so the link survives with no
                re-linking pass.
            link_type (str): What the message is to the job, for example the
                acknowledgement or the rejection.
            confidence (float | None): Resolver confidence, when automatic.
            resolved_by (str | None): What made the link - a resolver stage
                name, or a marker for a manual link from the review queue.

        Returns:
            bool: True when the link was created, False when it already
                existed. Re-running the resolver is therefore free.

        Raises:
            sqlite3.Error: If the insert fails.

        Note:
            Does not commit.
        """
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO message_links (
                gmail_message_id, identity_key, link_type, confidence,
                resolved_by, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (gmail_message_id, identity_key, link_type, confidence, resolved_by, _now()),
        )
        return cursor.rowcount > 0

    def unlink_message(self, gmail_message_id, identity_key):
        """Undo a link. Needed because an auto-link can be wrong.

        Summary:
            Remove the link between a message and a job identity.

        Parameters:
            gmail_message_id (str): The linked message.
            identity_key (str): The identity to detach it from.

        Returns:
            bool: True when a link was removed, False when there was none.

        Raises:
            sqlite3.Error: If the delete or the commit fails.

        Note:
            Commits. Unlinking returns the message to `unlinked_messages`, and
            also makes it eligible for body pruning again if it is
            `irrelevant` and old enough.
        """
        cursor = self.conn.execute(
            "DELETE FROM message_links WHERE gmail_message_id = ? AND identity_key = ?",
            (gmail_message_id, identity_key),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def links_for_message(self, gmail_message_id):
        """
        Summary:
            List every identity a message is attached to.

        Parameters:
            gmail_message_id (str): The message to inspect.

        Returns:
            list[sqlite3.Row]: Link rows. Usually zero or one, but a digest
                naming several roles can legitimately link to several
                identities.

        Raises:
            sqlite3.Error: If the query fails.
        """
        return self.conn.execute(
            "SELECT * FROM message_links WHERE gmail_message_id = ?", (gmail_message_id,)
        ).fetchall()

    def messages_for_identity(self, identity_key):
        """The per-role email timeline, oldest first.

        This is what the job detail page renders: acknowledgement, then OA
        invite, then rejection, in the order they arrived.

        Summary:
            Return every message linked to an identity, oldest first.

        Parameters:
            identity_key (str): The job identity whose timeline is wanted.

        Returns:
            list[sqlite3.Row]: Message rows joined to their link, each
                carrying `link_type`, `confidence`, and `resolved_by`
                alongside the message columns. Ties on timestamp break on
                message ID so the order is stable between renders.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            A message whose body was pruned still appears here with its
            headers, but linked messages are exempt from pruning, so in
            practice this only affects rows linked after a prune.
        """
        return self.conn.execute(
            """
            SELECT m.*, l.link_type, l.confidence, l.resolved_by
            FROM messages m
            JOIN message_links l ON l.gmail_message_id = m.gmail_message_id
            WHERE l.identity_key = ?
            ORDER BY m.received_ts ASC, m.gmail_message_id ASC
            """,
            (identity_key,),
        ).fetchall()

    def unlinked_messages(self, limit=100):
        """Job-related mail the resolver could not place.

        The review queue. Without it these messages are classified, stored, and
        attached to nothing - which looks exactly like the pipeline working.

        Summary:
            List job-related messages that are attached to no identity.

        Parameters:
            limit (int): Maximum rows to return. Defaults to 100.

        Returns:
            list[sqlite3.Row]: Messages in a job category with no link, newest
                first.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            `irrelevant` messages are excluded - they are supposed to be
            unlinked, so including them would bury the real queue.
        """
        marks = ", ".join("?" for _ in JOB_CATEGORIES)
        return self.conn.execute(
            f"""
            SELECT * FROM messages
            WHERE category IN ({marks})
              AND gmail_message_id NOT IN (SELECT gmail_message_id FROM message_links)
            ORDER BY received_ts DESC
            LIMIT ?
            """,
            (*JOB_CATEGORIES, limit),
        ).fetchall()

    def count_unlinked(self):
        """
        Summary:
            Count job-related messages the resolver could not place.

        Returns:
            int: The size of the review queue, shown as a navigation badge.

        Raises:
            sqlite3.Error: If the query fails.
        """
        marks = ", ".join("?" for _ in JOB_CATEGORIES)
        return self.conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM messages
            WHERE category IN ({marks})
              AND gmail_message_id NOT IN (SELECT gmail_message_id FROM message_links)
            """,
            JOB_CATEGORIES,
        ).fetchone()["n"]

    # --- leads -------------------------------------------------------------

    def upsert_lead(self, lead):
        """Create a lead, or refresh the source details of an existing one.

        Unique on `identity_key`, so the same posting arriving from three
        boards over three days produces one row. A repeat sighting refreshes
        the apply URL (the older one may have expired) but never resets status
        or relevance - that would resurrect a lead the user dismissed.

        Summary:
            Create a lead, or refresh the source URLs of one that already
            exists.

        Parameters:
            lead (dict): The lead fields. `identity_key` and `title` are
                required; `identity_scheme`, `company`, `location`,
                `apply_url`, `tracking_url`, `board`, `board_job_id`,
                `source_message_id`, and `status` are optional. `status`
                defaults to `LEAD_NEW`.

        Returns:
            bool: True when a new lead was created, False when an existing one
                was refreshed.

        Raises:
            KeyError: If `identity_key` or `title` is absent.
            sqlite3.Error: If the insert or update fails.

        Note:
            Does not commit. On the refresh path only the two URLs and
            `updated_at` are touched, and each is written with COALESCE so a
            None never erases a stored value. Status and relevance are never
            reset - doing so would resurrect a dismissed lead every time the
            posting was re-advertised.
        """
        now = _now()
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO job_leads (
                identity_key, identity_scheme, title, company, location,
                apply_url, tracking_url, board, board_job_id, source_message_id,
                status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lead["identity_key"],
                lead.get("identity_scheme"),
                lead["title"],
                lead.get("company"),
                lead.get("location"),
                lead.get("apply_url"),
                lead.get("tracking_url"),
                lead.get("board"),
                lead.get("board_job_id"),
                lead.get("source_message_id"),
                lead.get("status", LEAD_NEW),
                now,
                now,
            ),
        )
        if cursor.rowcount == 0:
            self.conn.execute(
                """
                UPDATE job_leads
                SET apply_url = COALESCE(?, apply_url),
                    tracking_url = COALESCE(?, tracking_url),
                    updated_at = ?
                WHERE identity_key = ?
                """,
                (lead.get("apply_url"), lead.get("tracking_url"), now,
                 lead["identity_key"]),
            )
        return cursor.rowcount > 0

    def lead_by_identity(self, identity_key):
        """
        Summary:
            Fetch a lead by its job identity.

        Parameters:
            identity_key (str): The identity to look up. Unique in this table.

        Returns:
            sqlite3.Row | None: The lead, or None when the identity has no
                lead.

        Raises:
            sqlite3.Error: If the query fails.
        """
        return self.conn.execute(
            "SELECT * FROM job_leads WHERE identity_key = ?", (identity_key,)
        ).fetchone()

    def lead(self, lead_id):
        """
        Summary:
            Fetch a lead by its row ID.

        Parameters:
            lead_id (int): The `job_leads.id` primary key.

        Returns:
            sqlite3.Row | None: The lead, or None when the ID is unknown.

        Raises:
            sqlite3.Error: If the query fails.
        """
        return self.conn.execute(
            "SELECT * FROM job_leads WHERE id = ?", (lead_id,)
        ).fetchone()

    def list_leads(self, statuses=LEAD_OPEN_STATUSES):
        """The to-apply list. Ready rows first, then newest.

        Summary:
            List leads, ordered so the ones the user can act on come first.

        Parameters:
            statuses (tuple[str, ...] | None): Statuses to include. Defaults
                to `LEAD_OPEN_STATUSES`. Pass None for every lead regardless
                of status, which is what the "show dismissed" view uses.

        Returns:
            list[sqlite3.Row]: Matching leads. Filtered results are ordered
                `ready` first, then by relevance score descending, then
                newest. The unfiltered branch is newest-first only.

        Raises:
            sqlite3.Error: If the query fails.
        """
        if statuses is None:
            return self.conn.execute(
                "SELECT * FROM job_leads ORDER BY created_at DESC"
            ).fetchall()
        marks = ", ".join("?" for _ in statuses)
        return self.conn.execute(
            f"""
            SELECT * FROM job_leads
            WHERE status IN ({marks})
            ORDER BY CASE status WHEN 'ready' THEN 0 ELSE 1 END,
                     relevance_score DESC NULLS LAST,
                     created_at DESC
            """,
            tuple(statuses),
        ).fetchall()

    def set_lead_status(self, lead_id, status, error=None):
        """
        Summary:
            Move a lead to a new status, optionally recording why preparation
            failed.

        Parameters:
            lead_id (int): The `job_leads.id` to update.
            status (str): One of `LEAD_STATUSES`.
            error (str | None): Failure detail to show on the lead. Passing
                None clears any previous error, so a successful retry does not
                leave a stale message behind.

        Raises:
            sqlite3.Error: If the update or the commit fails.

        Note:
            Commits. `prepare_error` is written unconditionally rather than
            with COALESCE, which is what makes the clear-on-success behaviour
            work.
        """
        self.conn.execute(
            "UPDATE job_leads SET status = ?, prepare_error = ?, updated_at = ? WHERE id = ?",
            (status, error, _now(), lead_id),
        )
        self.conn.commit()

    def set_lead_relevance(self, lead_id, score, reason):
        """
        Summary:
            Record how well a lead matches the user's profile.

        Parameters:
            lead_id (int): The `job_leads.id` to score.
            score (float): The relevance score. Compared against the
                preparation threshold by `leads_awaiting_preparation`, not
                here.
            reason (str): The model's justification, shown on the lead.

        Raises:
            sqlite3.Error: If the update or the commit fails.

        Note:
            Commits. Setting a score is what removes the lead from
            `leads_awaiting_relevance` and makes it a candidate for the
            expensive research pass.
        """
        self.conn.execute(
            """
            UPDATE job_leads
            SET relevance_score = ?, relevance_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (score, reason, _now(), lead_id),
        )
        self.conn.commit()

    def leads_awaiting_relevance(self, limit=50):
        """
        Summary:
            List new leads that have not been scored for relevance yet.

        Parameters:
            limit (int): Maximum rows to return. Defaults to 50.

        Returns:
            list[sqlite3.Row]: Unscored leads still in `new`, oldest first so
                a backlog drains in arrival order.

        Raises:
            sqlite3.Error: If the query fails.
        """
        return self.conn.execute(
            """
            SELECT * FROM job_leads
            WHERE relevance_score IS NULL AND status = ?
            ORDER BY created_at ASC LIMIT ?
            """,
            (LEAD_NEW, limit),
        ).fetchall()

    def leads_awaiting_preparation(self, threshold, limit=10):
        """Scored above the bar, not yet prepared.

        The gate that keeps Opus spend proportional: only leads the cheap model
        thinks are worth pursuing reach the expensive research pass.

        Summary:
            List scored leads at or above the threshold that still need
            preparing.

        Parameters:
            threshold (float): Minimum relevance score to qualify. This is the
                spend gate.
            limit (int): Maximum rows to return. Defaults to 10, deliberately
                small - each of these becomes an expensive model call.

        Returns:
            list[sqlite3.Row]: Qualifying leads, best score first, then oldest.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            Removing the threshold or raising the limit turns a parser bug
            into a large bill. Both are cost controls, not tuning knobs.
        """
        return self.conn.execute(
            """
            SELECT * FROM job_leads
            WHERE status = ? AND relevance_score IS NOT NULL AND relevance_score >= ?
            ORDER BY relevance_score DESC, created_at ASC
            LIMIT ?
            """,
            (LEAD_NEW, threshold, limit),
        ).fetchall()

    # --- research ----------------------------------------------------------

    def save_research(self, identity_key, summary, payload, model=None,
                      input_tokens=None, output_tokens=None):
        """
        Summary:
            Store company research for a job identity, replacing any earlier
            result.

        Parameters:
            identity_key (str): The identity the research is about. Unique, so
                re-researching overwrites rather than accumulating.
            summary (str): The human-readable summary shown on the lead.
            payload (Any | None): Structured findings, JSON-encoded before
                storage. None is stored as NULL.
            model (str | None): Which model produced it, for later attribution.
            input_tokens (int | None): Input tokens consumed.
            output_tokens (int | None): Output tokens consumed.

        Raises:
            sqlite3.Error: If the upsert or the commit fails.
            TypeError: If `payload` is not JSON-serialisable.

        Note:
            Commits. The token counts are what `research_spend_since` totals
            for the daily ceiling, so omitting them makes that spend
            invisible.
        """
        self.conn.execute(
            """
            INSERT INTO job_research (
                identity_key, summary, payload, model, input_tokens, output_tokens, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_key) DO UPDATE SET
                summary = excluded.summary,
                payload = excluded.payload,
                model = excluded.model,
                input_tokens = excluded.input_tokens,
                output_tokens = excluded.output_tokens,
                fetched_at = excluded.fetched_at
            """,
            (identity_key, summary,
             json.dumps(payload) if payload is not None else None,
             model, input_tokens, output_tokens, _now()),
        )
        self.conn.commit()

    def research_for(self, identity_key):
        """
        Summary:
            Fetch stored research for a job identity.

        Parameters:
            identity_key (str): The identity to look up.

        Returns:
            sqlite3.Row | None: The research row, or None when the identity
                has not been researched. `payload` is still a JSON string; the
                caller decodes it.

        Raises:
            sqlite3.Error: If the query fails.
        """
        return self.conn.execute(
            "SELECT * FROM job_research WHERE identity_key = ?", (identity_key,)
        ).fetchone()

    def research_spend_since(self, since_iso):
        """Token spend since a timestamp, for the daily ceiling in 6.5.

        Summary:
            Total research token spend and call count since a timestamp.

        Parameters:
            since_iso (str): Inclusive lower bound as an ISO-8601 timestamp,
                matching the format `_now` writes.

        Returns:
            dict: Keys `input_tokens`, `output_tokens`, and `calls`. Token
                totals are 0 rather than None when nothing matches, so the
                caller can compare against the ceiling without a null check.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            Counts only what reached `save_research`. A call that failed
            before its result was stored is invisible here, so the ceiling is
            a floor on real spend rather than an exact figure.
        """
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COUNT(*) AS calls
            FROM job_research WHERE fetched_at >= ?
            """,
            (since_iso,),
        ).fetchone()
        return dict(row)

    # --- provider routing and usage ----------------------------------------
    #
    # Everything here is called on the thread that owns the connection, never
    # from inside a worker. The provider pool loads counters once per cycle,
    # counts in memory while stages run, and flushes at the end - see the
    # concurrency contract in .agents/AGENTS.md.

    def provider_routes(self):
        """Task routing the user has explicitly chosen.

        Only rows written by an edit in Settings come back. A task with no row
        is absent from the result rather than present with a default, because
        "no opinion" and "deliberately chose the default" have to stay
        distinguishable: the first follows `.env` as it changes, the second
        would pin a value the user never revisits.

        Summary:
            Read the saved per-task provider routing.

        Returns:
            dict[str, tuple[str | None, str | None]]: Task id mapped to
                `(primary, fallback)`. Empty when nothing has been edited.

        Raises:
            sqlite3.Error: If the query fails.
        """
        rows = self.conn.execute(
            "SELECT task, primary_provider, fallback_provider FROM provider_settings"
        ).fetchall()
        return {
            row["task"]: (row["primary_provider"], row["fallback_provider"])
            for row in rows
        }

    def set_provider_route(self, task, primary, fallback=None):
        """
        Summary:
            Save which providers should serve one task, in order.

        Parameters:
            task (str): The task identifier being routed.
            primary (str | None): Provider to try first. None means the task is
                turned off entirely.
            fallback (str | None): Provider to try when the primary has no
                headroom. None means there is nowhere to fall back to.

        Raises:
            sqlite3.Error: If the write or the commit fails.

        Note:
            Commits, because this is a user action rather than pipeline
            progress - it must survive whatever the current cycle does next.
        """
        self.conn.execute(
            """
            INSERT INTO provider_settings
                (task, primary_provider, fallback_provider, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(task) DO UPDATE SET
                primary_provider = excluded.primary_provider,
                fallback_provider = excluded.fallback_provider,
                updated_at = excluded.updated_at
            """,
            (task, primary, fallback, _now()),
        )
        self.conn.commit()

    def clear_provider_route(self, task):
        """Forget an explicit choice, returning the task to its `.env` default.

        Summary:
            Delete the saved routing for one task.

        Parameters:
            task (str): The task identifier to reset.

        Returns:
            bool: True when a row was removed, False when there was nothing
                saved for that task.

        Raises:
            sqlite3.Error: If the delete or the commit fails.

        Note:
            Deletes rather than writing the current default. Writing it would
            freeze today's default into the database, so a later change to
            `.env` would silently not take effect.
        """
        cursor = self.conn.execute(
            "DELETE FROM provider_settings WHERE task = ?", (task,)
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def record_provider_usage(self, rows):
        """Append what a batch of model calls cost.

        Summary:
            Write one `provider_usage` row per completed call attempt.

        Parameters:
            rows (list[dict]): Each with `provider`, `task`, and `outcome`;
                optionally `model`, `input_tokens`, `output_tokens`,
                `total_tokens`. Missing token counts record as 0.

        Returns:
            int: How many rows were written.

        Raises:
            sqlite3.Error: If the write or the commit fails.

        Note:
            Commits. Failed attempts are recorded too, not just successes - a
            429 is exactly the event a daily budget needs to remember, and
            dropping it would let a restart retry a provider that is out.
        """
        if not rows:
            return 0
        at = _now()
        self.conn.executemany(
            """
            INSERT INTO provider_usage
                (provider, task, model, requests, input_tokens, output_tokens,
                 total_tokens, outcome, at)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["provider"],
                    row["task"],
                    row.get("model"),
                    row.get("input_tokens") or 0,
                    row.get("output_tokens") or 0,
                    row.get("total_tokens") or 0,
                    row["outcome"],
                    row.get("at") or at,
                )
                for row in rows
            ],
        )
        self.conn.commit()
        return len(rows)

    def provider_requests_since(self, provider, since_iso):
        """
        Summary:
            Count requests one provider has made since a timestamp.

        Parameters:
            provider (str): The provider name to count.
            since_iso (str): Inclusive lower bound as an ISO-8601 timestamp,
                matching the format `_now` writes.

        Returns:
            int: Requests recorded in the window, successful or not.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            A rolling window, not a calendar day. Google resets its free-tier
            quota at midnight Pacific; counting the last 24 hours is stricter
            than that everywhere on earth, which is the safe direction for a
            ceiling nobody wants to discover by being refused.
        """
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(requests), 0) AS n FROM provider_usage
            WHERE provider = ? AND at >= ?
            """,
            (provider, since_iso),
        ).fetchone()
        return row["n"]

    def provider_denied_day_since(self, provider, since_iso):
        """Whether a provider has reported a per-day limit inside the window.

        This is what stops a restart from un-exhausting a daily cap. The
        in-memory counter is rebuilt from request rows, but a provider can
        refuse for reasons our count did not predict - a shared project quota,
        a limit lowered upstream - and its own refusal is better evidence than
        our arithmetic.

        Summary:
            Report whether a per-day denial was recorded for a provider.

        Parameters:
            provider (str): The provider name to check.
            since_iso (str): Inclusive lower bound as an ISO-8601 timestamp.

        Returns:
            bool: True when at least one `denied_day` row falls in the window.

        Raises:
            sqlite3.Error: If the query fails.
        """
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM provider_usage
            WHERE provider = ? AND at >= ? AND outcome = 'denied_day'
            """,
            (provider, since_iso),
        ).fetchone()
        return row["n"] > 0

    def provider_usage_since(self, since_iso):
        """
        Summary:
            Summarise every provider's spend since a timestamp, for Settings.

        Parameters:
            since_iso (str): Inclusive lower bound as an ISO-8601 timestamp.

        Returns:
            dict[str, dict]: Provider name mapped to `requests`, `tokens`,
                `failures`, and `model` - the model most recently seen for that
                provider in the window.

        Raises:
            sqlite3.Error: If the query fails.
        """
        rows = self.conn.execute(
            """
            SELECT provider,
                   COALESCE(SUM(requests), 0) AS requests,
                   COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(CASE WHEN outcome <> 'ok' THEN 1 ELSE 0 END), 0)
                       AS failures,
                   MAX(at) AS last_at
            FROM provider_usage WHERE at >= ?
            GROUP BY provider
            """,
            (since_iso,),
        ).fetchall()
        summary = {}
        for row in rows:
            model = self.conn.execute(
                """
                SELECT model FROM provider_usage
                WHERE provider = ? AND model IS NOT NULL
                ORDER BY at DESC LIMIT 1
                """,
                (row["provider"],),
            ).fetchone()
            summary[row["provider"]] = {
                "requests": row["requests"],
                "tokens": row["tokens"],
                "failures": row["failures"],
                "last_at": row["last_at"],
                "model": model["model"] if model else None,
            }
        return summary

    def prune_provider_usage(self, older_than_days=30):
        """
        Summary:
            Delete usage rows past the retention window.

        Parameters:
            older_than_days (int): Retention window in days. Defaults to 30.

        Returns:
            int: How many rows were deleted.

        Raises:
            sqlite3.Error: If the delete or the commit fails.

        Note:
            Commits. The window only ever needs to reach back 24 hours for
            budgeting; the rest is kept so "what did last month cost" stays
            answerable, and dropped after that so the table cannot grow without
            bound on a machine that runs unattended.
        """
        cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat(
            timespec="seconds"
        )
        cursor = self.conn.execute(
            "DELETE FROM provider_usage WHERE at < ?", (cutoff,)
        )
        self.conn.commit()
        return cursor.rowcount

    # --- artifacts ---------------------------------------------------------

    def save_selection(self, identity_key, kind, bullet_ids=None, letter=None,
                       mapping=None, keywords=None, master_fingerprint=None,
                       model=None):
        """
        Summary:
            Record what a document is made of, replacing any earlier record of
            the same kind.

        Parameters:
            identity_key (str): The identity the document is for.
            kind (str): `resume` or `cover_letter`. Unique together with
                `identity_key`, so regenerating replaces.
            bullet_ids (list[int] | None): Experience row ids in render order.
            letter (Mapping | None): The four-part covering letter.
            mapping (list | None): Requirement-to-bullet pairs the letter was
                argued from, kept so a letter can be audited against it later.
            keywords (list[str] | None): What the selection was scored against.
            master_fingerprint (str | None): Hash of the master at selection
                time, so a since-edited master is detectable.
            model (str | None): Which model wrote the letter.

        Raises:
            sqlite3.Error: If the upsert or the commit fails.

        Note:
            Commits. Stores no document text beyond the letter itself - the
            resume is re-rendered from these ids, which is what keeps an edited
            bullet from leaving stale copies behind.
        """
        self.conn.execute(
            """
            INSERT INTO job_artifacts (
                identity_key, kind, bullet_ids, letter_json, mapping_json,
                keywords, master_fingerprint, model, generated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_key, kind) DO UPDATE SET
                bullet_ids = excluded.bullet_ids,
                letter_json = excluded.letter_json,
                mapping_json = excluded.mapping_json,
                keywords = excluded.keywords,
                master_fingerprint = excluded.master_fingerprint,
                model = excluded.model,
                generated_at = excluded.generated_at
            """,
            (
                identity_key, kind,
                json.dumps(bullet_ids) if bullet_ids is not None else None,
                json.dumps(letter) if letter is not None else None,
                json.dumps(mapping) if mapping is not None else None,
                json.dumps(keywords) if keywords is not None else None,
                master_fingerprint, model, _now(),
            ),
        )
        self.conn.commit()

    def selection_for(self, identity_key, kind):
        """
        Summary:
            Read back what one document is made of.

        Parameters:
            identity_key (str): The identity to look up.
            kind (str): `resume` or `cover_letter`.

        Returns:
            dict | None: The record with its JSON columns already decoded, or
                None when nothing has been recorded.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            Decodes here so no caller has to know which columns are JSON. A
            column that fails to parse comes back empty rather than raising -
            a corrupt record should cost one document, not the page.
        """
        row = self.conn.execute(
            "SELECT * FROM job_artifacts WHERE identity_key = ? AND kind = ?",
            (identity_key, kind),
        ).fetchone()
        if row is None:
            return None

        def _decode(value, fallback):
            try:
                return json.loads(value) if value else fallback
            except (TypeError, ValueError):
                return fallback

        record = dict(row)
        record["bullet_ids"] = _decode(record.get("bullet_ids"), [])
        record["letter"] = _decode(record.get("letter_json"), {})
        record["mapping"] = _decode(record.get("mapping_json"), [])
        record["keywords"] = _decode(record.get("keywords"), [])
        return record

    def selections_for(self, identity_key):
        """
        Summary:
            List every document recorded for a job identity.

        Parameters:
            identity_key (str): The identity to look up.

        Returns:
            list[dict]: Decoded records ordered by kind, so the same document
                always appears in the same place in the UI.

        Raises:
            sqlite3.Error: If the query fails.
        """
        kinds = [row["kind"] for row in self.conn.execute(
            "SELECT kind FROM job_artifacts WHERE identity_key = ? ORDER BY kind",
            (identity_key,),
        ).fetchall()]
        return [self.selection_for(identity_key, kind) for kind in kinds]

    # --- experiences -------------------------------------------------------

    def add_experience(self, entry):
        """
        Summary:
            Add one experience bullet to the pool the resume generator draws
            from.

        Parameters:
            entry (dict): The bullet. `bullet` is required; `kind`
                (defaults to "work"), `organisation`, `role`, `start_date`,
                `end_date`, `tags`, `impact`, and `sort_order` (defaults to 0)
                are optional.

        Returns:
            int: The new row's ID.

        Raises:
            KeyError: If `entry` has no `bullet`.
            sqlite3.Error: If the insert or the commit fails.

        Note:
            Commits. Always inserts - there is no dedupe, so calling twice
            with the same text stores it twice.
        """
        cursor = self.conn.execute(
            """
            INSERT INTO experiences (
                kind, organisation, role, start_date, end_date, bullet, tags,
                impact, sort_order, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.get("kind", "work"),
                entry.get("organisation"),
                entry.get("role"),
                entry.get("start_date"),
                entry.get("end_date"),
                entry["bullet"],
                entry.get("tags"),
                entry.get("impact"),
                entry.get("sort_order", 0),
                _now(),
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def list_experiences(self, kind=None):
        """
        Summary:
            List experience bullets in the order they should appear on a
            resume.

        Parameters:
            kind (str | None): Restrict to one kind, for example "work". None
                or empty returns every kind.

        Returns:
            list[sqlite3.Row]: Bullets ordered by explicit `sort_order`, then
                most recent `end_date`, then ID. A NULL `end_date` sorts as
                "9999" so current roles come first.

        Raises:
            sqlite3.Error: If the query fails.
        """
        if kind:
            return self.conn.execute(
                """
                SELECT * FROM experiences WHERE kind = ?
                ORDER BY sort_order, COALESCE(end_date, '9999'), id
                """,
                (kind,),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM experiences ORDER BY sort_order, COALESCE(end_date, '9999'), id"
        ).fetchall()

    def delete_experience(self, experience_id):
        """
        Summary:
            Permanently remove an experience bullet.

        Parameters:
            experience_id (int): The `experiences.id` to delete.

        Raises:
            sqlite3.Error: If the delete or the commit fails.

        Note:
            Commits, and there is no undo. An unknown ID deletes nothing and
            does not raise. Already-generated resumes are unaffected; they are
            files on disk, not references to this row.
        """
        self.conn.execute("DELETE FROM experiences WHERE id = ?", (experience_id,))
        self.conn.commit()

    # --- sender denylist ---------------------------------------------------

    def deny_sender(self, domain, reason=None):
        """Mark a domain as never job related. Feeds rough-filter rule 3.

        Summary:
            Add a sender domain to the denylist so its mail is dropped before
            any model sees it.

        Parameters:
            domain (str | None): The domain to deny. Trimmed and lowercased
                before storage, so casing and stray whitespace do not create
                duplicates.
            reason (str | None): Why it was denied, for later review.

        Returns:
            bool: True when a usable domain was supplied, False when it was
                empty or None. False means nothing was stored.

        Raises:
            sqlite3.Error: If the insert or the commit fails.

        Note:
            Commits. Re-denying an existing domain is ignored and still
            returns True. This is the cheapest filter stage - every domain
            added here is mail the LLM is no longer paid to reject.
        """
        domain = (domain or "").strip().lower()
        if not domain:
            return False
        self.conn.execute(
            "INSERT OR IGNORE INTO sender_denylist (domain, added_at, reason) VALUES (?, ?, ?)",
            (domain, _now(), reason),
        )
        self.conn.commit()
        return True

    def allow_sender(self, domain):
        """
        Summary:
            Remove a domain from the denylist so its mail is filtered normally
            again.

        Parameters:
            domain (str | None): The domain to allow. Trimmed and lowercased
                to match how `deny_sender` stored it.

        Raises:
            sqlite3.Error: If the delete or the commit fails.

        Note:
            Commits. A domain that was never denied deletes nothing and does
            not raise. Mail already dropped is not reconsidered - this only
            affects messages arriving from now on.
        """
        self.conn.execute(
            "DELETE FROM sender_denylist WHERE domain = ?", ((domain or "").strip().lower(),)
        )
        self.conn.commit()

    def denied_domains(self):
        """
        Summary:
            Return every denied sender domain.

        Returns:
            set[str]: The denied domains, lowercased. A set so the rough
                filter can test membership per message.

        Raises:
            sqlite3.Error: If the query fails.
        """
        return {
            row["domain"]
            for row in self.conn.execute("SELECT domain FROM sender_denylist")
        }

    # --- cursors -----------------------------------------------------------
    #
    # Sync state (Gmail historyId, backfill position) lives in the existing
    # profile key/value table under a prefix, rather than earning a table of
    # its own for three rows.

    CURSOR_PREFIX = "cursor:"

    def get_cursor(self, name, default=None):
        """
        Summary:
            Read a sync cursor, falling back to a default when unset.

        Parameters:
            name (str): Cursor name without the prefix, for example the Gmail
                history position. `CURSOR_PREFIX` is added here.
            default (Any): Returned when the cursor has never been written.

        Returns:
            str | Any: The stored value as a string, or `default`.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            Always returns a string when set, because `set_cursor` stringifies
            on the way in. Numeric cursors need converting by the caller.
        """
        row = self.conn.execute(
            "SELECT value FROM profile WHERE key = ?", (self.CURSOR_PREFIX + name,)
        ).fetchone()
        return row["value"] if row else default

    def set_cursor(self, name, value):
        """
        Summary:
            Write a sync cursor, overwriting any previous value.

        Parameters:
            name (str): Cursor name without the prefix.
            value (Any): The position to store. Stringified unless None, which
                is stored as NULL to mean "no position".

        Raises:
            sqlite3.Error: If the upsert or the commit fails.

        Note:
            Commits, because losing a cursor means re-walking mail that was
            already processed. Cursors live in the `profile` key/value table
            under `CURSOR_PREFIX` rather than in a table of their own.
        """
        self.conn.execute(
            """
            INSERT INTO profile (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (self.CURSOR_PREFIX + name, None if value is None else str(value)),
        )
        self.conn.commit()

    def commit(self):
        """
        Summary:
            Commit the shared connection.

        Raises:
            sqlite3.Error: If the commit fails.

        Note:
            Exists because several methods here deliberately do not commit -
            `upsert_message`, `set_filter_verdict`, `store_body`,
            `record_category`, `link_message`, and `upsert_lead`. A pipeline
            stage batches many of those and calls this once. Forgetting it
            loses the batch.
        """
        self.conn.commit()
