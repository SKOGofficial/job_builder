"""Writing the covering letter.

The method is the one in the guide this was built from: read the posting, list
what it asks for, put each requirement beside the experience that answers it,
and write from that table rather than from a blank page.

The important part is who does which half. **The pairing is computed here, not
by the model.** `score_bullet` already ranks stored experience bullets against
a posting's language, so the requirement-to-qualification table is arithmetic
over rows that exist. The model receives the finished pairs and writes prose
around them.

That division is deliberate. A model asked to pick its own supporting evidence
will produce a fluent letter containing a project the applicant never worked
on, and the applicant finds out in the interview. `research_client` already
states the rule for facts about the company - an invented one is worse than
useless - and the same rule holds for facts about the applicant.
"""

import json
import logging

from pipeline.generate import score_bullet

log = logging.getLogger(__name__)

#: How many stored bullets may back a single requirement. Two is enough to
#: write a sentence from; more crowds the prompt and invites the model to list
#: rather than argue.
BULLETS_PER_REQUIREMENT = 2

#: Requirements carried into the letter. A posting listing twenty is listing
#: its wish list, and a letter answering all twenty is a checklist, not a
#: letter.
MAX_REQUIREMENTS = 6

SYSTEM_PROMPT = """You write covering letters for a software engineering \
applicant. You will be given the applicant's positioning, the role, what the \
company does, and a table pairing each requirement in the posting with the \
applicant's own experience.

Rules:
- Use only the experience given to you. Never introduce a project, employer, \
technology, or number that does not appear in the table. If the table is thin, \
write a shorter letter.
- Answer requirements in the posting's own wording where it is natural to.
- Every claim about the applicant must trace to a bullet in the table. Every \
claim about the company must trace to the research given.
- Write plainly. No "I am thrilled", no "passionate", no restating your own \
sentences.
- British or American spelling, follow the applicant's own text.

Reply with JSON only, in this exact shape:
{
  "opening": "<who the applicant is, what they are applying for, what they care about - 2-4 sentences>",
  "match": ["<a paragraph arguing the applicant meets the role's needs, from the table>"],
  "why_here": "<why this company specifically, from the research - 2-4 sentences>",
  "closing": "<a short, direct closing paragraph - 2-3 sentences>"
}

`match` holds one or two paragraphs. With the opening, why_here and closing \
that makes a four or five paragraph letter. Never more than two."""


def build_mapping(requirements, bullets, limit=BULLETS_PER_REQUIREMENT):
    """
    Summary:
        Pair each posting requirement with the stored bullets that answer it.

    Parameters:
        requirements (list[str]): The posting's requirements, in its wording.
        bullets (list[Mapping]): Candidate experience rows.
        limit (int): Most bullets to attach to one requirement.

    Returns:
        list[dict]: `{"requirement": str, "bullets": [row, ...]}`, keeping only
            requirements something actually answers. An unanswered requirement
            is dropped rather than carried empty - a letter should not draw
            attention to a gap.

    Note:
        Scored with `score_bullet`, the same function that orders a resume, so
        the letter and the resume argue from the same evidence.
    """
    mapping = []
    for requirement in (requirements or [])[:MAX_REQUIREMENTS]:
        if not requirement:
            continue
        scored = [(score_bullet(row, [requirement]), row) for row in bullets]
        matched = [row for score, row in
                   sorted(scored, key=lambda pair: -pair[0]) if score > 0]
        if matched:
            mapping.append({"requirement": requirement,
                            "bullets": matched[:limit]})
    return mapping


def build_prompt(profile_text, lead, research, mapping):
    """
    Summary:
        Assemble the user half of the letter-writing request.

    Parameters:
        profile_text (str): The applicant's own positioning, from
            `RelevanceScorer.profile_text`.
        lead (Mapping): The role being applied to.
        research (Mapping): The research payload for the company.
        mapping (list[dict]): As returned by `build_mapping`.

    Returns:
        str: The prompt body.
    """
    parts = [f"Role: {lead['title']}", f"Company: {lead['company']}"]
    if lead.get("location"):
        parts.append(f"Location: {lead['location']}")

    if profile_text:
        parts.append(f"\nHow the applicant describes themselves:\n{profile_text}")

    for label, key in (("Mission", "mission"),
                       ("What the company does", "company_summary"),
                       ("What an application should emphasise", "tailoring_advice")):
        if research.get(key):
            parts.append(f"\n{label}: {research[key]}")

    for label, key in (("Products", "products"),
                       ("Recent news", "recent_news"),
                       ("How they work", "culture_notes"),
                       ("Nice to have", "nice_to_haves")):
        if research.get(key):
            parts.append(f"\n{label}:\n" +
                         "\n".join(f"- {item}" for item in research[key]))

    parts.append("\nRequirement and matching experience:")
    for pair in mapping:
        parts.append(f"\nRequirement: {pair['requirement']}")
        for row in pair["bullets"]:
            where = row["organisation"] or ""
            parts.append(f"  - [{where}] {row['bullet']}")

    return "\n".join(parts)


def parse_letter(content):
    """
    Summary:
        Validate the model's reply into a four-part letter.

    Parameters:
        content (str): Raw JSON text from the model.

    Returns:
        dict: Under `opening`, `match` (list), `why_here`, `closing`. Fields
            the model omitted come back empty rather than missing.

    Note:
        `match` is capped at two paragraphs here as well as asked for in the
        prompt. A model that ignores the instruction should not be able to turn
        a five paragraph letter into a nine paragraph one.
    """
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        log.warning("Cover letter reply was not JSON")
        return {"opening": "", "match": [], "why_here": "", "closing": ""}
    if not isinstance(data, dict):
        return {"opening": "", "match": [], "why_here": "", "closing": ""}

    def _text(value):
        return value.strip() if isinstance(value, str) else ""

    match = data.get("match")
    if isinstance(match, str):
        match = [match]
    if not isinstance(match, list):
        match = []
    match = [_text(item) for item in match if _text(item)][:2]

    return {
        "opening": _text(data.get("opening")),
        "match": match,
        "why_here": _text(data.get("why_here")),
        "closing": _text(data.get("closing")),
    }


def write_letter(client, profile_text, lead, research, mapping):
    """
    Summary:
        Write one covering letter.

    Parameters:
        client: Anything with `complete_json`, so a task client from the
            provider pool or a test double both work.
        profile_text (str): The applicant's positioning.
        lead (Mapping): The role.
        research (Mapping): The company research payload.
        mapping (list[dict]): As returned by `build_mapping`.

    Returns:
        dict: The four-part letter. Empty parts when the model returned
            nothing usable.

    Raises:
        ProviderRateLimited: Propagated from the client so the caller can stop
            cleanly, as every other model stage does.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(profile_text, lead, research,
                                                 mapping)},
    ]
    return client.complete_json(
        messages, parse_letter,
        {"opening": "", "match": [], "why_here": "", "closing": ""},
        1200,
    )
