"""Deciding what a tailored application is made of.

Two steps, kept strictly apart:

1. **Selection** - rank stored experience bullets against the keywords the
   research pulled out of the posting. Pure, deterministic, and testable with
   no model involved.
2. **Rendering** - which does not happen here at all. See `pipeline/latex.py`,
   driven on demand by `pipeline/documents.py`.

The order matters and matches the existing backlog note: "generate resume
artifacts through a deterministic template before adding AI-assisted wording".
A template that renders wrongly is debuggable from the output. A model that
invents a job the applicant never had is not, and they may not notice until an
interview.

Nothing is written to disk. What is stored is the recipe - the ordered
experience ids a resume was chosen from, and the text of its covering letter -
so an edited bullet or an edited master shows up in the next download instead
of leaving a stale file behind to be found later.
"""

import asyncio
import hashlib
import html
import logging
import os
import re
from datetime import date

log = logging.getLogger(__name__)

ARTIFACT_RESUME = "resume"
#: Replaces the old `cv` kind. A curriculum vitae was the same bullets at a
#: higher limit, which is not a second document - what actually accompanies a
#: resume is a letter.
ARTIFACT_COVER_LETTER = "cover_letter"


def master_fingerprint():
    """
    Summary:
        Hash the master resume, so a later render can tell it has changed.

    Returns:
        str: A short hex digest, or "" when no master is configured or
            readable.

    Note:
        Never raises. A missing master is a real state - the app is useful
        before one is configured - and it must not stop a selection being
        recorded.
    """
    from pipeline import latex

    try:
        text = latex.load_master()
    except (FileNotFoundError, OSError):
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _mapping_record(mapping):
    """
    Summary:
        Reduce a requirement mapping to what is worth storing.

    Parameters:
        mapping (list[dict]): As `cover_letter.build_mapping` returns it.

    Returns:
        list[dict]: Each requirement with the ids and text of the bullets that
            answered it.

    Note:
        Ids alone would be smaller, but the text is what makes a stored letter
        auditable years later, when the bullet it was argued from may have been
        reworded or deleted.
    """
    return [
        {
            "requirement": pair["requirement"],
            "bullets": [{"id": row["id"], "bullet": row["bullet"]}
                        for row in pair["bullets"]],
        }
        for pair in mapping
    ]

#: Where generated files land. Keyed on identity_key rather than job_id,
#: because a lead has no job_id until it is promoted - and keying on the
#: identity means nothing has to move when it is.
DEFAULT_OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "generated"
)

#: One page of bullets. The CV limit that used to sit beside this is gone with
#: the CV itself - the same bullets at a higher limit was never a second
#: document, and what actually accompanies a resume is a letter.
RESUME_BULLET_LIMIT = 12

WORD = re.compile(r"[a-z0-9+#.]+")


def tokenise(text):
    return set(WORD.findall((text or "").lower()))


def score_bullet(bullet_row, keywords):
    """How well one experience bullet matches a posting's keywords.

    Tags are weighted above prose: a tag is something the user deliberately
    attached, while a word in the bullet text may be incidental. Recency breaks
    ties, so equally relevant experience appears newest-first.
    """
    if not keywords:
        return 0.0
    keyword_tokens = set()
    for keyword in keywords:
        keyword_tokens |= tokenise(keyword)
    if not keyword_tokens:
        return 0.0

    tag_tokens = tokenise(bullet_row["tags"])
    text_tokens = tokenise(bullet_row["bullet"]) | tokenise(bullet_row["role"])

    tag_hits = len(tag_tokens & keyword_tokens)
    text_hits = len(text_tokens & keyword_tokens)
    return (tag_hits * 2.0 + text_hits) / len(keyword_tokens)


def _recency(bullet_row):
    """Sortable end date; ongoing roles sort newest."""
    end = bullet_row["end_date"]
    if not end or str(end).lower() in ("present", "current", "ongoing"):
        return "9999-99"
    return str(end)


def select_bullets(experiences, keywords, limit=RESUME_BULLET_LIMIT):
    """Rank and trim experience bullets for one posting.

    Scored bullets first, most relevant first. When the posting yielded no
    keywords - research failed, or the posting was vague - this falls back to
    recency, which is a reasonable resume rather than an empty one.
    """
    scored = [(score_bullet(row, keywords), _recency(row), row)
              for row in experiences]
    relevant = [item for item in scored if item[0] > 0]

    if relevant:
        relevant.sort(key=lambda item: (-item[0], item[1]), reverse=False)
        chosen = [row for _, _, row in relevant]
    else:
        chosen = sorted(experiences, key=_recency, reverse=True)

    return chosen[:limit]


def group_by_role(bullets):
    """Group bullets under their organisation and role, preserving order.

    Summary:
        Collapse a flat bullet list into one entry per organisation and role.

    Parameters:
        bullets (list[Mapping]): Experience rows, already ordered.

    Returns:
        list[dict]: One entry per role with its bullets, in first-seen order.

    Note:
        `kind` rides along because the LaTeX emitter renders work and project
        entries with different commands - `\\resumeSubheading` against
        `\\resumeProjectHeading` - and grouping is the last point at which the
        distinction is still available.

        The dates are part of the key, not just the organisation and role. Two
        research assistantships at the same university carry the same title and
        would otherwise merge into one entry, printing the newer role's dates
        above the older role's bullets.
    """
    grouped = []
    index = {}
    for row in bullets:
        key = (row["organisation"] or "", row["role"] or "",
               row["start_date"] or "", row["end_date"] or "")
        if key not in index:
            index[key] = {
                "kind": row["kind"],
                "organisation": row["organisation"],
                "role": row["role"],
                "start_date": row["start_date"],
                "end_date": row["end_date"],
                "bullets": [],
            }
            grouped.append(index[key])
        index[key]["bullets"].append(row["bullet"])
    return grouped


# --- rendering ---------------------------------------------------------------


def render_markdown(profile, lead, bullets, research=None, heading="Resume"):
    """Plain Markdown. Always available, no external toolchain."""
    lines = [f"# {profile.get('name') or 'Curriculum Vitae'}"]

    contact = " · ".join(filter(None, [
        profile.get("email"), profile.get("phone"), profile.get("location"),
        profile.get("website"),
    ]))
    if contact:
        lines.append(contact)

    lines.append("")
    lines.append(f"**{heading} for {lead['title']}"
                 + (f" at {lead['company']}" if lead["company"] else "") + "**")

    advice = (research or {}).get("tailoring_advice")
    if advice:
        lines += ["", "## Summary", "", advice]

    lines += ["", "## Experience", ""]
    for group in group_by_role(bullets):
        header = " - ".join(filter(None, [group["role"], group["organisation"]]))
        dates = " to ".join(filter(None, [group["start_date"], group["end_date"]]))
        lines.append(f"### {header}" + (f"  \n*{dates}*" if dates else ""))
        lines.append("")
        for bullet in group["bullets"]:
            lines.append(f"- {bullet}")
        lines.append("")

    keywords = (research or {}).get("posting_keywords") or []
    if keywords:
        lines += ["## Relevant skills", "", ", ".join(keywords), ""]

    lines.append(f"<!-- Generated {date.today().isoformat()} -->")
    return "\n".join(lines)


def render_html(profile, lead, bullets, research=None, heading="Resume"):
    """Self-contained HTML, for previewing in the app or printing to PDF."""
    def esc(value):
        return html.escape(str(value or ""))

    parts = [
        "<!doctype html><meta charset='utf-8'>",
        f"<title>{esc(profile.get('name'))} - {esc(lead['title'])}</title>",
        "<style>"
        "body{font-family:Georgia,serif;max-width:46rem;margin:2rem auto;"
        "padding:0 1rem;line-height:1.5;color:#1a1a1a}"
        "h1{margin-bottom:.2rem}h2{border-bottom:1px solid #ccc;padding-bottom:.2rem;"
        "margin-top:1.6rem}h3{margin-bottom:.1rem}.dates{color:#666;font-style:italic;"
        "font-size:.9em}.contact{color:#444}ul{margin-top:.4rem}"
        "@media print{body{margin:0;max-width:none}}"
        "</style>",
        f"<h1>{esc(profile.get('name') or 'Curriculum Vitae')}</h1>",
    ]

    contact = " &middot; ".join(
        esc(value) for value in
        (profile.get("email"), profile.get("phone"), profile.get("location"),
         profile.get("website")) if value
    )
    if contact:
        parts.append(f"<p class='contact'>{contact}</p>")

    target = esc(lead["title"]) + (f" at {esc(lead['company'])}"
                                   if lead["company"] else "")
    parts.append(f"<p><strong>{esc(heading)} for {target}</strong></p>")

    advice = (research or {}).get("tailoring_advice")
    if advice:
        parts.append(f"<h2>Summary</h2><p>{esc(advice)}</p>")

    parts.append("<h2>Experience</h2>")
    for group in group_by_role(bullets):
        header = " &ndash; ".join(
            esc(value) for value in (group["role"], group["organisation"]) if value)
        parts.append(f"<h3>{header}</h3>")
        dates = " to ".join(filter(None, [group["start_date"], group["end_date"]]))
        if dates:
            parts.append(f"<div class='dates'>{esc(dates)}</div>")
        parts.append("<ul>")
        parts.extend(f"<li>{esc(bullet)}</li>" for bullet in group["bullets"])
        parts.append("</ul>")

    keywords = (research or {}).get("posting_keywords") or []
    if keywords:
        parts.append("<h2>Relevant skills</h2><p>"
                     + ", ".join(esc(k) for k in keywords) + "</p>")

    return "\n".join(parts)


# The Typst renderer that used to live here is gone. It was never once
# exercised - Typst is not installed, so `typst_available()` returned False on
# every run and the PDF branch was dead the whole time, which is exactly what
# made "the resumes are built" look true when no resume had ever been rendered.
# PDFs now come from `pipeline/latex.py`, against the user's real LaTeX master.


# --- orchestration -----------------------------------------------------------


class ArtifactBuilder:
    """Decide what an application is made of, and record it.

    Writes no documents. Rendering happens on demand when the user asks for a
    download, so what is stored is the recipe: the experience ids a resume was
    built from, and the text of the letter.

    This runs on the event loop the web UI uses, so the two slow parts - the
    research call and the letter - go to an executor. Database access stays on
    the calling thread, which owns the sqlite connection, so each method reads
    what it needs first, offloads, then writes the result back.
    """

    def __init__(self, store, mail, research_client=None,
                 output_dir=DEFAULT_OUTPUT_DIR, executor=None,
                 letter_client=None):
        self.store = store
        self.mail = mail
        self.research_client = research_client
        #: Separate from `research_client`: research reaches the web and costs
        #: real money, while the letter is a plain completion. They route to
        #: different providers.
        self.letter_client = letter_client
        self.output_dir = output_dir
        self.executor = executor or asyncio.to_thread

    def profile(self):
        get = self.store.get_profile_value
        return {
            "name": get("name", ""),
            "email": get("email", ""),
            "phone": get("phone", ""),
            "location": get("location", ""),
            "website": get("website", ""),
        }

    @property
    def profile_text(self):
        """How the user describes themselves, for the letter's opening.

        Summary:
            The stored positioning text a covering letter is written from.

        Returns:
            str: The profile and target-roles text joined, empty when neither
                is set.

        Note:
            The same two values `RelevanceScorer.profile_text` scores against,
            deliberately - the letter should argue from the positioning that
            decided the lead was worth pursuing in the first place.
        """
        parts = [
            self.store.get_profile_value("profile_text", ""),
            self.store.get_profile_value("target_roles", ""),
        ]
        return "\n\n".join(part for part in parts if part).strip()

    async def research_for(self, lead):
        """Cached research, or a fresh call. Returns a payload dict.

        Summary:
            Return the stored research payload for a lead, calling the research
            client and caching the result when there is none.

        Parameters:
            lead (Mapping): The lead to research.

        Returns:
            dict: The research payload, empty when no client is configured.

        Raises:
            SpendCeilingReached: From the research client when the budget is
                spent.
            ResearchNotConfigured: From the research client when it cannot run.

        Note:
            The cache lookup and the save both touch the database and so stay
            on the calling thread; only the client call is offloaded.
        """
        import json

        existing = self.mail.research_for(lead["identity_key"])
        if existing is not None and existing["payload"]:
            try:
                return json.loads(existing["payload"])
            except (TypeError, ValueError):
                pass

        if self.research_client is None:
            return {}

        payload, input_tokens, output_tokens = await self.executor(
            self.research_client.research, lead)
        self.mail.save_research(
            lead["identity_key"],
            payload.get("company_summary", ""),
            payload,
            model=getattr(self.research_client, "model", None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return payload

    async def build(self, lead):
        """Decide what this application is made of, and record it.

        Writes no documents. A resume is stored as the ordered experience ids
        chosen for it, so an edited bullet or an edited master shows up in the
        next download rather than leaving a stale file behind. The letter is
        stored as text because prose cannot be recomputed for free.

        Summary:
            Research a lead, select its resume bullets, write its covering
            letter, and record both.

        Parameters:
            lead (Mapping): The lead to prepare.

        Returns:
            dict: `{"bullet_ids": [...], "letter": {...}}` describing what was
                recorded.

        Raises:
            ValueError: When there are no stored experiences to build from.
            SpendCeilingReached: From the research client when the budget is
                spent.
            ResearchNotConfigured: From the research client when it cannot run.

        Note:
            The only slow call left here is the letter, and it goes to the
            executor for the same reason everything else does - this runs on
            the loop that serves the UI.
        """
        research = await self.research_for(lead)
        # Plain dicts across the thread boundary, as elsewhere: a sqlite Row
        # would tempt the worker into touching a connection it does not own.
        experiences = [dict(row) for row in self.mail.list_experiences()]

        if not experiences:
            raise ValueError(
                "No stored experiences to build from. Add experience bullets "
                "on the Resume page first."
            )

        keywords = research.get("posting_keywords") or []
        chosen = select_bullets(experiences, keywords, RESUME_BULLET_LIMIT)
        bullet_ids = [row["id"] for row in chosen]

        self.mail.save_selection(
            lead["identity_key"], ARTIFACT_RESUME,
            bullet_ids=bullet_ids, keywords=keywords,
            master_fingerprint=master_fingerprint(),
        )

        letter, mapping = await self._write_letter(lead, research, experiences)
        if letter:
            self.mail.save_selection(
                lead["identity_key"], ARTIFACT_COVER_LETTER,
                letter=letter, mapping=_mapping_record(mapping),
                keywords=keywords,
                model=getattr(self.letter_client, "model", None),
            )

        return {"bullet_ids": bullet_ids, "letter": letter}

    async def _write_letter(self, lead, research, experiences):
        """
        Summary:
            Build the requirement-to-experience table and write the letter.

        Parameters:
            lead (Mapping): The role being applied to.
            research (Mapping): The research payload.
            experiences (list[dict]): Every stored bullet, as candidates.

        Returns:
            tuple[dict, list]: The four-part letter and the mapping it argued
                from. Both empty when no letter client is configured or the
                posting yielded nothing to answer.

        Note:
            A posting with no extracted requirements produces no letter rather
            than a generic one. A letter that could have been sent to anybody
            is worse than no letter, because it takes a slot the reader was
            willing to give to something specific.
        """
        from pipeline.cover_letter import build_mapping, write_letter

        if self.letter_client is None:
            return {}, []

        requirements = (research.get("requirements") or []) + \
            (research.get("responsibilities") or [])
        mapping = build_mapping(requirements, experiences)
        if not mapping:
            log.info("No requirement matched a stored experience for %s at %s; "
                     "skipping the letter", lead["title"], lead["company"])
            return {}, []

        letter = await self.executor(
            write_letter, self.letter_client, self.profile_text,
            dict(lead), research, mapping,
        )
        return letter, mapping


