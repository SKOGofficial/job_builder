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
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from utilities.durations import spell_duration
from utilities.identity import company_slug

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

#: How many times a message may fail classification for a reason of its own
#: before the pipeline stops offering it.
#:
#: Three, not one: the common causes - a truncated body, a provider having a
#: bad minute, a model returning prose instead of JSON - are often transient,
#: and retiring a real recruiter email on one bad reply is the expensive
#: mistake. But the retries have to end. The model queue is oldest-first, so a
#: message no provider can ever answer sits at its head and is tried *first*,
#: at cost, on every cycle for ever. A counter is what turns "still pending"
#: into "cannot be classified, and here is why".
MAX_CLASSIFY_ATTEMPTS = 3

#: Recorded on a message that passed the filter, had its body fetched, and got
#: nothing back. Phrased as a finished decision rather than an error, because
#: that is what it is - there is no reply the model could give.
EMPTY_BODY_REASON = "The fetched body was empty; there is nothing to classify."

LEAD_NEW = "new"
LEAD_PREPARING = "preparing"
LEAD_READY = "ready"
LEAD_DISMISSED = "dismissed"
LEAD_APPLIED = "applied"
LEAD_STATUSES = (LEAD_NEW, LEAD_PREPARING, LEAD_READY, LEAD_DISMISSED, LEAD_APPLIED)

#: Statuses the to-apply list shows by default. `ready` first - that is the
#: state the user can actually act on.
LEAD_OPEN_STATUSES = (LEAD_READY, LEAD_PREPARING, LEAD_NEW)

#: How long a posting stays on the to-apply list. Two weeks after a board
#: advertised a role, applying to it is mostly a way to feel busy - the posting
#: has usually been filled or closed, and a to-apply list padded with those
#: stops being a list of things to do.
LEAD_FRESHNESS_DAYS = 14

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
    when = spell_duration(retry_after)
    if not when:
        return f"{PREPARE_WAITING_PREFIX}. Retrying next cycle."
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


def _percentile(sorted_values, fraction):
    """
    Summary:
        Read a percentile out of an already-sorted list.

    Parameters:
        sorted_values (list[int]): Values in ascending order.
        fraction (float): The percentile as 0.0-1.0, e.g. 0.95 for p95.

    Returns:
        int: The value at that position, or 0 for an empty list.

    Note:
        Nearest-rank, not interpolated. These are stage durations read by a
        human off a diagnostics page, where "the slow one took 4.2 seconds" is
        a real measurement and an interpolated 4.17 is an invented one.
    """
    if not sorted_values:
        return 0
    index = min(len(sorted_values) - 1,
                max(0, int(round(fraction * (len(sorted_values) - 1)))))
    return sorted_values[index]


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

            Keyed on `body_fetched_at`, **not** on `body_text IS NULL`. The two
            look interchangeable and are not: `prune_bodies` deliberately sets
            `body_text` back to NULL on old irrelevant mail, so a queue defined
            by that column would hand every pruned message straight back to the
            fetcher, to be downloaded and pruned again for ever. What this asks
            is "has it been fetched", and only the timestamp answers that -
            which is what `store_body` and `prune_bodies` have always claimed
            in their own docstrings.
        """
        return self.conn.execute(
            """
            SELECT * FROM messages
            WHERE filter_verdict = ? AND body_fetched_at IS NULL
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

            Messages that have already failed `MAX_CLASSIFY_ATTEMPTS` times are
            excluded too. Oldest-first ordering means an unanswerable message
            is offered *first* on every cycle, so without the ceiling one bad
            row is a permanent tax on the front of the queue.
        """
        sql = f"""
            SELECT * FROM messages
            WHERE body_text IS NOT NULL
              AND TRIM(body_text) <> ''
              AND category IS NULL
              AND classify_attempts < {MAX_CLASSIFY_ATTEMPTS}
            ORDER BY received_ts ASC
        """
        if limit is None:
            return self.conn.execute(sql).fetchall()
        return self.conn.execute(sql + " LIMIT ?", (limit,)).fetchall()

    def unclassified_headers(self, limit=None):
        """Every unclassified message, headers only.

        Summary:
            List the sender and subject of all messages awaiting a category.

        Parameters:
            limit (int | None): Most rows to return. None for all of them.

        Returns:
            list[sqlite3.Row]: `gmail_message_id`, `sender`, and `subject`,
                oldest first.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            Deliberately separate from `messages_awaiting_classification`, and
            deliberately without its body requirement or its limit. The rule
            tier reads only the sender and the subject, costs nothing, and has
            no reason to be rationed - so it sweeps the whole backlog while the
            limit stays where the expense is, on the model's share.

            Bodies are excluded rather than merely unused: a full backlog is
            thousands of rows, and loading their text to run a regex over the
            subject line would be the most expensive part of the cheap tier.

            Filtered-out mail is excluded, and that is the point of the verdict
            clause. Messages the rough filter dropped never get a body, so they
            can never leave `category IS NULL` - and this query, which had no
            verdict clause and no limit, re-read all 264 of them on every
            ten-minute cycle and declined all of them, for ever. They are not a
            backlog; they are a decision that was already made.
        """
        sql = f"""
            SELECT gmail_message_id, sender, subject FROM messages
            WHERE category IS NULL
              AND (filter_verdict IS NULL OR filter_verdict = '{VERDICT_PASSED}')
              AND classify_attempts < {MAX_CLASSIFY_ATTEMPTS}
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
            f"""
            SELECT COUNT(*) AS n FROM messages
            WHERE body_text IS NOT NULL AND TRIM(body_text) <> ''
              AND category IS NULL
              AND classify_attempts < {MAX_CLASSIFY_ATTEMPTS}
            """
        ).fetchone()["n"]

    def record_classify_failure(self, gmail_message_id, reason):
        """Count one failed attempt against a message, and say why.

        Summary:
            Increment `classify_attempts` and store the reason it failed.

        Parameters:
            gmail_message_id (str): The message that could not be classified.
            reason (str): What went wrong, truncated to 500 characters.

        Returns:
            int: The message's attempt count after this failure.

        Raises:
            sqlite3.Error: If the update fails.

        Note:
            Does not commit; the router commits its pass. Only failures
            specific to *this* message are counted. A rate limit applies to
            every message behind it as well and says nothing about this one, so
            counting it would retire a queue's worth of perfectly good mail for
            the crime of being at the front during a bad afternoon.
        """
        self.conn.execute(
            """
            UPDATE messages
            SET classify_attempts = classify_attempts + 1,
                classify_error = ?
            WHERE gmail_message_id = ?
            """,
            ((reason or "")[:500], gmail_message_id),
        )
        row = self.conn.execute(
            "SELECT classify_attempts AS n FROM messages "
            "WHERE gmail_message_id = ?",
            (gmail_message_id,),
        ).fetchone()
        return row["n"] if row else 0

    def retire_unclassifiable(self):
        """Give up, in writing, on mail there is nothing to classify.

        Summary:
            Mark passed messages whose fetched body turned out to be empty as
            permanently unclassifiable.

        Returns:
            int: How many messages were retired.

        Raises:
            sqlite3.Error: If the update or the commit fails.

        Note:
            Commits. A message that passed the filter, had its body fetched,
            and came back empty is excluded from the model queue by the
            non-empty-body guard - for a good reason, since there is nothing to
            send - but nothing ever marked it, so it sat in `category IS NULL`
            for ever, indistinguishable from mail that simply had not been
            reached yet. One such message has been in this mailbox since May.

            Retired by exhausting the attempt counter rather than by assigning
            a category, because no category would be true. The distinction the
            pipeline needs is "tried and cannot be answered", and that is what
            the counter means everywhere else.
        """
        cursor = self.conn.execute(
            f"""
            UPDATE messages
            SET classify_attempts = {MAX_CLASSIFY_ATTEMPTS},
                classify_error = ?
            WHERE category IS NULL
              AND filter_verdict = ?
              AND body_fetched_at IS NOT NULL
              AND (body_text IS NULL OR TRIM(body_text) = '')
              AND classify_attempts < {MAX_CLASSIFY_ATTEMPTS}
            """,
            (EMPTY_BODY_REASON, VERDICT_PASSED),
        )
        self.conn.commit()
        return cursor.rowcount

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
            resolved_by (str | None): What made the link - the name of the
                resolver stage or parser responsible for it.

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

    def mark_handled(self, gmail_message_id):
        """Record that a category handler has finished with this message.

        Summary:
            Stamp `handled_at` so a handler does not pick the message up again.

        Parameters:
            gmail_message_id (str): The message the handler has processed.

        Raises:
            sqlite3.Error: If the update fails.

        Note:
            Does not commit. Called on **every** outcome, including the ones
            that wrote no link. That is the point: a digest carrying no
            parseable posting produced no link, so a backlog query keyed on
            links alone re-extracted it at full model cost on every cycle, for
            ever. Marking it handled is what makes "tried and found nothing"
            different from "not tried yet".
        """
        self.conn.execute(
            "UPDATE messages SET handled_at = ? WHERE gmail_message_id = ?",
            (_now(), gmail_message_id),
        )

    def messages_handled_without_result(self, category=CATEGORY_ALERT):
        """Messages marked handled that produced no link at all.

        Summary:
            List messages in one category stamped `handled_at` but attached to
            no identity.

        Parameters:
            category (str): The category to inspect. Defaults to
                `CATEGORY_ALERT`, the one an outage actually damaged.

        Returns:
            list[sqlite3.Row]: Matching messages, oldest stamp first.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            Two very different things land here. A board digest that genuinely
            advertised nothing is *supposed* to look like this - that is the
            retry leak `handled_at` exists to close. So is an alert whose
            extraction call failed and was wrongly recorded as an attempt.
            Only a human can tell them apart, which is why this reports rather
            than repairs; `cli.py requeue` prints the list before touching it.
        """
        return self.conn.execute(
            """
            SELECT * FROM messages
            WHERE category = ?
              AND handled_at IS NOT NULL
              AND gmail_message_id NOT IN (SELECT gmail_message_id FROM message_links)
            ORDER BY handled_at ASC
            """,
            (category,),
        ).fetchall()

    def requeue_handled_without_result(self, category=CATEGORY_ALERT):
        """Clear `handled_at` on messages that were retired producing nothing.

        Summary:
            Return messages in one category to the handler backlog.

        Parameters:
            category (str): The category to requeue. Defaults to
                `CATEGORY_ALERT`.

        Returns:
            int: How many messages were requeued.

        Raises:
            sqlite3.Error: If the update or the commit fails.

        Note:
            Commits. Deliberately the blunt version: it puts back genuinely
            empty digests along with the damaged ones, costing one wasted
            extraction each. That asymmetry is the right way round - a wasted
            call is a fraction of a cent, and a lost alert is a job the user
            never sees.
        """
        cursor = self.conn.execute(
            """
            UPDATE messages
            SET handled_at = NULL
            WHERE category = ?
              AND handled_at IS NOT NULL
              AND gmail_message_id NOT IN (SELECT gmail_message_id FROM message_links)
            """,
            (category,),
        )
        self.conn.commit()
        if cursor.rowcount:
            log.info("Requeued %d %s message(s) that produced no link",
                     cursor.rowcount, category)
        return cursor.rowcount

    def messages_awaiting_handling(self, category, limit=50, newest_first=True):
        """A category handler's backlog.

        Summary:
            List classified messages in one category that no handler has
            processed yet.

        Parameters:
            category (str): The category to select, from `CATEGORIES`.
            limit (int): Most rows to return.
            newest_first (bool): Newest first for alerts, where a fresh posting
                matters more than an old one. Acknowledgements pass False -
                they are processed oldest first so that a lead is promoted by
                the receipt that actually came first.

        Returns:
            list[sqlite3.Row]: Unhandled messages in that category.

        Raises:
            sqlite3.Error: If the query fails.
        """
        order = "DESC" if newest_first else "ASC"
        return self.conn.execute(
            f"""
            SELECT * FROM messages
            WHERE category = ?
              AND handled_at IS NULL
            ORDER BY received_ts {order}
            LIMIT ?
            """,
            (category, limit),
        ).fetchall()

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
            Commits. Unlinking also makes the message eligible for body pruning
            again if it is `irrelevant` and old enough. It does not clear
            `handled_at`, so no handler picks it back up - removing a wrong link
            is not a request to re-run extraction.
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
                `source_message_id`, `posted_ts`, and `status` are optional.
                `status` defaults to `LEAD_NEW`.

        Returns:
            bool: True when a new lead was created, False when an existing one
                was refreshed.

        Raises:
            KeyError: If `identity_key` or `title` is absent.
            sqlite3.Error: If the insert or update fails.

        Note:
            Does not commit. On the refresh path the two URLs, `updated_at` and
            `posted_ts` are touched, and each is written with COALESCE so a
            None never erases a stored value. Status and relevance are never
            reset - doing so would resurrect a dismissed lead every time the
            posting was re-advertised.

            `posted_ts` takes the **later** of the stored and incoming values,
            so a role advertised again next week has its clock reset and gets
            another fortnight. A board still pushing a posting is the best
            evidence available that it is still open, and the alternative -
            first sighting wins - would expire a live role while the alerts for
            it were still arriving.
        """
        now = _now()
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO job_leads (
                identity_key, identity_scheme, title, company, location,
                apply_url, tracking_url, board, board_job_id, source_message_id,
                posted_ts, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                lead.get("posted_ts"),
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
                    -- NULLIF puts the NULL back when both sides are absent;
                    -- without it two unknowns would collapse to epoch 0 and
                    -- the lead would be expired as fifty years stale.
                    posted_ts = NULLIF(
                        MAX(COALESCE(?, 0), COALESCE(posted_ts, 0)), 0),
                    updated_at = ?
                WHERE identity_key = ?
                """,
                (lead.get("apply_url"), lead.get("tracking_url"),
                 lead.get("posted_ts"), now, lead["identity_key"]),
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
        """The to-apply list, newest posting first.

        Summary:
            List leads with the most recently advertised roles at the top.

        Parameters:
            statuses (tuple[str, ...] | None): Statuses to include. Defaults
                to `LEAD_OPEN_STATUSES`. Pass None for every lead regardless
                of status, which is what the "show dismissed" view uses.

        Returns:
            list[sqlite3.Row]: Matching leads, newest posting first, with
                relevance breaking ties within a posting date.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            Posting date is the primary sort, ahead of both `ready` and
            relevance. It used to be last, behind a `ready` bucket and the
            relevance score, which meant the top of the list was whatever the
            preparer had happened to finish - and a role advertised three weeks
            ago outranked one posted this morning. Applying early is worth more
            than any of the signals that were beating it, so the newest posting
            wins and relevance only breaks ties.

            `created_at` is the tiebreaker of last resort, never the sort key -
            see `migrate_v6` for why it cannot carry this.
        """
        order = (
            "ORDER BY COALESCE(posted_ts, 0) DESC, "
            "relevance_score DESC NULLS LAST, "
            "created_at DESC"
        )
        if statuses is None:
            return self.conn.execute(
                f"SELECT * FROM job_leads {order}"
            ).fetchall()
        marks = ", ".join("?" for _ in statuses)
        return self.conn.execute(
            f"SELECT * FROM job_leads WHERE status IN ({marks}) {order}",
            tuple(statuses),
        ).fetchall()

    def purge_stale_leads(self, older_than_days=LEAD_FRESHNESS_DAYS):
        """Drop open leads whose posting has gone stale.

        Summary:
            Delete still-open leads advertised longer ago than the freshness
            window.

        Parameters:
            older_than_days (int): Age in days past which an open lead is
                deleted. Defaults to `LEAD_FRESHNESS_DAYS`.

        Returns:
            int: How many leads were deleted.

        Raises:
            sqlite3.Error: If the delete or the commit fails.

        Note:
            Only `new`, `preparing`, and `ready` are touched. `applied` leads
            are the record that the user applied and are referenced by a real
            application, and `dismissed` leads are the memory of a decision -
            deleting those would let the next alert for the same role recreate
            it, and a to-apply list that keeps re-suggesting roles the user has
            already said no to is worse than one that is merely long.

            The date falls back to `created_at` when `posted_ts` is missing.
            Skipping dateless rows instead would make them permanent, which is
            the one outcome this method exists to prevent.
        """
        cutoff = int(time.time()) - older_than_days * 86400
        marks = ", ".join("?" for _ in LEAD_OPEN_STATUSES)
        cursor = self.conn.execute(
            f"""
            DELETE FROM job_leads
            WHERE status IN ({marks})
              AND COALESCE(
                    posted_ts,
                    CAST(strftime('%s', created_at) AS INTEGER)
                  ) < ?
            """,
            (*LEAD_OPEN_STATUSES, cutoff),
        )
        self.conn.commit()
        if cursor.rowcount:
            log.info("Purged %d lead(s) older than %d days",
                     cursor.rowcount, older_than_days)
        return cursor.rowcount

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
                `total_tokens`, `duration_ms`. Missing token counts record as
                0; a missing duration records as NULL, which is the honest
                value for a call that never reached a provider.

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
                 total_tokens, outcome, duration_ms, at)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
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
                    row.get("duration_ms"),
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

    # --- queue depths ------------------------------------------------------

    def queue_depths(self):
        """Every queue the pipeline drains, counted the same way it drains it.

        Summary:
            Report the depth of each pipeline queue, plus the rows that are
            stuck in none of them.

        Returns:
            dict[str, int]: `awaiting_filter`, `awaiting_body`,
                `awaiting_classification`, `awaiting_rules`, `dead_lettered`,
                and one `awaiting_handling_<category>` per job category.

        Raises:
            sqlite3.Error: If a query fails.

        Note:
            This exists because two honest counts of "unclassified" disagreed
            by 264. `count_awaiting_classification` required a body, so it read
            0; anything counting `category IS NULL` read 265, because messages
            the rough filter dropped never get a body and so never leave that
            set. Both were right about different things and neither said so.

            Every number here is produced by the same predicate the stage that
            drains it uses, so the page and the pipeline cannot drift apart.
        """
        one = lambda sql, args=(): self.conn.execute(sql, args).fetchone()["n"]
        depths = {
            "awaiting_filter": one(
                "SELECT COUNT(*) n FROM messages WHERE filter_verdict IS NULL"
            ),
            "awaiting_body": one(
                """
                SELECT COUNT(*) n FROM messages
                WHERE filter_verdict = ? AND body_fetched_at IS NULL
                """,
                (VERDICT_PASSED,),
            ),
            "awaiting_classification": self.count_awaiting_classification(),
            "awaiting_rules": one(
                f"""
                SELECT COUNT(*) n FROM messages
                WHERE category IS NULL
                  AND (filter_verdict IS NULL OR filter_verdict = ?)
                  AND classify_attempts < {MAX_CLASSIFY_ATTEMPTS}
                """,
                (VERDICT_PASSED,),
            ),
            "dead_lettered": one(
                f"""
                SELECT COUNT(*) n FROM messages
                WHERE category IS NULL
                  AND classify_attempts >= {MAX_CLASSIFY_ATTEMPTS}
                """
            ),
        }
        for category in (CATEGORY_ALERT, CATEGORY_UPDATE,
                         CATEGORY_ACKNOWLEDGEMENT):
            depths[f"awaiting_handling_{category}"] = one(
                "SELECT COUNT(*) n FROM messages "
                "WHERE category = ? AND handled_at IS NULL",
                (category,),
            )
        return depths

    def filtered_out(self):
        """
        Summary:
            Count the messages the rough filter dropped, by reason.

        Returns:
            dict[str, int]: Drop verdict mapped to how many messages carry it.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            Reported separately from `queue_depths` on purpose. These rows are
            not a backlog and never will be - they have no body and are not
            meant to have one - so putting them in the same table as the queues
            is what created the confusion in the first place.
        """
        rows = self.conn.execute(
            """
            SELECT filter_verdict AS verdict, COUNT(*) AS n FROM messages
            WHERE filter_verdict IS NOT NULL AND filter_verdict <> ?
            GROUP BY filter_verdict ORDER BY n DESC
            """,
            (VERDICT_PASSED,),
        ).fetchall()
        return {row["verdict"]: row["n"] for row in rows}

    # --- stage timings -----------------------------------------------------

    def record_stage_runs(self, rows):
        """Append what each stage of one cycle did, and how long it took.

        Summary:
            Write one `stage_runs` row per timed pipeline stage.

        Parameters:
            rows (list[dict]): Each with `cycle_id`, `stage`, `started_at`,
                `duration_ms`, and `outcome`; optionally `processed` and
                `detail`.

        Returns:
            int: How many rows were written.

        Raises:
            sqlite3.Error: If the write or the commit fails.

        Note:
            Commits, and takes a batch rather than a row. The cycle buffers its
            timings and flushes them once at the end for the same reason the
            provider ledger does: the measurement must not become a write in
            the middle of the thing it is measuring.
        """
        if not rows:
            return 0
        self.conn.executemany(
            """
            INSERT INTO stage_runs
                (cycle_id, stage, started_at, duration_ms, processed, outcome,
                 detail)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["cycle_id"],
                    row["stage"],
                    row["started_at"],
                    int(row.get("duration_ms") or 0),
                    int(row.get("processed") or 0),
                    row["outcome"],
                    row.get("detail"),
                )
                for row in rows
            ],
        )
        self.conn.commit()
        return len(rows)

    def recent_stage_runs(self, cycles=20):
        """The stages of the last few cycles, newest cycle first.

        Summary:
            List every `stage_runs` row belonging to the most recent cycles.

        Parameters:
            cycles (int): How many cycles back to read. Defaults to 20.

        Returns:
            list[sqlite3.Row]: Stage rows, newest cycle first and in stage
                order within a cycle.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            Selected by cycle rather than by row count. A cycle that skipped
            four stages writes fewer rows than one that ran them all, so a flat
            `LIMIT` would silently show more history for a broken pipeline than
            a working one - the opposite of what the page is for.
        """
        return self.conn.execute(
            """
            SELECT * FROM stage_runs
            WHERE cycle_id IN (
                SELECT cycle_id FROM stage_runs
                GROUP BY cycle_id ORDER BY MAX(started_at) DESC LIMIT ?
            )
            ORDER BY started_at DESC, id DESC
            """,
            (cycles,),
        ).fetchall()

    def stage_timings(self, since_iso):
        """How long each stage has been taking.

        Summary:
            Summarise duration and throughput per stage over a window.

        Parameters:
            since_iso (str): Inclusive lower bound as an ISO-8601 timestamp.

        Returns:
            list[dict]: One entry per stage with `stage`, `runs`, `processed`,
                `median_ms`, `p95_ms`, `max_ms`, and `failures`, slowest
                median first.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            Percentiles are computed in Python rather than SQL. sqlite has no
            percentile function, and a stage runs at most a few hundred times
            in a retention window - small enough that sorting the list costs
            less than the window function would.
        """
        rows = self.conn.execute(
            """
            SELECT stage, duration_ms, processed, outcome
            FROM stage_runs WHERE started_at >= ?
            """,
            (since_iso,),
        ).fetchall()

        by_stage = {}
        for row in rows:
            entry = by_stage.setdefault(
                row["stage"], {"durations": [], "processed": 0, "failures": 0}
            )
            entry["durations"].append(row["duration_ms"])
            entry["processed"] += row["processed"] or 0
            if row["outcome"] not in ("ok", "skipped"):
                entry["failures"] += 1

        summary = []
        for stage, entry in by_stage.items():
            durations = sorted(entry["durations"])
            summary.append({
                "stage": stage,
                "runs": len(durations),
                "processed": entry["processed"],
                "median_ms": _percentile(durations, 0.5),
                "p95_ms": _percentile(durations, 0.95),
                "max_ms": durations[-1] if durations else 0,
                "failures": entry["failures"],
            })
        summary.sort(key=lambda entry: entry["median_ms"], reverse=True)
        return summary

    def last_stage_errors(self):
        """The most recent failure of each stage, if it has ever failed.

        Summary:
            Report the latest non-ok outcome per stage.

        Returns:
            dict[str, dict]: Stage name mapped to `started_at`, `outcome`, and
                `detail`.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            This is the gap that let a stage fail on every single cycle without
            anything saying so: the scheduler only ever surfaced an error that
            reached the top of `PipelineCycle.run`, and a stage that logged and
            carried on never did.
        """
        rows = self.conn.execute(
            """
            SELECT stage, started_at, outcome, detail FROM stage_runs
            WHERE outcome NOT IN ('ok', 'skipped')
            GROUP BY stage
            HAVING started_at = MAX(started_at)
            """
        ).fetchall()
        return {
            row["stage"]: {
                "started_at": row["started_at"],
                "outcome": row["outcome"],
                "detail": row["detail"],
            }
            for row in rows
        }

    def prune_stage_runs(self, older_than_days=30):
        """
        Summary:
            Delete stage timing rows past the retention window.

        Parameters:
            older_than_days (int): Retention window in days. Defaults to 30.

        Returns:
            int: How many rows were deleted.

        Raises:
            sqlite3.Error: If the delete or the commit fails.

        Note:
            Commits. Seven stages every ten minutes is about a thousand rows a
            day, so this table outgrows `provider_usage` and is pruned on the
            same schedule for the same reason.
        """
        cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat(
            timespec="seconds"
        )
        cursor = self.conn.execute(
            "DELETE FROM stage_runs WHERE started_at < ?", (cutoff,)
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

    # --- contacts and referrals --------------------------------------------
    #
    # People worth asking for a referral. The matching surface is
    # `company_slug`, not the display name: a lead says "Stripe" and the user
    # typed "Stripe, Inc.", and only the reduced form brings the two together.
    # It is computed on the way in rather than at every read, so a company
    # renamed on the contact cannot silently stop matching.

    OUTREACH_DRAFTED = "drafted"
    OUTREACH_SENT = "sent"
    OUTREACH_SKIPPED = "skipped"

    def save_contact(self, contact):
        """Create a contact, or update the one whose id is given.

        Summary:
            Insert or update one referral contact, deriving its match slug.

        Parameters:
            contact (dict): `name` and `company` are required. `id`, `email`,
                `role`, `careers_url`, and `notes` are optional. An `id` that
                is present and not None updates that row; its absence inserts.

        Returns:
            int: The contact's row id, whether inserted or updated.

        Raises:
            KeyError: If `name` or `company` is absent.
            ValueError: If the company reduces to an empty match key, which
                would match every lead with an unnamed company rather than
                none of them.
            sqlite3.Error: If the write or the commit fails.

        Note:
            Commits, because this is a direct user edit rather than part of a
            batched pipeline pass - the surrounding methods that skip the
            commit all belong to the ingest stages.

            `last_checked_ts` and `archived` are deliberately not writable
            here. They are state the app maintains, and letting an edit of the
            name reset "what have I already seen" would silently repopulate the
            morning list.
        """
        now = _now()
        slug = company_slug(contact["company"])
        if not slug:
            raise ValueError(
                "Company %r reduces to an empty match key." % (contact["company"],)
            )
        values = (
            contact["name"].strip(),
            (contact.get("email") or "").strip() or None,
            contact["company"].strip(),
            slug,
            (contact.get("role") or "").strip() or None,
            (contact.get("careers_url") or "").strip() or None,
            (contact.get("notes") or "").strip() or None,
        )
        contact_id = contact.get("id")
        if contact_id:
            self.conn.execute(
                """
                UPDATE contacts
                SET name = ?, email = ?, company = ?, company_slug = ?,
                    role = ?, careers_url = ?, notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (*values, now, contact_id),
            )
        else:
            cursor = self.conn.execute(
                """
                INSERT INTO contacts (
                    name, email, company, company_slug, role, careers_url,
                    notes, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*values, now, now),
            )
            contact_id = cursor.lastrowid
        self.conn.commit()
        return contact_id

    def contact(self, contact_id):
        """
        Summary:
            Fetch one contact by row id.

        Parameters:
            contact_id (int): The `contacts.id` to read.

        Returns:
            sqlite3.Row | None: The contact, or None when the id is unknown.

        Raises:
            sqlite3.Error: If the query fails.
        """
        return self.conn.execute(
            "SELECT * FROM contacts WHERE id = ?", (contact_id,)
        ).fetchone()

    def list_contacts(self, include_archived=False):
        """
        Summary:
            List referral contacts, alphabetically by company then name.

        Parameters:
            include_archived (bool): True to include archived contacts.
                Defaults to False, which is what the morning list wants.

        Returns:
            list[sqlite3.Row]: Matching contacts.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            Ordered by company rather than by how recently they were added, so
            two people at the same company sit together - the pair share every
            match, and separating them would show the same postings twice in
            two distant places on the page.
        """
        where = "" if include_archived else "WHERE archived = 0"
        return self.conn.execute(
            f"SELECT * FROM contacts {where} "
            "ORDER BY company COLLATE NOCASE, name COLLATE NOCASE"
        ).fetchall()

    def set_contact_archived(self, contact_id, archived=True):
        """
        Summary:
            Archive or restore a contact.

        Parameters:
            contact_id (int): The `contacts.id` to update.
            archived (bool): True to archive, False to restore.

        Raises:
            sqlite3.Error: If the update or the commit fails.

        Note:
            Archiving rather than deleting, so the outreach history stays
            meaningful. Deleting the person you asked in March would leave a
            drafted email attached to nobody.
        """
        self.conn.execute(
            "UPDATE contacts SET archived = ?, updated_at = ? WHERE id = ?",
            (1 if archived else 0, _now(), contact_id),
        )
        self.conn.commit()

    def delete_contact(self, contact_id):
        """
        Summary:
            Delete a contact and every referral draft written for them.

        Parameters:
            contact_id (int): The `contacts.id` to remove.

        Returns:
            int: How many contact rows were deleted - 0 for an unknown id.

        Raises:
            sqlite3.Error: If either delete or the commit fails.

        Note:
            Takes the outreach rows with it, since they are meaningless without
            the person. `set_contact_archived` is the non-destructive option
            and the one the page offers by default.
        """
        self.conn.execute(
            "DELETE FROM referral_outreach WHERE contact_id = ?", (contact_id,)
        )
        cursor = self.conn.execute(
            "DELETE FROM contacts WHERE id = ?", (contact_id,)
        )
        self.conn.commit()
        return cursor.rowcount

    def mark_contact_checked(self, contact_id, ts=None):
        """Record that the user has seen this contact's current matches.

        Summary:
            Stamp a contact's last-checked time.

        Parameters:
            contact_id (int): The `contacts.id` to stamp.
            ts (int | None): Unix seconds to record. None uses now.

        Raises:
            sqlite3.Error: If the update or the commit fails.

        Note:
            This is what the "new" count is measured against, so it is the one
            write on this table that changes what the badge says. A posting
            advertised *after* this moment counts as new; everything at or
            before it has been seen.
        """
        self.conn.execute(
            "UPDATE contacts SET last_checked_ts = ?, updated_at = ? WHERE id = ?",
            (int(ts if ts is not None else time.time()), _now(), contact_id),
        )
        self.conn.commit()

    def record_outreach(self, contact_id, identity_key, subject, body,
                        model=None):
        """Store a drafted referral email, replacing any earlier draft.

        Summary:
            Save the referral email drafted for one contact about one role.

        Parameters:
            contact_id (int): Who the email is to.
            identity_key (str): The role it is about. Keyed on identity rather
                than on a lead id so the record survives the lead being
                promoted to a real application.
            subject (str): The drafted subject line.
            body (str): The drafted body.
            model (str | None): Which model wrote it.

        Returns:
            int: The `referral_outreach.id` of the stored draft.

        Raises:
            sqlite3.Error: If the write or the commit fails.

        Note:
            A redraft overwrites the text but **keeps the status and
            `sent_at`**. Redrafting an email already marked sent must not make
            the app forget it was sent - that would put the ask back on the
            morning list and risk a duplicate message to a real person.
        """
        now = _now()
        self.conn.execute(
            """
            INSERT INTO referral_outreach (
                contact_id, identity_key, subject, body, model, status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(contact_id, identity_key) DO UPDATE SET
                subject = excluded.subject,
                body = excluded.body,
                model = excluded.model
            """,
            (contact_id, identity_key, subject, body, model,
             self.OUTREACH_DRAFTED, now),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM referral_outreach "
            "WHERE contact_id = ? AND identity_key = ?",
            (contact_id, identity_key),
        ).fetchone()
        return row["id"]

    def outreach_for_contact(self, contact_id):
        """
        Summary:
            Every referral draft written for one contact, keyed by role.

        Parameters:
            contact_id (int): The `contacts.id` to read.

        Returns:
            dict[str, sqlite3.Row]: Identity key to outreach row.

        Raises:
            sqlite3.Error: If the query fails.

        Note:
            A mapping rather than a list, because the page's question is always
            "has this particular role been asked about", once per rendered row.
        """
        return {
            row["identity_key"]: row
            for row in self.conn.execute(
                "SELECT * FROM referral_outreach WHERE contact_id = ?",
                (contact_id,),
            )
        }

    def set_outreach_status(self, outreach_id, status):
        """
        Summary:
            Move a referral draft to a new status.

        Parameters:
            outreach_id (int): The `referral_outreach.id` to update.
            status (str): `drafted`, `sent`, or `skipped`.

        Raises:
            ValueError: If the status is not one of the three.
            sqlite3.Error: If the update or the commit fails.

        Note:
            Moving to `sent` stamps `sent_at`; moving away from it clears the
            stamp, so an accidental click is fully reversible rather than
            leaving a date behind that says an email went out when none did.
        """
        allowed = (self.OUTREACH_DRAFTED, self.OUTREACH_SENT,
                   self.OUTREACH_SKIPPED)
        if status not in allowed:
            raise ValueError("Unknown outreach status: %r" % (status,))
        self.conn.execute(
            "UPDATE referral_outreach SET status = ?, sent_at = ? WHERE id = ?",
            (status, _now() if status == self.OUTREACH_SENT else None,
             outreach_id),
        )
        self.conn.commit()

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
