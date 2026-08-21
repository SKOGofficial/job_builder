"""What the system asks a model to do, and which model it asks.

Every distinct model call in the project is named here. Naming them is what
makes "use the cheap model for routing and the good one for research" a setting
rather than a code change, and what lets the Settings page show a row per job
instead of a single global model picker.

Three sources decide a task's chain, in order:

1. `provider_settings` in the database - an explicit choice made in Settings.
2. `LLM_ROUTE_<TASK>` in the environment.
3. The `default_chain` declared below.

An absent database row means "no opinion", not "chose the default", so a task
nobody has edited keeps following `.env` as `.env` changes. That distinction is
why "reset to defaults" deletes the row rather than writing today's default
into it.
"""

import os

#: Tasks whose model returns JSON validated by a caller-supplied parser. Served
#: by anything exposing `complete_json`.
SHAPE_JSON = "json"

#: Tasks that research a role and return `(payload, input_tokens,
#: output_tokens)`. Served by anything exposing `research`.
SHAPE_RESEARCH = "research"


class Task:
    """One kind of work the system asks a model to do."""

    def __init__(self, label, shape, max_tokens, default_chain, description=""):
        self.label = label
        self.shape = shape
        self.max_tokens = max_tokens
        self.default_chain = tuple(default_chain)
        self.description = description


#: Task identifiers are persisted in `provider_settings.task`. Renaming one
#: orphans a user's saved routing silently, so they are frozen once shipped -
#: change the `label` instead, which is what the UI shows.
#:
#: `max_tokens` here is documentation and a budget hint. The real value still
#: comes from the call site, so a stage that asks for more than its declared
#: figure keeps working.
TASKS = {
    "route_email": Task(
        "Route incoming email", SHAPE_JSON, 200, ("groq", "gemini"),
        "Decides whether an email is a job alert, an update on an application, "
        "an acknowledgement, or not job related at all.",
    ),
    "extract_alert": Task(
        "Extract job alerts", SHAPE_JSON, 1500, ("groq", "gemini"),
        "Pulls every posting out of an alert digest. The most expensive call "
        "in the pipeline, and the one that creates leads.",
    ),
    "extract_update": Task(
        "Read application updates", SHAPE_JSON, 400, ("groq", "gemini"),
        "Reads which role an update email concerns and what happened.",
    ),
    "extract_acknowledgement": Task(
        "Read acknowledgements", SHAPE_JSON, 400, ("groq", "gemini"),
        "Reads which role a 'thanks for applying' email confirms.",
    ),
    # 800, not 200. The reply is two short fields, so 200 looked generous -
    # but Gemini 3.x spends output budget on reasoning before it emits any
    # text, and this prompt carries the whole profile. Measured against a
    # filled-in profile, 200 returned `{"score": 0` and nothing else, which
    # `parse_score` correctly rejected as invalid JSON, which left the lead
    # unscored and silently ungated. Groq never showed it: it does not reason
    # into the output budget, so the fault only appeared on failover.
    "score_relevance": Task(
        "Score lead relevance", SHAPE_JSON, 800, ("groq", "gemini"),
        "Scores a lead against your profile. The gate that decides which "
        "leads are worth researching.",
    ),
    "classify_reply": Task(
        "Classify matched replies", SHAPE_JSON, 200, ("groq", "gemini"),
        "Labels a reply as a rejection, offer, interview, or assessment.",
    ),
    "research": Task(
        "Research a company and role", SHAPE_RESEARCH, 4096, ("gemini", "anthropic"),
        "Researches the company and the opening so the resume can be tailored "
        "to it. The only task that reaches the web.",
    ),
    # Gemini leads because this is the only output an employer reads in the
    # applicant's own voice, and it runs once per application rather than per
    # email. Groq rather than Anthropic behind it: Claude is registered for
    # SHAPE_RESEARCH only, so it cannot serve a plain JSON completion at all -
    # a chain naming it here would be unroutable.
    "write_cover_letter": Task(
        "Write a cover letter", SHAPE_JSON, 1200, ("gemini", "groq"),
        "Writes the covering letter for one application, from the requirements "
        "the research found and the experience bullets that answer them.",
    ),
    # The second task that reaches the web, and the only one a user triggers by
    # hand. Same chain as `research` for the same reasons - Gemini's grounding
    # is cheaper and Claude picks up whatever it cannot take.
    "check_openings": Task(
        "Check a company for openings", SHAPE_RESEARCH, 2048,
        ("gemini", "anthropic"),
        "Searches one company's careers page for the roles it is advertising "
        "now. Runs only when you press Check now for a contact.",
    ),
    # Gemini leads for the same reason it leads the cover letter: this is
    # output a real person reads in the applicant's own voice. Groq rather than
    # Anthropic behind it, because Claude is registered for SHAPE_RESEARCH only
    # and a chain naming it here would be unroutable.
    # 2000 for an email of about 200 tokens, and the gap is not slack. Gemini's
    # flash models think before they answer, and `maxOutputTokens` is the
    # ceiling on thinking *and* answer together - so a budget sized to the
    # email is spent reasoning about it, and the reply arrives truncated
    # mid-JSON. Measured: 700 cut off after the subject line; 2500 completed
    # with room to spare.
    "draft_referral": Task(
        "Draft a referral request", SHAPE_JSON, 2000, ("gemini", "groq"),
        "Writes the short email asking someone you know to refer you for a "
        "specific opening at their company.",
    ),
}

#: Gap-filling shares `AlertHandler`'s injected client with alert extraction -
#: `parse_alert` receives one client and uses it for both - so the two cannot
#: route separately without changing that signature. Routed together, and shown
#: as a single row in Settings.
TASK_ALIASES = {"complete_posting": "extract_alert"}

#: Consulted when a task's whole chain is unavailable. Groq rather than Gemini
#: because it has no daily ceiling to be locked out of: a per-minute limit
#: always clears, so there is always something to wait for.
DEFAULT_PROVIDER = "groq"


def resolve(task):
    """
    Summary:
        Resolve a task id through the alias table.

    Parameters:
        task (str): A task id, possibly an alias.

    Returns:
        str: The canonical task id.

    Raises:
        KeyError: If the id is neither a known task nor a known alias. Raised
            rather than defaulted because a typo'd task name would otherwise
            route silently to whatever the fallback was, and nothing would look
            wrong until the bill or the labels did.
    """
    canonical = TASK_ALIASES.get(task, task)
    if canonical not in TASKS:
        raise KeyError(f"Unknown model task: {task!r}")
    return canonical


def env_chain(task):
    """Read a routing override out of the environment.

    Summary:
        Parse `LLM_ROUTE_<TASK>` into a provider chain.

    Parameters:
        task (str): The canonical task id.

    Returns:
        tuple[str, ...] | None: The chain, or None when unset. A value of
            "none" or "off" yields an empty tuple, which disables the task -
            distinct from None, which means "no override".
    """
    raw = os.environ.get(f"LLM_ROUTE_{task.upper()}")
    if raw is None or not raw.strip():
        return None
    if raw.strip().lower() in ("none", "off", "disabled"):
        return ()
    names = [part.strip().lower() for part in raw.split(",")]
    return tuple(name for name in names if name and name != "none")


def default_provider():
    """
    Summary:
        The provider to fall back on when a whole chain is unavailable.

    Returns:
        str: A provider name, from `LLM_DEFAULT_PROVIDER` or `DEFAULT_PROVIDER`.
    """
    return (os.environ.get("LLM_DEFAULT_PROVIDER") or "").strip().lower() \
        or DEFAULT_PROVIDER


def chain_for(task, saved=None):
    """Decide which providers serve a task, in order.

    Summary:
        Resolve a task's provider chain from saved settings, environment, and
        declared defaults.

    Parameters:
        task (str): A task id or alias.
        saved (dict | None): Rows from `MailStore.provider_routes`, mapping
            task id to `(primary, fallback)`. A task absent from this mapping
            has no explicit choice and falls through to the environment.

    Returns:
        tuple[str, ...]: Provider names to try in order. Empty when the task
            has been turned off.

    Raises:
        KeyError: If the task id is unknown.

    Note:
        Duplicates are removed while preserving order, so a saved row naming
        the same provider twice - easy to produce in two dropdowns - costs one
        attempt rather than two.
    """
    canonical = resolve(task)
    if saved and canonical in saved:
        primary, fallback = saved[canonical]
        chain = tuple(name for name in (primary, fallback) if name)
    else:
        from_env = env_chain(canonical)
        chain = from_env if from_env is not None else TASKS[canonical].default_chain

    seen = []
    for name in chain:
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def tasks_for_shape(shape):
    """
    Summary:
        List the task ids that expect a given client shape.

    Parameters:
        shape (str): `SHAPE_JSON` or `SHAPE_RESEARCH`.

    Returns:
        list[str]: Matching task ids, in registry order.
    """
    return [name for name, task in TASKS.items() if task.shape == shape]
