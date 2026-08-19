"""Drafting the ask.

Modelled on `pipeline/cover_letter.py`, and for the same reason: **the model
does not pick its own evidence.** `score_bullet` ranks the stored experience
bullets against the posting, the top few go into the prompt, and the model
writes prose around what it was given. A model left to choose its own supporting
detail writes a fluent email crediting the applicant with a project they never
worked on - and here that lands in front of someone who actually knows them.

The other difference from a covering letter is who is reading. A hiring manager
reads a letter as an application; a colleague reads this as a favour being
asked. So it is short, it names the specific posting, it states the ask plainly,
and it never explains to the reader what their own employer does.

Nothing here sends anything. The app holds `gmail.readonly` and this stage
produces text; the send is the user's, from their own mail client.
"""

import json
import logging

from pipeline.generate import score_bullet
from pipeline.relevance import MAX_PROFILE_CHARS

log = logging.getLogger(__name__)

#: Bullets carried into the prompt. Two or three is what a short email can
#: actually use - more invites the model to list them, which turns a favour into
#: a resume the reader did not ask for.
SUPPORTING_BULLETS = 3

#: Words. A referral ask that runs long is asking for the reader's time before
#: it has asked for anything else.
MAX_WORDS = 150

EMPTY_DRAFT = {"subject": "", "body": ""}

SYSTEM_PROMPT = """You write a short email asking someone the applicant already \
knows to refer them for a specific job at the company where that person works.

You are given who the contact is, how the applicant knows them, the opening, and \
a few of the applicant's own experience bullets. Write the email.

Rules:
- Under 150 words. This is a favour being asked, not an application.
- Name the specific role and say where it was seen. A vague "any openings?" \
puts the work of finding one on the reader.
- Use only the experience you are given. Never introduce a project, employer, \
technology, or number that is not in the material above. If little was given, \
write a shorter email.
- Refer to the relationship only as it was described to you. Never invent a \
shared project, a mutual friend, or a past conversation - the reader knows what \
is true.
- Do not explain the company to someone who works there.
- Make the ask explicit and easy to decline. A referral they feel cornered into \
is worth less than one they offer.
- Plain language. No "I hope this finds you well", no "I am passionate about", \
no flattery.
- British or American spelling, following the applicant's own text.

Reply with JSON only, in this exact shape:
{
  "subject": "<a specific subject line naming the role, under 60 characters>",
  "body": "<the email body, greeting through sign-off, plain text with blank lines between paragraphs>"
}"""


def supporting_bullets(experiences, lead, research=None,
                       limit=SUPPORTING_BULLETS):
    """Pick which of the applicant's own bullets back the ask.

    Summary:
        Rank stored experience bullets against the opening and return the best.

    Parameters:
        experiences (list[Mapping]): Candidate experience rows.
        lead (Mapping): The opening, read for its title.
        research (Mapping | None): The stored research payload, whose
            `posting_keywords` sharpen the ranking when it exists.
        limit (int): Most bullets to return.

    Returns:
        list[Mapping]: The highest-scoring bullets, best first. Empty when
            nothing scored above zero, which the prompt handles by asking for a
            shorter email rather than inventing material.

    Note:
        Scored with `score_bullet`, the same function that orders a resume and
        a covering letter, so all three argue from the same evidence.
    """
    keywords = [lead["title"]] if lead["title"] else []
    if research:
        keywords += list(research.get("posting_keywords") or [])[:8]
    scored = [(score_bullet(row, keywords), row) for row in experiences]
    return [row for score, row in sorted(scored, key=lambda pair: -pair[0])
            if score > 0][:limit]


def build_prompt(profile_text, contact, lead, bullets, research=None):
    """
    Summary:
        Assemble the user half of the referral-drafting request.

    Parameters:
        profile_text (str): The applicant's own positioning.
        contact (Mapping): Who is being asked - name, role, and the notes the
            user wrote about how they know them.
        lead (Mapping): The opening being asked about.
        bullets (list[Mapping]): The supporting experience, from
            `supporting_bullets`.
        research (Mapping | None): Stored research, used only for the one line
            about why this company.

    Returns:
        str: The prompt body.
    """
    parts = [
        "Contact: %s" % (contact["name"],),
        "Where they work: %s" % (contact["company"],),
    ]
    if contact["role"]:
        parts.append("Their role: %s" % (contact["role"],))
    if contact["notes"]:
        parts.append("How the applicant knows them: %s" % (contact["notes"],))
    else:
        # Said explicitly rather than omitted. An absent field invites the
        # model to fill the gap with a plausible shared history.
        parts.append(
            "How the applicant knows them: not recorded. Do not describe the "
            "relationship at all."
        )

    parts.append("\nThe opening:")
    parts.append("Role: %s" % (lead["title"],))
    if lead["location"]:
        parts.append("Location: %s" % (lead["location"],))
    url = lead["apply_url"] or lead["tracking_url"]
    if url:
        parts.append("Posting: %s" % (url,))
    if lead["board"]:
        parts.append("Where the applicant saw it: %s" % (lead["board"],))

    if profile_text:
        parts.append("\nHow the applicant describes themselves:\n%s"
                     % (profile_text.strip()[:MAX_PROFILE_CHARS],))

    if research and research.get("tailoring_advice"):
        parts.append("\nWhat an application here should emphasise: %s"
                     % (research["tailoring_advice"],))

    if bullets:
        parts.append("\nThe applicant's relevant experience:")
        for row in bullets:
            where = row["organisation"] or ""
            parts.append("  - [%s] %s" % (where, row["bullet"]))
    else:
        parts.append(
            "\nNo specific experience was matched to this role. Keep the email "
            "short and do not claim any."
        )

    return "\n".join(parts)


def parse_draft(content):
    """
    Summary:
        Validate the model's reply into a subject and a body.

    Parameters:
        content (str): Raw JSON text from the model.

    Returns:
        dict: `subject` and `body`. Both come back empty rather than missing
            when the model returned nothing usable, so the page can say "that
            produced nothing" instead of rendering half an email.

    Note:
        Never raises, matching `parse_letter`. The word cap is enforced here as
        well as asked for in the prompt: a model that ignores the instruction
        should not be able to put a thousand-word essay in front of someone
        doing the applicant a favour.
    """
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        log.warning("Referral draft reply was not JSON")
        return dict(EMPTY_DRAFT)
    if not isinstance(data, dict):
        return dict(EMPTY_DRAFT)

    def _text(value):
        return value.strip() if isinstance(value, str) else ""

    body = _text(data.get("body"))
    words = body.split()
    if len(words) > MAX_WORDS * 2:
        log.info("Referral draft ran to %d words; trimming", len(words))
        body = " ".join(words[:MAX_WORDS * 2]) + "..."

    return {"subject": _text(data.get("subject"))[:120], "body": body}


def draft_referral(client, profile_text, contact, lead, bullets,
                   research=None):
    """
    Summary:
        Draft one referral request email.

    Parameters:
        client: Anything with `complete_json`, so a task client from the pool
            or a test double both work.
        profile_text (str): The applicant's positioning.
        contact (Mapping): Who is being asked.
        lead (Mapping): The opening.
        bullets (list[Mapping]): Supporting experience.
        research (Mapping | None): Stored research for this role.

    Returns:
        dict: `subject` and `body`, empty when the model produced nothing
            usable.

    Raises:
        ProviderRateLimited: Propagated from the client so the caller can stop
            cleanly, as every other model stage does.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(profile_text, contact, lead,
                                                 bullets, research)},
    ]
    return client.complete_json(messages, parse_draft, dict(EMPTY_DRAFT), 700)
