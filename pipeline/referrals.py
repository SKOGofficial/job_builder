"""Matching the people you know against the roles coming in.

A referral is the highest-value signal in the whole pipeline and the only one
the tracker could not previously represent: it knew about a thousand postings
and nothing at all about the five people who could get an application read.

Two halves, deliberately unequal in cost:

- **Matching is free.** Leads already arrive from board alerts. Grouping them by
  company and joining to the contacts table costs one query, so it runs on every
  page load and needs no scheduling, no budget, and no provider.
- **Checking a careers page costs money.** So it never runs on a timer. The user
  presses a button for one company, and `OpeningsChecker` spends one grounded
  search against that company alone.

Matching happens at read time rather than being stamped onto a lead when it is
created. Contacts get added and edited *after* leads arrive - the first thing
anyone does with this feature is add five people and expect to see the postings
already sitting in the list - and a flag written at creation time would need a
backfill pass every time a contact changed.

Nothing here imports a UI framework, so the whole module is usable from `cli.py`
as well as from the page.
"""

import asyncio
import logging
import time

from utilities.identity import company_slug, identity_key, identity_scheme
from utilities.mailstore import LEAD_OPEN_STATUSES

log = logging.getLogger(__name__)

#: The board recorded on a lead that came from a careers-page check rather than
#: from an alert email. Deliberately visible on the lead card: these postings
#: were reported by a model reading the web rather than extracted from mail the
#: user actually received, and that difference in provenance should be legible
#: without opening anything.
BOARD_CAREERS_CHECK = "careers-check"


def _value(row, name, default=None):
    """Read a column that may be absent from an older row.

    Summary:
        Read a possibly-absent column off a row or dict.

    Parameters:
        row (Mapping): The row to read.
        name (str): The column name.
        default: Returned when the column is absent or empty.

    Returns:
        The column value, or `default`.

    Note:
        Same guard `web/pages/leads.py` and the orchestrator use: a
        `sqlite3.Row` fetched through a statement cached before a migration
        comes back without the new column and raises rather than returning None.
    """
    try:
        return row[name] or default
    except (IndexError, KeyError):
        return default


def leads_by_company(mail, statuses=LEAD_OPEN_STATUSES):
    """Group the open to-apply list by normalised company.

    Summary:
        Bucket open leads under the company slug they belong to.

    Parameters:
        mail (MailStore): The store to read leads from.
        statuses (tuple[str, ...] | None): Lead statuses to include. Defaults
            to the open ones.

    Returns:
        dict[str, list[sqlite3.Row]]: Company slug to leads, each bucket
            keeping `list_leads`' newest-posting-first order.

    Raises:
        sqlite3.Error: Propagated from the query.

    Note:
        One query for the whole page, bucketed in Python. The alternative - a
        query per contact - would be a dozen round trips to answer a question
        the fortnight-long freshness window keeps to a few hundred rows.
    """
    buckets = {}
    for lead in mail.list_leads(statuses):
        slug = company_slug(lead["company"])
        if not slug:
            # A lead whose company could not be identified matches nobody. It
            # must not fall into a shared empty-string bucket, or every contact
            # would appear to have a match.
            continue
        buckets.setdefault(slug, []).append(lead)
    return buckets


def matches_for(mail, contacts=None):
    """What each contact's company has open right now.

    Summary:
        Join referral contacts to the open leads at their companies.

    Parameters:
        mail (MailStore): The store to read from.
        contacts (list | None): Contacts to match. None reads the unarchived
            ones.

    Returns:
        list[dict]: One entry per contact, in `list_contacts` order, each with
            `contact`, `leads` (newest posting first), `new_count`, and
            `outreach` - the contact's drafts keyed by identity. Contacts whose
            company has nothing open are included with an empty `leads`, since
            "Datadog: nothing new" is the answer for most of them on most
            mornings and a page that hid them would look broken.

    Raises:
        sqlite3.Error: Propagated from the queries.
    """
    if contacts is None:
        contacts = mail.list_contacts()
    buckets = leads_by_company(mail)

    results = []
    for contact in contacts:
        leads = buckets.get(contact["company_slug"], [])
        results.append({
            "contact": contact,
            "leads": leads,
            "new_count": sum(1 for lead in leads if is_new_for(contact, lead)),
            "outreach": mail.outreach_for_contact(contact["id"]),
        })
    return results


def is_new_for(contact, lead):
    """Whether a posting arrived after the user last looked at this contact.

    Summary:
        Decide whether one lead counts as new for one contact.

    Parameters:
        contact (Mapping): The contact, carrying `last_checked_ts`.
        lead (Mapping): The lead, carrying `posted_ts`.

    Returns:
        bool: True when the posting is newer than the last check.

    Note:
        A contact never checked has *every* match new, which is what makes the
        badge appear the moment a contact is added at a company that already
        has openings. A lead with no posting date is treated as new for the
        same reason: the freshness window means it cannot be old, and silently
        hiding a posting is a worse failure than showing one twice.
    """
    checked = _value(contact, "last_checked_ts")
    if not checked:
        return True
    posted = _value(lead, "posted_ts")
    if not posted:
        return True
    return posted > checked


def new_match_count(mail):
    """The drawer badge: postings you have not looked at yet.

    Summary:
        Count new matches across every unarchived contact.

    Parameters:
        mail (MailStore): The store to read from.

    Returns:
        int: How many contact-and-posting pairs are new.

    Raises:
        sqlite3.Error: Propagated from the queries.

    Note:
        Counts pairs, not postings. Two colleagues at the same company both
        being able to refer you to one opening is two things to act on, and
        collapsing them would understate the morning's work.
    """
    contacts = mail.list_contacts()
    if not contacts:
        # The common case for anyone not using the feature, and worth the early
        # return: this runs inside `pending_counts` on every single page render.
        return 0
    buckets = leads_by_company(mail)
    return sum(
        1
        for contact in contacts
        for lead in buckets.get(contact["company_slug"], [])
        if is_new_for(contact, lead)
    )


class OpeningsChecker:
    """Checks one company's careers page on demand, and files what it finds.

    The button, not the timer. A grounded search per company per morning would
    be a standing daily bill for a question the mailbox usually answers for
    free, so this only ever runs when the user asks it to for one contact.

    Split across the thread boundary like every other stage: the model call goes
    to an executor, and every database read and write stays on the calling
    thread, which is the one that owns the sqlite connection.
    """

    def __init__(self, store, mail, client=None, executor=None):
        """
        Summary:
            Build a checker bound to a store, a mail store, and a client.

        Parameters:
            store (JobStore): Used to skip roles already applied to.
            mail (MailStore): Where contacts and leads live.
            client: Anything exposing `find_openings(contact)`, so a
                `ResearchTaskClient` from the pool or a test double both work.
                None makes `check` a no-op rather than an error.
            executor (Callable | None): Awaitable runner for the blocking call.
                Defaults to `asyncio.to_thread`.
        """
        self.store = store
        self.mail = mail
        self.client = client
        self.executor = executor or asyncio.to_thread

    async def check(self, contact):
        """Search one company for current openings and record them as leads.

        Summary:
            Run one careers-page check for one contact.

        Parameters:
            contact (Mapping): The contact whose company to check. Needs
                `id`, `company`, and optionally `careers_url`.

        Returns:
            dict: `found` (openings the model reported), `created` (new leads),
                `known` (openings already on the list), and `applied` (openings
                already applied to, which are not re-listed).

        Raises:
            ProviderRateLimited: Propagated so the page can say when to retry.
            ProviderBudgetExhausted: Propagated for the same reason.

        Note:
            Stamps the contact as checked even when nothing was found. The
            stamp records that the user has looked, which is true either way,
            and not stamping would leave the badge showing work already done.
        """
        if self.client is None:
            log.info("Careers check skipped for %s: no research provider",
                     contact["company"])
            return {"found": 0, "created": 0, "known": 0, "applied": 0}

        payload = dict(contact)
        openings, _input_tokens, _output_tokens = await self.executor(
            self.client.find_openings, payload
        )

        checked_at = int(time.time())
        created = known = applied = 0
        for opening in openings:
            key = identity_key(opening["title"], contact["company"],
                               opening.get("location"))

            # The same guard the alert handler applies: a role already applied
            # to must not come back as something to do.
            if self.store.job_by_identity(key) is not None:
                applied += 1
                continue

            is_new = self.mail.upsert_lead({
                "identity_key": key,
                "identity_scheme": identity_scheme(opening.get("location")),
                "title": opening["title"],
                "company": contact["company"],
                "location": opening.get("location"),
                "apply_url": opening["url"],
                "board": BOARD_CAREERS_CHECK,
                # The check is evidence the posting is live *now*, which is
                # exactly what `posted_ts` drives - ordering and expiry. A
                # careers page rarely states when a role went up, so the
                # check time stands in when nothing was reported, the same
                # way `upsert_lead` lets a repeat sighting reset the clock.
                "posted_ts": opening.get("posted_ts") or checked_at,
            })
            created += int(is_new)
            known += int(not is_new)

        self.mail.commit()
        self.mail.mark_contact_checked(contact["id"], checked_at)
        log.info(
            "Careers check of %s reported %d opening(s): %d new, %d known, "
            "%d already applied to",
            contact["company"], len(openings), created, known, applied,
        )
        return {"found": len(openings), "created": created, "known": known,
                "applied": applied}
