"""Scoring leads, so the list can be read in the order worth reading it.

This was once the gate on spending: a cheap, already-paced model scored each
lead, and anything above the bar had its research and cover letter bought
unattended. That was a defensible design when the alternative was researching
every posting in a digest - $45-150 a month, most of it on roles dismissed at a
glance - but it made the bill a function of how well the model guessed at
someone else's taste.

Generation is now a click (see `pipeline/prepare.py`), so this is no longer a
gate on anything. It is a ranking, and that is a job it is actually good at. A
score sorts the list and explains itself; a person decides.

Scoring still runs on every cycle, because it is free relative to research and
because an unsorted list of 363 leads is not a shortlist. `relevance_reason` is
stored so a score can be argued with rather than taken on faith.
"""

import asyncio
import json
import logging

from clients.llm_client import GroqNotConfigured, GroqRateLimited
from utilities.mailstore import LEAD_NEW

log = logging.getLogger(__name__)

#: Where the to-apply list stops showing leads by default.
#:
#: Low on purpose, and lower-stakes than it used to be. This once decided what
#: got researched, so an over-tight bar meant a role was silently never
#: prepared; now it only decides what is shown first, and `prepare_now`
#: bypasses it entirely. A missed good lead still costs more than a wasted row.
DEFAULT_THRESHOLD = 0.45

#: Profile key holding a user-chosen threshold, if any.
RELEVANCE_THRESHOLD_KEY = "relevance_threshold"

MAX_PROFILE_CHARS = 3000

RELEVANCE_SYSTEM_PROMPT = """You score how well a job opening fits an applicant.

You are given the applicant's profile and one job opening. Score the fit from 0 \
to 1:

- 0.8-1.0: squarely what they are looking for
- 0.5-0.8: plausible - adjacent field, or a stretch they might want
- 0.2-0.5: weak fit, but not absurd
- 0.0-0.2: wrong field, wrong seniority, or wrong location entirely

Judge on role, seniority, field, and location. Be generous with anything \
adjacent: the cost of scoring a decent role too low is a missed opportunity, \
and the cost of scoring a poor one too high is a small amount of wasted \
compute.

If the profile is empty or says nothing useful, return 0.5 - an unknown fit is \
not a bad fit.

Reply with JSON only, in this exact shape:
{"score": <number between 0 and 1>, "reason": "<one short sentence>"}"""


def parse_score(content):
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return {"score": None, "reason": "Model reply was not valid JSON."}
    if not isinstance(data, dict):
        return {"score": None, "reason": "Model reply was not a JSON object."}
    try:
        score = max(0.0, min(float(data.get("score")), 1.0))
    except (TypeError, ValueError):
        return {"score": None, "reason": "Model returned no usable score."}
    reason = data.get("reason")
    reason = reason.strip()[:200] if isinstance(reason, str) else ""
    return {"score": score, "reason": reason}


def build_messages(profile_text, lead):
    described = "\n".join(filter(None, [
        f"Title: {lead['title']}",
        f"Company: {lead['company']}" if lead["company"] else "",
        f"Location: {lead['location']}" if lead["location"] else "",
    ]))
    return [
        {"role": "system", "content": RELEVANCE_SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"<profile>\n{(profile_text or '').strip()[:MAX_PROFILE_CHARS]}\n</profile>\n\n"
            f"<opening>\n{described}\n</opening>"
        )},
    ]


def configured_threshold(store):
    """The relevance bar, as the user set it.

    Summary:
        Read the configured relevance threshold.

    Parameters:
        store (JobStore): The store holding the profile key/value table.

    Returns:
        float: The threshold, clamped to 0.0-1.0. Defaults to
            `DEFAULT_THRESHOLD`.

    Note:
        Never raises on a bad stored value - a hand-edited profile row should
        degrade to the default rather than stop the pipeline.
    """
    raw = store.get_profile_value(RELEVANCE_THRESHOLD_KEY, "")
    if not raw:
        return DEFAULT_THRESHOLD
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        log.warning("%s=%r is not a number; using %s",
                    RELEVANCE_THRESHOLD_KEY, raw, DEFAULT_THRESHOLD)
        return DEFAULT_THRESHOLD


class RelevanceScorer:
    """Scores unscored leads against the stored profile.

    Scoring is a blocking model call and goes to an executor; reading the
    profile and storing the score stay on the calling thread, which owns the
    sqlite connection.
    """

    def __init__(self, store, mail, client=None, threshold=DEFAULT_THRESHOLD,
                 executor=None):
        self.store = store
        self.mail = mail
        self.client = client
        self.threshold = threshold
        self.executor = executor or asyncio.to_thread

    def profile_text(self):
        """What the user said they are looking for.

        Both free-text pages feed this. An empty profile is handled in the
        prompt rather than here: scoring everything 0.5 means a new user still
        gets resumes prepared, just less selectively.
        """
        parts = [
            self.store.get_profile_value("profile_text", ""),
            self.store.get_profile_value("target_roles", ""),
        ]
        return "\n\n".join(part for part in parts if part).strip()

    async def score_lead(self, lead):
        """Score one lead. Returns the stored score, or None when unavailable.

        Summary:
            Ask the model how well one lead matches the stored profile and
            record the score.

        Parameters:
            lead (Mapping): The lead row to score.

        Returns:
            float | None: The stored score, or None when there is no client or
                the model returned nothing usable.

        Raises:
            GroqRateLimited: Propagated so `run` can stop the batch cleanly.
        """
        if self.client is None:
            return None
        # The profile read must happen here, on the connection-owning thread,
        # not inside the executor call below.
        messages = build_messages(self.profile_text(), lead)
        result = await self.executor(
            self.client.complete_json,
            messages,
            parse_score,
            {"score": None, "reason": "Model returned no choices."},
            200,
        )
        if result["score"] is None:
            # Leave it unscored so a later cycle retries, rather than baking in
            # a wrong number that silently suppresses the lead forever.
            log.debug("No score for lead %s: %s", lead["id"], result["reason"])
            return None
        self.mail.set_lead_relevance(lead["id"], result["score"], result["reason"])
        return result["score"]

    async def run(self, limit=50):
        """Score the backlog. Returns how many were scored.

        Summary:
            Score every lead still awaiting a relevance score, stopping cleanly
            if the model's rate limit is reached.

        Parameters:
            limit (int): Most leads to score in one pass.

        Returns:
            int: How many leads were scored.

        Note:
            A rate limit ends the pass rather than failing it. Scores already
            written are kept, and the leads not reached stay unscored, so the
            next cycle picks them up with a fresh token budget.
        """
        if self.client is None:
            log.info("Relevance scoring skipped: no model client")
            return 0
        scored = 0
        for lead in self.mail.leads_awaiting_relevance(limit):
            try:
                if await self.score_lead(lead) is not None:
                    scored += 1
            except GroqNotConfigured:
                break
            except GroqRateLimited as exc:
                # Routine, not a failure: log it as the other stages do rather
                # than letting the generic handler below print a traceback.
                log.info(
                    "Relevance scoring paused by the rate limit after %d "
                    "lead(s); retrying next cycle, in about %ss",
                    scored, exc.retry_after,
                )
                break
            except Exception:
                log.exception("Relevance scoring failed for lead %s", lead["id"])
                break
        return scored

    def worth_preparing(self, limit=10):
        """Leads that cleared the bar and have not been prepared yet."""
        return self.mail.leads_awaiting_preparation(self.threshold, limit)
