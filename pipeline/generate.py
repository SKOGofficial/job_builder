"""Turning experiences plus research into a tailored resume and CV.

Two steps, kept strictly apart:

1. **Selection** - rank stored experience bullets against the keywords the
   research pulled out of the posting. Pure, deterministic, and testable with
   no model involved.
2. **Rendering** - fill a template with the selected bullets.

The order matters and matches the existing backlog note: "generate resume
artifacts through a deterministic template before adding AI-assisted wording".
A template that renders wrongly is debuggable from the output. A model that
invents a job the applicant never had is not, and they may not notice until an
interview.

Output formats degrade rather than fail. Markdown and HTML need nothing beyond
the standard library, so a lead always ends up with something usable; PDF is
attempted only when a Typst or LaTeX binary is actually present. A homelab that
never installed a TeX distribution still gets a working to-apply list.
"""

import html
import logging
import os
import re
import shutil
import subprocess
from datetime import date

log = logging.getLogger(__name__)

ARTIFACT_RESUME = "resume"
ARTIFACT_CV = "cv"

#: Where generated files land. Keyed on identity_key rather than job_id,
#: because a lead has no job_id until it is promoted - and keying on the
#: identity means nothing has to move when it is.
DEFAULT_OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "generated"
)

#: A resume is a summary; a CV is the full record.
RESUME_BULLET_LIMIT = 12
CV_BULLET_LIMIT = 200

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
    """Group bullets under their organisation and role, preserving order."""
    grouped = []
    index = {}
    for row in bullets:
        key = (row["organisation"] or "", row["role"] or "")
        if key not in index:
            index[key] = {
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


def typst_available():
    return shutil.which("typst") is not None


def render_pdf(markdown_path, pdf_path):
    """Best-effort PDF via Typst.

    Typst rather than LaTeX: a single static binary against a multi-gigabyte
    TeX Live install, on a box that has to stay up unattended. Absence is not
    an error - Markdown and HTML are already written by the time this runs.
    """
    if not typst_available():
        return None
    try:
        subprocess.run(
            ["typst", "compile", "--format", "pdf", markdown_path, pdf_path],
            check=True, capture_output=True, timeout=60,
        )
        return pdf_path
    except (subprocess.SubprocessError, OSError) as exc:
        log.info("Typst could not render %s (%s); Markdown and HTML are still "
                 "available", markdown_path, exc)
        return None


# --- orchestration -----------------------------------------------------------


class ArtifactBuilder:
    """Research a lead, build its resume and CV, mark it ready."""

    def __init__(self, store, mail, research_client=None,
                 output_dir=DEFAULT_OUTPUT_DIR):
        self.store = store
        self.mail = mail
        self.research_client = research_client
        self.output_dir = output_dir

    def profile(self):
        get = self.store.get_profile_value
        return {
            "name": get("name", ""),
            "email": get("email", ""),
            "phone": get("phone", ""),
            "location": get("location", ""),
            "website": get("website", ""),
        }

    def research_for(self, lead):
        """Cached research, or a fresh call. Returns a payload dict."""
        import json

        existing = self.mail.research_for(lead["identity_key"])
        if existing is not None and existing["payload"]:
            try:
                return json.loads(existing["payload"])
            except (TypeError, ValueError):
                pass

        if self.research_client is None:
            return {}

        payload, input_tokens, output_tokens = self.research_client.research(lead)
        self.mail.save_research(
            lead["identity_key"],
            payload.get("company_summary", ""),
            payload,
            model=getattr(self.research_client, "model", None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return payload

    def build(self, lead):
        """Produce artifacts for one lead. Returns the paths written."""
        research = self.research_for(lead)
        keywords = research.get("posting_keywords") or []
        experiences = self.mail.list_experiences()

        if not experiences:
            raise ValueError(
                "No stored experiences to build from. Add experience bullets "
                "on the Resume page first."
            )

        directory = os.path.join(self.output_dir, lead["identity_key"])
        os.makedirs(directory, exist_ok=True)
        profile = self.profile()
        written = {}

        for kind, limit, heading in (
            (ARTIFACT_RESUME, RESUME_BULLET_LIMIT, "Resume"),
            (ARTIFACT_CV, CV_BULLET_LIMIT, "Curriculum vitae"),
        ):
            bullets = select_bullets(experiences, keywords, limit)
            markdown = render_markdown(profile, lead, bullets, research, heading)
            page = render_html(profile, lead, bullets, research, heading)

            markdown_path = os.path.join(directory, f"{kind}.md")
            html_path = os.path.join(directory, f"{kind}.html")
            _write(markdown_path, markdown)
            _write(html_path, page)

            pdf_path = render_pdf(markdown_path,
                                  os.path.join(directory, f"{kind}.pdf"))

            best = pdf_path or html_path
            self.mail.save_artifact(lead["identity_key"], kind, best,
                                    getattr(self.research_client, "model", None))
            written[kind] = best

        return written


def _write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
