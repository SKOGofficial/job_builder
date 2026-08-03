"""Scoring leads before spending real money on them.

The gate that keeps the to-apply list application-ready without the bill being
absurd. A daily digest is five to ten postings; at roughly $0.30-0.50 of Opus
tokens each, researching all of them is $45-150 a month, most of it spent on
roles that get dismissed at a glance.

So a cheap model that is already wired up and already paced scores each lead
against the stored profile first, and only leads above the bar reach the
expensive pass. The user experience is unchanged - the list still has resumes
ready when they open it - but the spend follows the roles they would actually
pursue.

The threshold is deliberately low by default. A missed good lead costs more
than a wasted dollar, and `relevance_reason` is stored so the bar can be tuned
against real data instead of guessed at.
"""

import json
import logging

from clients.llm_client import GroqNotConfigured
from utilities.mailstore import LEAD_NEW

log = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.45

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


class RelevanceScorer:
    """Scores unscored leads against the stored profile."""

    def __init__(self, store, mail, client=None, threshold=DEFAULT_THRESHOLD):
        self.store = store
        self.mail = mail
        self.client = client
        self.threshold = threshold

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

    def score_lead(self, lead):
        """Score one lead. Returns the stored score, or None when unavailable."""
        if self.client is None:
            return None
        result = self.client.complete_json(
            build_messages(self.profile_text(), lead),
            parse_score,
            {"score": None, "reason": "Model returned no choices."},
            max_tokens=200,
        )
        if result["score"] is None:
            # Leave it unscored so a later cycle retries, rather than baking in
            # a wrong number that silently suppresses the lead forever.
            log.debug("No score for lead %s: %s", lead["id"], result["reason"])
            return None
        self.mail.set_lead_relevance(lead["id"], result["score"], result["reason"])
        return result["score"]

    def run(self, limit=50):
        """Score the backlog. Returns how many were scored."""
        if self.client is None:
            log.info("Relevance scoring skipped: no model client")
            return 0
        scored = 0
        for lead in self.mail.leads_awaiting_relevance(limit):
            try:
                if self.score_lead(lead) is not None:
                    scored += 1
            except GroqNotConfigured:
                break
            except Exception:
                log.exception("Relevance scoring failed for lead %s", lead["id"])
                break
        return scored

    def worth_preparing(self, limit=10):
        """Leads that cleared the bar and have not been prepared yet."""
        return self.mail.leads_awaiting_preparation(self.threshold, limit)
