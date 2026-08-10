"""Emitting LaTeX, and compiling it if the machine can.

The master resume is a real LaTeX document that lives outside this repo, in the
user's vault, and is authored by hand. This module treats it as a template: the
preamble, contact block, Education and Skills sections are reproduced verbatim,
and only the Experience and Projects sections are regenerated from the
`experiences` table. That is what makes a stored application a list of row ids
rather than a copy of a document.

Compilation is best-effort and engine-agnostic. Nothing here raises because a
LaTeX distribution is missing - the `.tex` is the real output and is useful on
its own, since it can be pasted into Overleaf.
"""

import logging
import os
import re
import shutil
import subprocess
from datetime import date

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised only without the dep
    load_dotenv = None

log = logging.getLogger(__name__)

#: Where the section bodies we regenerate begin and end. These markers are the
#: template's own, already present in the master, and stable because they are
#: how a human reading the file finds their way around it.
EXPERIENCE_MARKER = "%-----------EXPERIENCE-----------"
PROJECTS_MARKER = "%-----------PROJECTS-----------"
SKILLS_MARKER = "%-----------PROGRAMMING SKILLS-----------"

#: Tried in order. Tectonic first because it is a single binary that fetches
#: what it needs; the TeX Live engines only work on a machine that already has
#: a distribution installed.
ENGINES = ("tectonic", "latexmk", "xelatex", "pdflatex")

#: A resume is a page. Sixty seconds is generous even for a cold Tectonic run
#: that has to download packages.
COMPILE_TIMEOUT = 60

#: Applied in a single pass, never as a sequence of `str.replace` calls. Some
#: replacements contain characters that are themselves special - the one for a
#: backslash contains braces - so a second pass would escape the escapes and
#: turn `a\b` into `a\textbackslash\{\}b`.
_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

_SPECIAL = re.compile("|".join(re.escape(key) for key in _ESCAPES))

#: A balanced pair of straight double quotes, non-greedy so adjacent quoted
#: phrases stay separate.
_QUOTED = re.compile(r'"([^"]*)"')


def _load_env():
    if load_dotenv:
        env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
        )
        load_dotenv(dotenv_path=env_path)


def escape(text):
    r"""Make text safe to drop into a LaTeX document.

    The one that matters is `%`. LaTeX treats it as a comment, so an unescaped
    percent silently swallows the rest of the line and compiles without error -
    which is how the real resume came to claim a solar panel "converted 60"
    with the units and the rest of the sentence missing. Bullet text comes out
    of the database, where percentages and ampersands are routine, so nothing
    reaches a document without passing through here.

    Summary:
        Escape every LaTeX special character in a string, and convert straight
        double quotes to LaTeX's directional pair.

    Parameters:
        text (str | None): Arbitrary text. None and non-strings become "".

    Returns:
        str: The same text, safe to interpolate into a document.

    Note:
        The quote conversion is here rather than in a second pass because this
        is the single gate every piece of stored text passes through, and a
        separate step is a step somebody forgets. A straight `"` in LaTeX
        renders as a closing quote at both ends, so a paper title comes out as
        ”like this” - correct characters, visibly wrong direction.

        Quotes are converted first. Backtick and apostrophe are not special in
        LaTeX and so pass through the escaping pass untouched.
    """
    if not text:
        return ""
    # Balanced pairs only. An unpaired quote is left alone rather than guessed
    # at, since it is more often an inch mark than an opening quote.
    out = _QUOTED.sub(r"``\1''", str(text))
    return _SPECIAL.sub(lambda match: _ESCAPES[match.group()], out)


def master_path():
    """
    Summary:
        Locate the master resume LaTeX file.

    Returns:
        str: The configured path, empty when `JOB_BUILDER_RESUME_MASTER` is
            unset.

    Note:
        Deliberately returns a path rather than reading it, so a caller can
        report a missing master by name instead of failing anonymously.
    """
    _load_env()
    return (os.environ.get("JOB_BUILDER_RESUME_MASTER", "") or "").strip()


def load_master(path=None):
    """
    Summary:
        Read the master resume.

    Parameters:
        path (str | None): Override; defaults to `master_path()`.

    Returns:
        str: The file's text.

    Raises:
        FileNotFoundError: When no master is configured, or the configured path
            does not exist. The message names the setting, because the usual
            cause is an unset or stale `JOB_BUILDER_RESUME_MASTER`.
    """
    resolved = path or master_path()
    if not resolved:
        raise FileNotFoundError(
            "No master resume configured. Set JOB_BUILDER_RESUME_MASTER to the "
            "main.tex of your resume."
        )
    if not os.path.exists(resolved):
        raise FileNotFoundError(
            f"Master resume not found at {resolved}. Check "
            "JOB_BUILDER_RESUME_MASTER."
        )
    with open(resolved, encoding="utf-8") as handle:
        return handle.read()


def split_master(text):
    """
    Summary:
        Split the master into the parts kept verbatim and the parts replaced.

    Parameters:
        text (str): The master document.

    Returns:
        tuple[str, str]: Everything before the Experience section, and
            everything from the Skills section onward.

    Raises:
        ValueError: When a marker is missing or they appear out of order, which
            means the master was restructured and the emitter would otherwise
            splice content into the wrong place.

    Note:
        Education sits above Experience and Skills below Projects, so keeping
        the head and the tail preserves both without naming them.
    """
    head_at = text.find(EXPERIENCE_MARKER)
    tail_at = text.find(SKILLS_MARKER)
    if head_at == -1 or tail_at == -1:
        missing = EXPERIENCE_MARKER if head_at == -1 else SKILLS_MARKER
        raise ValueError(
            f"Master resume is missing the {missing!r} marker; cannot tell "
            "which sections to regenerate."
        )
    if tail_at < head_at:
        raise ValueError(
            "Master resume has the Skills section above Experience; the "
            "emitter expects Education, Experience, Projects, Skills."
        )
    return text[:head_at], text[tail_at:]


def _dates(entry):
    """A date range as the template renders it, or empty when undated."""
    start, end = entry.get("start_date"), entry.get("end_date")
    if start and end:
        return f"{escape(start)} - {escape(end)}"
    return escape(start or end or "")


def _items(bullets):
    return "\n".join(f"        \\resumeItem{{{escape(b)}}}" for b in bullets)


def render_experience(groups):
    """
    Summary:
        Emit the Experience section from grouped work entries.

    Parameters:
        groups (list[dict]): As returned by `generate.group_by_role`.

    Returns:
        str: A complete `\\section{Experience}` block, empty when there is
            nothing to show.
    """
    if not groups:
        return ""
    parts = [EXPERIENCE_MARKER, "\\section{Experience}",
             "  \\resumeSubHeadingListStart", ""]
    for entry in groups:
        parts.append("    \\resumeSubheading")
        parts.append(
            f"      {{{escape(entry.get('role'))}}}{{{_dates(entry)}}}"
        )
        parts.append(
            f"      {{{escape(entry.get('organisation'))}}}"
            f"{{{escape(entry.get('location'))}}}"
        )
        parts.append("      \\resumeItemListStart")
        parts.append(_items(entry["bullets"]))
        parts.append("      \\resumeItemListEnd")
        parts.append("")
    parts.append("  \\resumeSubHeadingListEnd")
    parts.append("")
    return "\n".join(parts)


def render_projects(groups):
    """
    Summary:
        Emit the Projects section from grouped project entries.

    Parameters:
        groups (list[dict]): As returned by `generate.group_by_role`.

    Returns:
        str: A complete `\\section{Projects}` block, empty when there is
            nothing to show.
    """
    if not groups:
        return ""
    parts = [PROJECTS_MARKER, "\\section{Projects}",
             "    \\resumeSubHeadingListStart", ""]
    for entry in groups:
        name = escape(entry.get("role") or entry.get("organisation"))
        where = escape(entry.get("organisation"))
        title = f"\\textbf{{{name}}}"
        # The organisation is worth showing only when it says something the
        # project name does not.
        if where and where != name:
            title += f" $|$ \\emph{{{where}}}"
        parts.append("      \\resumeProjectHeading")
        parts.append(f"          {{{title}}}{{{_dates(entry)}}}")
        parts.append("          \\resumeItemListStart")
        parts.append(_items(entry["bullets"]))
        parts.append("          \\resumeItemListEnd")
        parts.append("")
    parts.append("    \\resumeSubHeadingListEnd")
    parts.append("")
    return "\n".join(parts)


def render_resume(master, work_groups, project_groups):
    """
    Summary:
        Produce a complete resume by splicing generated sections into the
        master.

    Parameters:
        master (str): The master document's text.
        work_groups (list[dict]): Grouped `work` entries.
        project_groups (list[dict]): Grouped `project` entries.

    Returns:
        str: A compilable LaTeX document.

    Raises:
        ValueError: From `split_master` when the master's markers are missing.
    """
    head, tail = split_master(master)
    return head + render_experience(work_groups) + render_projects(project_groups) + tail


#: Shipped with the repo rather than sourced from the vault, which has no
#: cover-letter master to point at.
DEFAULT_COVER_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "cover_letter.tex",
)

#: Never a guessed name. A letter addressed to the wrong person is worse than
#: one addressed to nobody, and a model asked for a recruiter's name will
#: supply one whether or not it found it.
SALUTATION = "Dear Hiring Manager,"


def cover_template_path():
    """
    Summary:
        Locate the cover letter template.

    Returns:
        str: `JOB_BUILDER_COVER_MASTER` when set, otherwise the bundled
            template.
    """
    _load_env()
    configured = (os.environ.get("JOB_BUILDER_COVER_MASTER", "") or "").strip()
    return configured or DEFAULT_COVER_TEMPLATE


def letter_paragraphs(letter):
    """
    Summary:
        Flatten a structured letter into ordered paragraphs.

    Parameters:
        letter (Mapping): Under `opening`, `match` (a list), `why_here`, and
            `closing`. See `pipeline.cover_letter.write_letter`.

    Returns:
        list[str]: Non-empty paragraphs in reading order - who you are, the
            qualification match, why this company, and the ask.

    Note:
        The four-field shape is what makes the letter four or five paragraphs
        without asking a model to count. `match` carries one or two.
    """
    ordered = [letter.get("opening")]
    ordered.extend(letter.get("match") or [])
    ordered.append(letter.get("why_here"))
    ordered.append(letter.get("closing"))
    return [str(p).strip() for p in ordered if p and str(p).strip()]


def render_letter(profile, letter, template=None, today=None):
    """
    Summary:
        Produce a compilable cover letter document.

    Parameters:
        profile (Mapping): Contact details, as `generate.ArtifactBuilder.profile`
            returns them.
        letter (Mapping): The structured letter. See `letter_paragraphs`.
        template (str | None): Override the template text, for tests.
        today (str | None): Date line. Defaults to today, long form.

    Returns:
        str: A complete LaTeX document.

    Raises:
        FileNotFoundError: When the template file is missing.
    """
    if template is None:
        path = cover_template_path()
        if not os.path.exists(path):
            raise FileNotFoundError(f"Cover letter template not found at {path}.")
        with open(path, encoding="utf-8") as handle:
            template = handle.read()

    if today is None:
        # Built from parts rather than strftime: the no-pad day directive is
        # %-d on Linux and %#d on Windows, and neither is portable.
        stamp = date.today()
        today = f"{stamp.strftime('%B')} {stamp.day}, {stamp.year}"

    contact = " $|$ ".join(
        escape(part) for part in (
            profile.get("phone"), profile.get("email"),
            profile.get("location"), profile.get("website"),
        ) if part
    )
    body = "\n\n".join(escape(p) for p in letter_paragraphs(letter))

    return (template
            .replace("__NAME__", escape(profile.get("name")))
            .replace("__CONTACT__", contact)
            .replace("__DATE__", escape(today))
            .replace("__SALUTATION__", escape(SALUTATION))
            .replace("__BODY__", body))


def compile_pdf(tex_path, engine=None):
    """Best-effort PDF. Returns None when no engine is installed.

    Absence of a LaTeX distribution is not an error: the `.tex` beside it is
    the real output, and it compiles on Overleaf. This mirrors how `render_pdf`
    treats a missing Typst.

    Summary:
        Compile a `.tex` file to PDF with whichever engine is available.

    Parameters:
        tex_path (str): The document to compile. Output lands beside it.
        engine (str | None): Force one engine. Defaults to
            `JOB_BUILDER_LATEX_ENGINE`, then the first of `ENGINES` on PATH.

    Returns:
        str | None: Path to the PDF, or None when no engine was found or the
            compile produced nothing.
    """
    _load_env()
    forced = engine or (os.environ.get("JOB_BUILDER_LATEX_ENGINE", "") or "").strip()
    candidates = (forced,) if forced else ENGINES
    chosen = next((name for name in candidates if shutil.which(name)), None)
    if chosen is None:
        log.info(
            "No LaTeX engine found (looked for %s); leaving %s uncompiled",
            ", ".join(candidates), os.path.basename(tex_path),
        )
        return None

    directory = os.path.dirname(os.path.abspath(tex_path))
    if chosen == "tectonic":
        command = [chosen, "--outdir", directory, tex_path]
    elif chosen == "latexmk":
        command = [chosen, "-pdf", "-interaction=nonstopmode",
                   f"-outdir={directory}", tex_path]
    else:
        command = [chosen, "-interaction=nonstopmode",
                   f"-output-directory={directory}", tex_path]

    try:
        subprocess.run(command, check=True, capture_output=True,
                       timeout=COMPILE_TIMEOUT, cwd=directory)
    except (subprocess.SubprocessError, OSError) as exc:
        log.info("%s could not compile %s (%s); the .tex is still usable",
                 chosen, os.path.basename(tex_path), exc)
        return None

    pdf_path = os.path.splitext(tex_path)[0] + ".pdf"
    return pdf_path if os.path.exists(pdf_path) else None


def safe_filename(text):
    """
    Summary:
        Turn a company or role name into something a filesystem accepts.

    Parameters:
        text (str): Arbitrary text.

    Returns:
        str: Alphanumerics, spaces, hyphens and underscores only, collapsed and
            trimmed. "document" when nothing survives.
    """
    cleaned = re.sub(r"[^\w\s-]", "", str(text or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "document"
