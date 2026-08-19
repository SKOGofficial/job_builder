"""Referrals: the people you know, and what their employers are advertising.

The morning sweep. One card per contact, each showing the open roles at their
company, newest posting first, with the ones that arrived since you last looked
marked. A referral is worth more than any amount of resume tailoring, and it
expires faster - which is why this page is built around acting immediately
rather than around browsing.

Two costs, kept visibly apart:

- **Matching is free** and happens on every render. It only sees roles that
  reached the mailbox as a board alert.
- **Check now** spends a grounded web search on one company. It is a button, per
  contact, never a timer, and what it finds becomes an ordinary lead so the rest
  of the pipeline picks it up.

Nothing here sends email. The app holds `gmail.readonly`; a draft gets a copy
button and a `mailto:` link, and the send is yours.
"""

import asyncio
import logging
import time
from urllib.parse import quote

from nicegui import ui

from clients.providers.base import ProviderBudgetExhausted, ProviderRateLimited
from pipeline.referral_email import draft_referral, supporting_bullets
from pipeline.referrals import OpeningsChecker, is_new_for, matches_for
from pipeline.relevance import RelevanceScorer
from utilities.durations import spell_duration
from utilities.mailstore import MailStore
from web.shell import card, page_shell
from web.state import get_state

log = logging.getLogger(__name__)

#: Longest a mailto: URL may run. Windows and several mail clients silently
#: truncate beyond roughly this, and a half-sent email is worse than one the
#: user pastes by hand - so past this the page says to use Copy instead.
MAILTO_LIMIT = 1800


def posted_age(lead):
    """How long ago the role was advertised.

    Summary:
        Describe a lead's posting age in words.

    Parameters:
        lead (Mapping): The lead to describe.

    Returns:
        str: A phrase such as "today" or "6 days ago", or an empty string when
            the lead carries no posting date.

    Note:
        Age rather than a date, for the same reason the to-apply list uses age:
        what matters is how much of the window is left, and a referral ask is
        worth sending on day one and barely worth sending on day ten.
    """
    posted = lead["posted_ts"]
    if not posted:
        return ""
    days = max(0, int((time.time() - posted) // 86400))
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def mailto_link(email, subject, body):
    """
    Summary:
        Build a `mailto:` URL that opens a pre-filled message.

    Parameters:
        email (str | None): The recipient.
        subject (str): The subject line.
        body (str): The message body.

    Returns:
        str: The URL, or an empty string when there is no address to send to
            or the result is too long for a mail client to handle.
    """
    if not email:
        return ""
    url = (f"mailto:{quote(email)}?subject={quote(subject)}"
           f"&body={quote(body)}")
    return url if len(url) <= MAILTO_LIMIT else ""


@ui.page("/referrals")
def referrals_page():
    state = get_state()
    store, mail = state.store, state.mail
    editing = {"id": None}

    with page_shell(
        "Referrals",
        "People you know, and what their companies are advertising. A role "
        "marked new arrived since you last looked. Searching a careers page "
        "costs one model call; everything else on this page is free.",
        active="/referrals",
    ):

        # --- contact editing ------------------------------------------------

        def save():
            if not (name.value or "").strip():
                ui.notify("A contact needs a name.", type="warning")
                return
            if not (company.value or "").strip():
                ui.notify("A contact needs a company - it is what the "
                          "postings are matched against.", type="warning")
                return
            try:
                mail.save_contact({
                    "id": editing["id"],
                    "name": name.value,
                    "email": email.value,
                    "company": company.value,
                    "role": role.value,
                    "careers_url": careers_url.value,
                    "notes": notes.value,
                })
            except ValueError as exc:
                ui.notify(str(exc), type="warning")
                return
            clear()
            contact_list.refresh()
            ui.notify("Contact saved.", type="positive")

        def clear():
            editing["id"] = None
            for field in (name, email, company, role, careers_url, notes):
                field.value = ""
            form_heading.refresh()

        def edit(contact):
            editing["id"] = contact["id"]
            name.value = contact["name"]
            email.value = contact["email"] or ""
            company.value = contact["company"]
            role.value = contact["role"] or ""
            careers_url.value = contact["careers_url"] or ""
            notes.value = contact["notes"] or ""
            form_heading.refresh()

        def archive(contact):
            mail.set_contact_archived(contact["id"], True)
            contact_list.refresh()
            ui.notify(f"{contact['name']} archived. Their drafts are kept.")

        def restore(contact):
            mail.set_contact_archived(contact["id"], False)
            contact_list.refresh()
            ui.notify(f"{contact['name']} restored.")

        def mark_checked(contact):
            mail.mark_contact_checked(contact["id"])
            contact_list.refresh()
            ui.notify(f"Marked {contact['company']} as checked.")

        # --- the expensive button -------------------------------------------

        async def check_now(contact):
            """Search one company's careers page, on demand.

            Summary:
                Run a careers-page check for one contact and report what it
                found.

            Parameters:
                contact (Mapping): The contact whose company to check.

            Note:
                The only place on this page that spends money, which is why it
                reports the count it found rather than refreshing silently -
                a paid call that looks like nothing happened invites a second
                press.
            """
            client = state.pool.for_task("check_openings")
            if client is None:
                ui.notify(
                    "No research provider is configured, so careers pages "
                    "cannot be checked. Add a Gemini or Anthropic key in "
                    "Settings.", type="warning",
                )
                return

            ui.notify(f"Checking {contact['company']}...")
            try:
                result = await OpeningsChecker(store, mail, client).check(
                    dict(contact)
                )
            except ProviderRateLimited as exc:
                ui.notify(
                    "Every provider is rate limited; try again in about "
                    f"{spell_duration(exc.retry_after)}.", type="warning",
                )
                return
            except ProviderBudgetExhausted as exc:
                ui.notify(str(exc), type="warning")
                return
            except Exception as exc:
                log.exception("Careers check failed for %s", contact["company"])
                ui.notify(f"Could not check {contact['company']}: {exc}",
                          type="negative")
                return

            contact_list.refresh()
            if not result["found"]:
                ui.notify(f"Nothing advertised at {contact['company']} right now.")
                return
            ui.notify(
                f"{contact['company']}: {result['found']} opening(s) found, "
                f"{result['created']} new.",
                type="positive",
            )

        # --- drafting -------------------------------------------------------

        async def draft(contact, lead):
            """Write the referral email for one contact and one role.

            Summary:
                Draft, store, and show a referral request.

            Parameters:
                contact (Mapping): Who is being asked.
                lead (Mapping): The opening being asked about.

            Note:
                Every database read happens here, on the thread that owns the
                connection; only the model call goes to a worker, inside
                `complete_json`.
            """
            client = state.pool.for_task("draft_referral")
            if client is None:
                ui.notify(
                    "No model provider is configured, so drafts cannot be "
                    "written. Add a key in Settings.", type="warning",
                )
                return

            research_row = mail.research_for(lead["identity_key"])
            research = {}
            if research_row is not None and research_row["payload"]:
                import json

                try:
                    research = json.loads(research_row["payload"])
                except ValueError:
                    research = {}

            profile_text = RelevanceScorer(store, mail).profile_text()
            bullets = supporting_bullets(mail.list_experiences(), lead, research)

            ui.notify("Writing the draft...")
            try:
                written = await asyncio.to_thread(
                    draft_referral, client, profile_text, contact, lead,
                    bullets, research,
                )
            except ProviderRateLimited as exc:
                ui.notify(
                    "Every provider is rate limited; try again in about "
                    f"{spell_duration(exc.retry_after)}.", type="warning",
                )
                return
            except Exception as exc:
                log.exception("Referral draft failed for contact %s",
                              contact["id"])
                ui.notify(f"Could not write the draft: {exc}", type="negative")
                return

            if not written["body"]:
                ui.notify("The model returned nothing usable. Try again.",
                          type="warning")
                return

            mail.record_outreach(contact["id"], lead["identity_key"],
                                 written["subject"], written["body"],
                                 getattr(client, "last_model", None)
                                 or getattr(client, "model", None))
            contact_list.refresh()
            show_draft(contact, lead, written["subject"], written["body"])

        def show_draft(contact, lead, subject, body):
            """The draft, editable, with copy and mailto beside it.

            Summary:
                Open the dialog holding one referral draft.

            Parameters:
                contact (Mapping): Who it is addressed to.
                lead (Mapping): The role it is about.
                subject (str): The drafted subject.
                body (str): The drafted body.

            Note:
                Editable on purpose. This email goes to a real person who knows
                the user, so it should be read and adjusted before it is sent -
                and the copy button takes whatever is in the box, not what the
                model wrote.
            """
            with ui.dialog() as dialog, ui.card().classes(
                "w-[42rem] max-w-full gap-3 p-6"
            ):
                ui.label(f"Referral request to {contact['name']}").classes(
                    "text-lg font-semibold"
                )
                ui.label(f"{lead['title']} at {contact['company']}").classes(
                    "text-sm opacity-70"
                )
                subject_field = ui.input("Subject", value=subject).props(
                    "dense outlined"
                ).classes("w-full")
                body_field = ui.textarea("Body", value=body).props(
                    "outlined autogrow"
                ).classes("w-full")
                url = lead["apply_url"] or lead["tracking_url"]
                if url:
                    ui.label(
                        "The posting link is not in the body - paste it in "
                        "where it belongs:"
                    ).classes("text-xs opacity-60")
                    ui.link(url, url, new_tab=True).classes("text-xs break-all")

                if not contact["email"]:
                    ui.label(
                        "No email address stored for this contact, so there is "
                        "nothing to open a mail client with. Copy works."
                    ).classes("text-xs opacity-70")

                async def copy():
                    text = (f"Subject: {subject_field.value}\n\n"
                            f"{body_field.value}")
                    await ui.clipboard.write(text)
                    ui.notify("Copied.", type="positive")

                def open_mail():
                    link = mailto_link(contact["email"], subject_field.value,
                                       body_field.value)
                    if not link:
                        ui.notify(
                            "This draft is too long for a mail-client link. "
                            "Use Copy instead.", type="warning",
                        )
                        return
                    ui.navigate.to(link, new_tab=True)

                with ui.row().classes("w-full items-center justify-end gap-2"):
                    ui.button("Close", on_click=dialog.close).props("flat no-caps")
                    ui.button("Copy", icon="content_copy", on_click=copy).props(
                        "flat no-caps"
                    )
                    if contact["email"]:
                        ui.button(
                            "Open in mail client", icon="mail",
                            on_click=open_mail,
                        ).props("unelevated no-caps")
            dialog.open()

        def reopen(contact, lead, outreach):
            show_draft(contact, lead, outreach["subject"] or "",
                       outreach["body"] or "")

        def set_sent(outreach, sent):
            mail.set_outreach_status(
                outreach["id"],
                MailStore.OUTREACH_SENT if sent else MailStore.OUTREACH_DRAFTED,
            )
            contact_list.refresh()
            ui.notify("Marked as sent." if sent else "Marked as not sent yet.")

        # --- rendering ------------------------------------------------------

        @ui.refreshable
        def form_heading():
            ui.label(
                "Edit contact" if editing["id"] else "Add a contact"
            ).classes("text-base font-semibold")

        with card():
            form_heading()
            with ui.row().classes("w-full gap-3"):
                name = ui.input("Name").props("dense outlined").classes("grow")
                email = ui.input("Email").props("dense outlined").classes("grow")
            with ui.row().classes("w-full gap-3"):
                company = ui.input(
                    "Company", placeholder="matched against incoming postings"
                ).props("dense outlined").classes("grow")
                role = ui.input(
                    "Their role", placeholder="Staff Engineer"
                ).props("dense outlined").classes("grow")
            careers_url = ui.input(
                "Careers page", placeholder="https://example.com/careers"
            ).props("dense outlined").classes("w-full")
            notes = ui.input(
                "How you know them",
                placeholder="worked together at Acme, 2023-2024",
            ).props("dense outlined").classes("w-full")
            ui.label(
                "The note is the only thing a draft may say about your "
                "relationship, so it is worth writing properly."
            ).classes("text-xs opacity-60")
            with ui.row().classes("self-end gap-2"):
                if_editing = ui.button("Cancel", on_click=clear).props(
                    "flat no-caps"
                )
                if_editing.bind_visibility_from(editing, "id")
                ui.button("Save contact", on_click=save).props("unelevated no-caps")

        @ui.refreshable
        def contact_list():
            entries = matches_for(mail)
            archived = [row for row in mail.list_contacts(include_archived=True)
                        if row["archived"]]

            with card("p-5"):
                with ui.row().classes("w-full items-center gap-2"):
                    total_new = sum(entry["new_count"] for entry in entries)
                    ui.label(
                        f"{len(entries)} contact(s), {total_new} new posting(s)"
                        if entries else "No contacts yet"
                    ).classes("text-sm")
                    ui.space()
                    ui.button(icon="refresh", on_click=contact_list.refresh).props(
                        "flat round dense"
                    ).tooltip("Refresh")

            if not entries:
                with card():
                    ui.label(
                        "Add someone you would be comfortable asking for a "
                        "referral. Their company is then matched against every "
                        "posting the mailbox brings in, and anything new is "
                        "waiting here in the morning."
                    ).classes("text-sm opacity-70")
            for entry in entries:
                contact_card(entry)

            if archived:
                with ui.expansion(f"Archived ({len(archived)})").classes(
                    "w-full"
                ).props("dense-toggle"):
                    for contact in archived:
                        with ui.row().classes("items-center gap-3 py-1"):
                            ui.label(
                                f"{contact['name']} · {contact['company']}"
                            ).classes("text-sm opacity-70")
                            ui.button(
                                "Restore", on_click=lambda c=contact: restore(c)
                            ).props("flat dense no-caps")

        def contact_card(entry):
            contact, leads = entry["contact"], entry["leads"]
            with card("p-5"):
                with ui.row().classes("w-full items-start justify-between gap-4"):
                    with ui.column().classes("gap-1 grow"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(contact["name"]).classes(
                                "text-base font-semibold"
                            )
                            if entry["new_count"]:
                                ui.html(
                                    '<span style="background-color:#22c55e;'
                                    'color:#fff;padding:2px 10px;'
                                    'border-radius:9999px;font-size:11px;'
                                    'font-weight:600">'
                                    f'{entry["new_count"]} new</span>'
                                )
                        where = " · ".join(
                            part for part in [contact["role"], contact["company"]]
                            if part
                        )
                        ui.label(where).classes("text-sm opacity-70")
                        if contact["notes"]:
                            ui.label(contact["notes"]).classes(
                                "text-xs opacity-60 italic"
                            )
                    with ui.row().classes("items-center gap-1"):
                        ui.button(
                            icon="edit", on_click=lambda c=contact: edit(c)
                        ).props("flat round dense").tooltip("Edit")
                        ui.button(
                            icon="archive", on_click=lambda c=contact: archive(c)
                        ).props("flat round dense").tooltip("Archive")

                with ui.row().classes("items-center gap-2 flex-wrap pt-1"):
                    ui.button(
                        "Check now", icon="travel_explore",
                        on_click=lambda c=contact: check_now(c),
                    ).props("flat dense no-caps").tooltip(
                        "Search this company's careers page. Costs one model call."
                    )
                    if contact["careers_url"]:
                        ui.button(
                            "Careers page", icon="open_in_new",
                            on_click=lambda u=contact["careers_url"]:
                                ui.navigate.to(u, new_tab=True),
                        ).props("flat dense no-caps")
                    if entry["new_count"]:
                        ui.button(
                            "Mark checked",
                            on_click=lambda c=contact: mark_checked(c),
                        ).props("flat dense no-caps")

                if not leads:
                    ui.label(
                        "Nothing open at this company on the to-apply list."
                    ).classes("text-xs opacity-60 pt-1")
                    return

                for lead in leads:
                    lead_row(entry, lead)

        def lead_row(entry, lead):
            contact = entry["contact"]
            outreach = entry["outreach"].get(lead["identity_key"])
            with ui.column().classes("w-full gap-1 border-t pt-3"):
                with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                    ui.label(lead["title"]).classes("text-sm font-medium")
                    if is_new_for(contact, lead):
                        ui.label("new").classes(
                            "text-xs px-2 py-0.5 rounded-full "
                            "bg-green-500/15 text-green-600 dark:text-green-400"
                        )
                    if lead["location"]:
                        ui.label(lead["location"]).classes("text-xs opacity-70")
                    age = posted_age(lead)
                    if age:
                        ui.label(f"posted {age}").classes("text-xs opacity-60")
                    if lead["board"]:
                        ui.label(f"via {lead['board']}").classes(
                            "text-xs opacity-60"
                        )

                with ui.row().classes("items-center gap-2 flex-wrap"):
                    url = lead["apply_url"] or lead["tracking_url"]
                    if url:
                        ui.button(
                            "Open posting", icon="open_in_new",
                            on_click=lambda u=url: ui.navigate.to(u, new_tab=True),
                        ).props("flat dense no-caps")

                    if outreach is None:
                        ui.button(
                            "Draft referral email", icon="edit_note",
                            on_click=lambda c=contact, l=lead: draft(c, l),
                        ).props("unelevated dense no-caps")
                    else:
                        ui.button(
                            "Open draft", icon="drafts",
                            on_click=lambda c=contact, l=lead, o=outreach:
                                reopen(c, l, o),
                        ).props("unelevated dense no-caps")
                        ui.button(
                            "Rewrite", icon="refresh",
                            on_click=lambda c=contact, l=lead: draft(c, l),
                        ).props("flat dense no-caps")
                        sent = outreach["status"] == MailStore.OUTREACH_SENT
                        ui.switch(
                            "Sent", value=sent,
                            on_change=lambda e, o=outreach: set_sent(o, e.value),
                        ).props("dense")
                        if sent and outreach["sent_at"]:
                            ui.label(f"on {outreach['sent_at'][:10]}").classes(
                                "text-xs opacity-60"
                            )

        contact_list()
