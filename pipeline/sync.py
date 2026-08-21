"""Pulling Gmail into the local mirror.

Two paths, and choosing between them correctly is what keeps the poller cheap:

- **Incremental** (`users.history.list`) is the steady-state path. Two quota
  units per page, and it returns only what changed rather than re-walking the
  mailbox. Requires a stored `historyId` from a previous run.
- **Bounded full sync** (`users.messages.list`) seeds a new install and
  recovers when the history cursor has expired. Gmail keeps history for about
  a week, so any downtime longer than that lands here. Bounded by a date cutoff
  because a decade-old mailbox is not something to walk by accident.

Headers only at this stage. Bodies are fetched later, and only for messages the
rough filter passed, so mail that is obviously not job related costs one
metadata call.

Every blocking Google call goes through an injectable executor; database access
stays on the calling thread, which is the one that owns the sqlite connection.
Same contract as `GmailScanner`, for the same reason.
"""

import asyncio
import logging
from datetime import date, timedelta

from clients import gmail_client
from clients.gmail_client import GmailHistoryExpired, GmailMessageGone

log = logging.getLogger(__name__)

CURSOR_HISTORY_ID = "gmail_history_id"
CURSOR_LAST_SYNC = "gmail_last_sync"

#: How far back a first run reaches. A year covers an active job search without
#: turning the initial backfill into an overnight job.
DEFAULT_BACKFILL_DAYS = 365


class MailboxSync:
    """Keeps `messages` in step with the mailbox."""

    def __init__(self, mail, executor=None, credential_loader=None,
                 backfill_days=DEFAULT_BACKFILL_DAYS):
        self.mail = mail
        self.executor = executor or asyncio.to_thread
        self.credential_loader = credential_loader or gmail_client.load_credentials
        self.backfill_days = backfill_days
        self.last_error = None
        #: Messages confirmed deleted this run, so a held cursor cannot spend
        #: every pass re-asking for the same corpses. Cleared the moment the
        #: cursor advances, since nothing past it can be listed again - which
        #: is also what stops this growing without bound in a service that
        #: stays up for months.
        self._gone = set()

    async def run(self, max_messages=None):
        """One sync pass. Returns the number of new messages stored."""
        self.last_error = None
        try:
            creds = await self.executor(self.credential_loader)
        except Exception as exc:
            self.last_error = f"Gmail unavailable: {exc}"
            log.warning("Sync skipped: %s", exc)
            return 0

        cursor = self.mail.get_cursor(CURSOR_HISTORY_ID)
        if cursor:
            try:
                return await self._incremental(creds, cursor, max_messages)
            except GmailHistoryExpired:
                log.info("History cursor %s expired; falling back to a bounded "
                         "full sync", cursor)
            except Exception as exc:
                self.last_error = f"Incremental sync failed: {exc}"
                log.exception("Incremental sync failed")
                return 0

        try:
            return await self._full(creds, max_messages)
        except Exception as exc:
            self.last_error = f"Full sync failed: {exc}"
            log.exception("Full sync failed")
            return 0

    async def _incremental(self, creds, cursor, max_messages):
        """One pass down the history path.

        Summary:
            Fetch what changed since the cursor, advancing it only if the
            whole window was covered.

        Parameters:
            creds: Gmail credentials.
            cursor (str): The stored history ID to read forward from.
            max_messages (int | None): Cap on messages fetched this pass.

        Returns:
            int: How many new messages were stored.

        Note:
            **The cursor is held when the batch was capped.** It used to
            advance regardless, which quietly threw away every message past
            `max_messages` in one window: the ids were never fetched, and the
            new cursor meant they could never be listed again. Holding it means
            the next pass re-lists the same window, skips what is already
            mirrored, and takes the next batch. Rare at a ten-minute cadence,
            and near-certain on the first pass after a long outage - which is
            exactly when losing mail matters most and is least visible.
        """
        message_ids, new_cursor = await self.executor(
            gmail_client.list_history, cursor, creds)
        stored, remaining = await self._store_headers(
            message_ids, creds, max_messages)

        if remaining:
            log.info(
                "Holding the history cursor at %s: %d message(s) in this "
                "window are still unfetched. The next pass continues from "
                "here.", cursor, remaining,
            )
        elif new_cursor:
            self.mail.set_cursor(CURSOR_HISTORY_ID, new_cursor)
            # Nothing before the new cursor can be listed again, so the
            # deleted-id memory has done its job.
            self._gone.clear()

        self._stamp()
        log.info("Incremental sync stored %d new message(s)", stored)
        return stored

    async def _full(self, creds, max_messages):
        """Bounded walk, used to seed and to recover.

        The historyId is read *before* listing, not after. Taking it afterwards
        would skip anything that arrived during the walk - a race that silently
        loses mail exactly when the mailbox is busiest.
        """
        profile = await self.executor(gmail_client.get_profile, creds)
        starting_history_id = profile.get("historyId")

        since = date.today() - timedelta(days=self.backfill_days)
        query = f"after:{since.strftime('%Y/%m/%d')}"
        message_ids = await self.executor(
            gmail_client.iter_message_ids, query, creds, max_messages)

        stored, _remaining = await self._store_headers(
            message_ids, creds, max_messages)
        # The cursor here is the historyId read *before* the walk, not a
        # position within it, so a capped walk still leaves a correct
        # re-seed point. The walk's own bound is the documented one.
        if starting_history_id:
            self.mail.set_cursor(CURSOR_HISTORY_ID, starting_history_id)
        self._stamp()
        log.info("Full sync walked %d message(s), stored %d new",
                 len(message_ids), stored)
        return stored

    async def _store_headers(self, message_ids, creds, max_messages):
        """Fetch headers for IDs we have not seen and store them.

        The bulk existence check matters on a backfill: without it a resumed
        run re-fetches every message it already has, which is the difference
        between minutes and hours.

        Summary:
            Mirror the headers of every message not already stored, up to the
            per-pass cap.

        Parameters:
            message_ids (list[str]): Candidate IDs from the listing.
            creds: Gmail credentials.
            max_messages (int | None): Most messages to fetch this pass.

        Returns:
            tuple[int, int]: How many were stored, and how many unseen IDs were
                left untouched by the cap. The caller needs the second number
                to decide whether it may advance the history cursor.

        Note:
            Confirmed-deleted IDs are remembered rather than merely skipped.
            They can never be stored, so they stay "unseen" for ever, and with
            the cursor now held on a capped batch they would otherwise fill
            every subsequent batch with the same dead ids and never reach the
            live mail behind them.
        """
        if not message_ids:
            return 0, 0
        known = self.mail.known_message_ids(message_ids)
        unseen = [mid for mid in message_ids
                  if mid not in known and mid not in self._gone]
        remaining = 0
        if max_messages and len(unseen) > max_messages:
            remaining = len(unseen) - max_messages
            unseen = unseen[:max_messages]

        stored = gone = 0
        for message_id in unseen:
            try:
                header = await self.executor(
                    gmail_client.get_message_headers, message_id, creds)
            except GmailMessageGone:
                # Routine, so no traceback: the message was deleted between
                # Gmail listing it and this fetch. `list_history` already drops
                # the deletions it can see, and this is the remainder of that
                # race. Nothing to store and nothing to retry.
                self._gone.add(message_id)
                gone += 1
                continue
            except Exception:
                # One unreadable message must not abort the pass. This one is
                # worth a traceback, because unlike a deletion it is not
                # expected, and the message stays unstored so a later run
                # picks it up again.
                log.warning("Could not fetch headers for %s", message_id,
                            exc_info=True)
                continue
            if self.mail.upsert_message(header):
                stored += 1
        self.mail.commit()
        if gone:
            log.info("%d message(s) were deleted before they could be "
                     "mirrored", gone)
        return stored, remaining

    def _stamp(self):
        from datetime import datetime

        self.mail.set_cursor(CURSOR_LAST_SYNC,
                             datetime.now().isoformat(timespec="seconds"))

    def reset_cursor(self):
        """Force the next run onto the full-sync path."""
        self.mail.set_cursor(CURSOR_HISTORY_ID, None)


class BodyFetcher:
    """Downloads bodies for messages the rough filter passed."""

    def __init__(self, mail, executor=None, credential_loader=None):
        self.mail = mail
        self.executor = executor or asyncio.to_thread
        self.credential_loader = credential_loader or gmail_client.load_credentials

    async def run(self, limit=50, creds=None):
        pending = self.mail.messages_awaiting_body(limit)
        if not pending:
            return 0
        if creds is None:
            try:
                creds = await self.executor(self.credential_loader)
            except Exception as exc:
                log.warning("Body fetch skipped: %s", exc)
                return 0

        fetched = gone = 0
        for row in pending:
            message_id = row["gmail_message_id"]
            try:
                payload = await self.executor(
                    gmail_client.get_message_body, message_id, creds)
            except GmailMessageGone:
                # The message was mirrored and has since been deleted, so the
                # body is never coming. Store an empty one rather than skipping.
                #
                # Skipping would be a permanent loop: `messages_awaiting_body`
                # selects on `body_text IS NULL` alone, so a row left NULL comes
                # back on every cycle for ever, spending an API call each time
                # to be told again that the message is gone. Same reasoning as
                # the empty-body case below - it is the NULL that has to go.
                self.mail.store_body(message_id, "", row["snippet"])
                gone += 1
                continue
            except Exception:
                # Not expected, so it keeps its traceback, and the row is left
                # NULL deliberately: this one really should be retried.
                log.warning("Could not fetch body for %s", message_id,
                            exc_info=True)
                continue
            # An empty body is stored as an empty string rather than left NULL,
            # so the message is not re-fetched forever on every pass.
            self.mail.store_body(message_id, payload.get("body") or "",
                                 payload.get("snippet"))
            fetched += 1
        self.mail.commit()
        log.info("Fetched %d message bod%s", fetched, "y" if fetched == 1 else "ies")
        if gone:
            log.info("%d mirrored message(s) have since been deleted; stored "
                     "an empty body so they are not re-fetched", gone)
        return fetched
