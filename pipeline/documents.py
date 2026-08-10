"""Turning a stored selection back into a document on disk.

Nothing is cached. A resume is re-rendered from its experience ids every time
it is asked for, which is the point of storing ids rather than files: a bullet
edited this morning is in the download this afternoon, with nothing to
invalidate and no stale copy to find later.

The output goes straight to the user's Downloads folder rather than through the
browser. The app usually runs in a native window where browser download
semantics do not apply, and the server is always the same machine as the user,
so writing the file is both simpler and more reliable than offering it.
"""

import logging
import os
import shutil
import tempfile

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised only without the dep
    load_dotenv = None

from pipeline import latex
from pipeline.generate import (
    ARTIFACT_COVER_LETTER,
    ARTIFACT_RESUME,
    group_by_role,
)

log = logging.getLogger(__name__)


def _load_env():
    if load_dotenv:
        env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
        )
        load_dotenv(dotenv_path=env_path)


def downloads_dir():
    """
    Summary:
        Where finished documents are written.

    Returns:
        str: `JOB_BUILDER_DOWNLOAD_DIR` when set, otherwise the user's
            Downloads folder. Falls back to the home directory when Downloads
            does not exist, which is the case on a stripped container.
    """
    _load_env()
    configured = (os.environ.get("JOB_BUILDER_DOWNLOAD_DIR", "") or "").strip()
    if configured:
        return os.path.expanduser(configured)
    home = os.path.expanduser("~")
    downloads = os.path.join(home, "Downloads")
    return downloads if os.path.isdir(downloads) else home


def unique_path(directory, filename):
    """
    Summary:
        A path in `directory` that does not already exist.

    Parameters:
        directory (str): Target directory.
        filename (str): Desired name, extension included.

    Returns:
        str: The path, suffixed " (2)", " (3)" and so on when taken.

    Note:
        Never overwrites. Two tailored resumes for the same company are a
        normal thing to have, and silently replacing the first would lose work
        the user cannot get back.
    """
    stem, extension = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    counter = 2
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem} ({counter}){extension}")
        counter += 1
    return candidate


def document_name(kind, lead):
    """
    Summary:
        A filename that says what the document is and who it is for.

    Parameters:
        kind (str): `resume` or `cover_letter`.
        lead (Mapping): The role it was tailored to.

    Returns:
        str: A base name with no extension.
    """
    label = "Cover Letter" if kind == ARTIFACT_COVER_LETTER else "Resume"
    company = latex.safe_filename(lead.get("company") or "")
    role = latex.safe_filename(lead.get("title") or "")
    parts = [part for part in (company, role) if part]
    return f"{label} - {' - '.join(parts)}" if parts else label


def build_resume_tex(mail, store_profile, selection, master=None):
    """
    Summary:
        Re-render a resume from a stored selection.

    Parameters:
        mail (MailStore): Where the experience rows live.
        store_profile (Mapping): Contact details for the header. Unused for the
            resume itself, whose header comes from the master, but accepted so
            both builders share a signature.
        selection (Mapping): As `MailStore.selection_for` returns it.
        master (str | None): Override the master text, for tests.

    Returns:
        str: A complete LaTeX document.

    Raises:
        FileNotFoundError: When no master resume is configured or found.
        ValueError: When the master's section markers are missing.

    Note:
        Bullets are re-read by id and re-ordered to match the stored sequence.
        An id that no longer exists is skipped rather than failing the render -
        a deleted bullet should cost one line, not the document.
    """
    by_id = {row["id"]: dict(row) for row in mail.list_experiences()}
    rows = [by_id[i] for i in selection.get("bullet_ids") or [] if i in by_id]
    groups = group_by_role(rows)
    work = [g for g in groups if g["kind"] == "work"]
    projects = [g for g in groups if g["kind"] == "project"]
    return latex.render_resume(master or latex.load_master(), work, projects)


def build_letter_tex(profile, selection):
    """
    Summary:
        Render a covering letter from its stored text.

    Parameters:
        profile (Mapping): Contact details for the header.
        selection (Mapping): As `MailStore.selection_for` returns it.

    Returns:
        str: A complete LaTeX document.

    Raises:
        FileNotFoundError: When the letter template is missing.
    """
    return latex.render_letter(profile, selection.get("letter") or {})


def deliver(tex_text, base_name, target_dir=None):
    """Compile if possible, and put the result in Downloads either way.

    Summary:
        Write, compile, and deliver one document.

    Parameters:
        tex_text (str): The complete LaTeX document.
        base_name (str): Filename without extension.
        target_dir (str | None): Override the downloads directory.

    Returns:
        tuple[str, bool]: The delivered path, and whether it is a PDF.

    Raises:
        OSError: If the downloads directory cannot be written to.

    Note:
        Blocking - it shells out to a LaTeX engine. Callers on the event loop
        must push this to a thread, which is safe because this function takes
        finished text and never a database handle. That is deliberate: a
        signature with no store in it cannot be made to touch sqlite from the
        wrong thread.

        The `.tex` is delivered when no engine is installed rather than
        nothing. It compiles on Overleaf, so the feature is useful before any
        engine is chosen, which matters because none is installed today.
    """
    destination = target_dir or downloads_dir()
    os.makedirs(destination, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="job_builder_") as work_dir:
        tex_path = os.path.join(work_dir, f"{base_name}.tex")
        with open(tex_path, "w", encoding="utf-8") as handle:
            handle.write(tex_text)

        pdf_path = latex.compile_pdf(tex_path)
        source = pdf_path or tex_path
        extension = ".pdf" if pdf_path else ".tex"
        final = unique_path(destination, f"{base_name}{extension}")
        shutil.copyfile(source, final)

    log.info("Wrote %s", final)
    return final, bool(pdf_path)


def build_document(mail, profile, lead, kind, master=None):
    """Read everything the document needs and render the LaTeX.

    Summary:
        Turn a stored selection into a complete LaTeX document.

    Parameters:
        mail (MailStore): For the experience rows and the selection.
        profile (Mapping): Contact details.
        lead (Mapping): The role the document is for.
        kind (str): `resume` or `cover_letter`.
        master (str | None): Override the master text, for tests.

    Returns:
        str: A compilable LaTeX document.

    Raises:
        LookupError: When nothing has been recorded for this lead and kind, so
            the caller can say "not prepared yet" rather than "failed".
        FileNotFoundError: When the master or the letter template is missing.

    Note:
        **Must run on the thread that owns the sqlite connection.** This half
        exists separately from `deliver` precisely so the blocking half can be
        pushed to a worker without carrying a database handle with it - which
        is a `ProgrammingError` the moment the worker touches it. Rendering is
        string work and costs nothing on the loop.
    """
    selection = mail.selection_for(lead["identity_key"], kind)
    if selection is None:
        raise LookupError(f"No {kind.replace('_', ' ')} has been prepared yet.")

    if kind == ARTIFACT_RESUME:
        return build_resume_tex(mail, profile, selection, master=master)
    return build_letter_tex(profile, selection)
