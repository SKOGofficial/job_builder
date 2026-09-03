"""Job alerts to leads.

One alert email carries five to ten postings, so this is the stage that links
one message to many identities - the reason `message_links` is a link table
rather than a column on `messages`.

Two guards matter here:

- **Dedupe against applications.** Boards keep recommending a role you applied
  to three weeks ago. A to-apply list that resurrects finished work stops being
  trusted, and untrusted lists get ignored.
- **Never create a `jobs` row.** An alert is an advert, not evidence of
  anything. Only an acknowledgement email or an explicit user action promotes a
  lead into an application, or the dashboard fills with roles nobody applied to
  and every metric that divides by application count becomes meaningless.
"""

import asyncio
import logging

from clients.llm_client import GroqRateLimited
from clients.providers.base import ProviderUnavailable
from pipeline.parsers import parse_alert
from utilities.identity import identity_key, identity_scheme
from utilities.mailstore import CATEGORY_ALERT, alert_staleness_days

log = logging.getLogger(__name__)


def _received_ts(message):
    """The alert's received timestamp, or None.

    Summary:
        Read `received_ts` off a message row without assuming it is present.

    Parameters:
        message (Mapping): The message row or dict.

    Returns:
        int | None: The epoch seconds, or None when the column is absent or
            empty.

    Note:
        A `sqlite3.Row` raises `IndexError` for an unknown column rather than
        returning None, and a row fetched through a statement cached before a
        migration can come back without one - the same guard the orchestrator
        uses for `list_unsubscribe`.
    """
    try:
        return message["received_ts"]
    except (IndexError, KeyError):
        return None


class AlertHandler:
    """Turns alert emails into leads.

    Parsing an alert means a blocking model call, and this runs on the same
    event loop as the web UI, so that call goes to an executor. Everything
    after it is database work and stays on the calling thread, which is the one
    that owns the sqlite connection.
    """

    def __init__(self, store, mail, client=None, executor=None,
                 staleness_days=None):
        self.store = store
        self.mail = mail
        self.client = client
        self.executor = executor or asyncio.to_thread
        #: Read once per handler rather than per message, so a setting changed
        #: mid-pass cannot make one batch inconsistent with itself.
        self.staleness_days = (staleness_days if staleness_days is not None
                               else alert_staleness_days(store))
        #: Alerts retired unextracted this pass, and messages that failed for a
        #: reason of their own. Both reported by `run`.
        self.retired = 0
        self.failed = 0

    async def handle(self, message):
        """Process one alert email. Returns (created, skipped, linked).

        Summary:
            Extract every posting from one alert email and record the ones not
            already applied to as leads.

        Parameters:
            message (Mapping): The stored message row to process.

        Returns:
            tuple[int, int, int]: Leads created, postings skipped because they
                are already applied to, and message links written.

        Raises:
            GroqRateLimited: Propagated from parsing so `run` can stop the
                batch cleanly rather than logging a false parse failure.
        """
        postings = await self.executor(parse_alert, dict(message), self.client)
        if not postings:
            if self.client is None:
                # Not a real attempt. `parse_alert` gives up immediately when
                # no deterministic parser claims the message and there is no
                # model to fall back on, so marking this handled would discard
                # a perfectly parseable digest because a provider happened to
                # be cooling off. Left for a cycle that can actually try.
                log.info("Alert %s left for a cycle with a model available",
                         message["gmail_message_id"])
                return 0, 0, 0
            # Marked handled all the same. Plenty of board mail carries no
            # posting at all, and without this the same empty digest is
            # re-extracted at full model cost on every cycle for ever.
            log.info("No postings extracted from alert %s",
                     message["gmail_message_id"])
            self.mail.mark_handled(message["gmail_message_id"])
            self.mail.commit()
            return 0, 0, 0

        # When the role was advertised. The alert's own received time, because
        # boards do not put a posting date in the mail - see `migrate_v6`.
        posted_ts = _received_ts(message)

        created = skipped = linked = 0
        for posting in postings:
            key = identity_key(posting.title, posting.company, posting.location)

            # Already applied to this role - do not resurrect it as a lead.
            if self.store.job_by_identity(key) is not None:
                skipped += 1
                if self.mail.link_message(message["gmail_message_id"], key,
                                          CATEGORY_ALERT, 1.0, "alert_parser"):
                    linked += 1
                continue

            is_new = self.mail.upsert_lead({
                "identity_key": key,
                "identity_scheme": identity_scheme(posting.location),
                "title": posting.title,
                "company": posting.company,
                "location": posting.location,
                "apply_url": posting.apply_url,
                "tracking_url": posting.tracking_url,
                "board": posting.board,
                "board_job_id": posting.board_job_id,
                "source_message_id": message["gmail_message_id"],
                "posted_ts": posted_ts,
            })
            created += int(is_new)

            # Link whether the lead is new or a repeat sighting: the timeline
            # should show every alert that surfaced this role, and the link
            # survives promotion because it points at the identity.
            if self.mail.link_message(message["gmail_message_id"], key,
                                      CATEGORY_ALERT, 1.0, "alert_parser"):
                linked += 1

        self.mail.mark_handled(message["gmail_message_id"])
        self.mail.commit()
        log.info("Alert %s produced %d new lead(s), %d already applied to",
                 message["gmail_message_id"], created, skipped)
        return created, skipped, linked

    async def run(self, limit=50):
        """Process every alert email that has not been linked yet.

        Summary:
            Turn each unhandled alert email into leads, stopping cleanly if the
            model's rate limit is reached.

        Parameters:
            limit (int): Most alert emails to process in one pass.

        Returns:
            tuple[int, int, int]: Leads created, postings skipped because they
                are already applied to, and message links written.

        Note:
            A rate limit ends the pass rather than failing it. Everything
            already written is kept, and the emails not reached stay unlinked,
            so the next cycle picks them up with a fresh token budget.

            Retiring stale alerts is not done here. It costs no model call, so
            it belongs outside the provider gate - `PipelineCycle` runs it
            unconditionally, next to the other free stages. Doing it here meant
            that a cooling-off pool, which skips `dispatch` entirely, also
            skipped the one piece of backlog work that needed no provider at
            all. `_pending` still refuses stale rows, so this handler cannot
            extract one whether or not the retirement has run.
        """
        totals = [0, 0, 0]
        for message in self._pending(limit):
            try:
                created, skipped, linked = await self.handle(message)
            except ProviderUnavailable as exc:
                # Not a rate limit and not a parse failure: the request never
                # reached a model. Stop the pass and leave every message in it
                # untouched - crucially *without* `mark_handled`, because
                # nothing was tried, and a retry is exactly what these need.
                log.warning(
                    "Alert handling stopped after %d lead(s): no provider could serve "
                    "the request (%s). Nothing was marked handled, so the whole batch "
                    "retries next cycle.",
                    totals[0], exc,
                )
                break
            except GroqRateLimited as exc:
                log.info(
                    "Alert handling paused by the rate limit after %d lead(s); "
                    "the remaining alerts retry next cycle, in about %ss",
                    totals[0], exc.retry_after,
                )
                break
            except Exception:
                # A failure specific to this message - a body no parser can
                # read, a posting with no title - is not a reason to abandon
                # the ones behind it. AGENTS.md has said so since the router
                # learned it; this handler had not, so anything but a rate
                # limit propagated out of `dispatch` and took the whole
                # `prepare` stage down with it for that cycle.
                #
                # Marked handled, not left pending: it *was* tried, and a
                # message that fails identically every cycle is exactly what
                # `handled_at` exists to stop being re-charged for.
                log.exception(
                    "Alert %s could not be extracted; marking it handled and "
                    "continuing", message["gmail_message_id"],
                )
                self.mail.mark_handled(message["gmail_message_id"])
                self.failed += 1
                continue
            totals[0] += created
            totals[1] += skipped
            totals[2] += linked
        if self.failed:
            log.warning("%d alert(s) failed to extract this pass", self.failed)
        return tuple(totals)

    def _pending(self, limit):
        """Alerts not yet extracted, oldest first.

        Summary:
            List the alert emails this handler still has to process.

        Parameters:
            limit (int): Most rows to return.

        Returns:
            list[sqlite3.Row]: Unhandled alert messages.

        Note:
            Keyed on `handled_at`, not on the absence of a link. A digest whose
            postings have all been applied to already links to nothing new, and
            under the old query that made it permanently pending.

            Oldest first, which is a reversal. Newest-first was the right call
            while the queue held seven weeks of alerts - a fresh posting is
            worth more than an old one - but it starved the tail permanently:
            469 alerts draining at fifteen a cycle never reached July. Now that
            anything past the staleness cutoff is retired unextracted,
            everything left is inside the window and worth extracting, so the
            fair order is the one where nothing waits for ever.

            The cutoff is applied here as well as by the retirement pass. The
            two run in different stages, and a handler that would spend a model
            call on a stale alert simply because the cheap pass had not got to
            it yet is a handler with a hole in it.
        """
        return self.mail.messages_awaiting_handling(
            CATEGORY_ALERT, limit, newest_first=False,
            newer_than_days=self.staleness_days)
