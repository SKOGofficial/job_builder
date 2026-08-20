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
        message_ids, new_cursor = await self.executor(
            gmail_client.list_history, cursor, creds)
        stored = await self._store_headers(message_ids, creds, max_messages)
        if new_cursor:
            self.mail.set_cursor(CURSOR_HISTORY_ID, new_cursor)
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

        stored = await self._store_headers(message_ids, creds, max_messages)
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
        """
        if not message_ids:
            return 0
        unseen = [mid for mid in message_ids
                  if mid not in self.mail.known_message_ids(message_ids)]
        if max_messages:
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
                # race. Nothing to store and nothing to retry - the history
                # cursor moves past it either way.
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
        return stored

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
