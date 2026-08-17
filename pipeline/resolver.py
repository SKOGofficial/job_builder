"""Which job is this email about?

The piece with the most design risk in the pipeline, because the failure mode
is quiet. Three open applications at one large company plus an update email
that says only "your application" is not an edge case - it is Tuesday. Guessing
wrong attaches a rejection to the wrong role and marks the wrong job dead.

So this refuses to guess. Resolution walks from strongest signal to weakest and
stops at the first that is unambiguous; when several jobs remain plausible it
returns unresolved *with* the candidates, and the caller records that it tried
and moves on. Refusing is the useful outcome here - an update email that cannot
be placed leaves every job's status alone, which is right, whereas a coin flip
marks one of them dead.

No LLM here. Extraction of (title, company, location) from prose happens in
`pipeline/extract.py`; this module is the matching logic, so it stays pure and
fully testable without a network or an API key.
"""

import logging

from clients.gmail_client import GENERIC_DOMAINS, sender_domain
from utilities.identity import (
    candidate_keys,
    company_slug,
    identity_key,
    normalize_title,
)
from utilities.mailstore import LEAD_NEW, LEAD_PREPARING, LEAD_READY

log = logging.getLogger(__name__)

#: Lead statuses that can still receive an email. A dismissed lead is not a
#: resolution target, and an applied one has become a job that the job tier
#: will find first.
LEAD_RESOLVABLE_STATUSES = (LEAD_NEW, LEAD_PREPARING, LEAD_READY)

RESOLVED_BOARD_ID = "board_job_id"
RESOLVED_IDENTITY = "exact_identity"
RESOLVED_DOMAIN_TITLE = "domain+title"
RESOLVED_DOMAIN_ONLY = "domain_sole_open"

#: Coverage above which a text is considered to name a role. Set high on
#: purpose: "Backend Engineer" and "Frontend Engineer" share a token, and
#: conflating those is exactly the mistake this module exists to avoid.
TITLE_MATCH_THRESHOLD = 0.6


def title_similarity(text, title):
    """How much of `title` appears in `text`, 0.0 to 1.0.

    Deliberately **asymmetric**. The usual caller compares a subject line
    ("Your Backend Engineer application - an update") against a stored job
    title ("Backend Engineer"), and a symmetric measure like Jaccard punishes
    the subject for its extra words - that pair scores 0.5 and falls below the
    threshold, so a perfectly clear match is thrown away. Coverage of the title
    scores it 1.0, which is the question actually being asked: does this text
    name this job?

    Caveat: a one-word title ("Engineer") matches any text containing that
    word. Nothing better is available from the title alone, and the
    multi-candidate check in `_by_domain` is what stops that becoming a wrong
    link - if two jobs both score highly, neither is chosen.
    """
    text_tokens = set(normalize_title(text).split())
    title_tokens = set(normalize_title(title).split())
    if not text_tokens or not title_tokens:
        return 0.0
    return len(text_tokens & title_tokens) / len(title_tokens)


class Resolution:
    """The outcome of trying to place a message against a job.

    `candidates` is populated when resolution failed *because* several jobs
    were plausible, which is the difference between "this email is about a role
    I do not track" and "it is about one of these three and I cannot say
    which". Handlers report the distinction rather than treating both as a
    blank failure.
    """

    def __init__(self, identity_key=None, confidence=0.0, resolved_by=None,
                 candidates=(), reason=""):
        self.identity_key = identity_key
        self.confidence = confidence
        self.resolved_by = resolved_by
        self.candidates = list(candidates)
        self.reason = reason

    @property
    def resolved(self):
        return self.identity_key is not None

    @property
    def ambiguous(self):
        return not self.resolved and len(self.candidates) > 1

    def __repr__(self):
        if self.resolved:
            return (f"<Resolution {self.identity_key} via {self.resolved_by} "
                    f"@{self.confidence:.2f}>")
        return f"<Resolution unresolved ({len(self.candidates)} candidates): {self.reason}>"


class JobResolver:
    def __init__(self, store, mail):
        self.store = store
        self.mail = mail

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _domain_root(domain):
        return domain.rsplit(".", 1)[0].replace(".", "").replace("-", "")

    def _company_owns_domain(self, company, root):
        slug = company_slug(company)
        return bool(slug) and (slug in root or root.endswith(slug))

    def jobs_for_domain(self, domain):
        """Applications whose company plausibly owns this sender domain.

        Free mail providers never count: a Gmail address tells us nothing about
        which company sent it.
        """
        if not domain or domain in GENERIC_DOMAINS:
            return []
        root = self._domain_root(domain)
        return [row for row in self.store.list_jobs()
                if self._company_owns_domain(row["company"], root)]

    def leads_for_domain(self, domain):
        """Leads whose company plausibly owns this sender domain.

        Leads are resolution targets too, not just jobs. An acknowledgement for
        a role still on the to-apply list must find the lead, or the
        acknowledgement handler creates a *second* row for the same job under
        whatever loose title the email used - and the lead never leaves the
        to-apply list.
        """
        if not domain or domain in GENERIC_DOMAINS:
            return []
        root = self._domain_root(domain)
        return [row for row in self.mail.list_leads(LEAD_RESOLVABLE_STATUSES)
                if self._company_owns_domain(row["company"], root)]

    def candidates_for_domain(self, domain):
        """Open applications and live leads, as one comparable list.

        Each entry is `(identity_key, title, reference)`. Mixing the two is
        deliberate - from the resolver's point of view they are both "a role at
        this company that this email might be about", and having two plausible
        targets is ambiguous whichever tables they came from.
        """
        candidates = []
        jobs = self.jobs_for_domain(domain)
        open_jobs = [job for job in jobs if self.is_open(job)] or jobs
        for job in open_jobs:
            if job["identity_key"]:
                candidates.append((job["identity_key"], job["position_title"],
                                   job["job_id"]))
        for lead in self.leads_for_domain(domain):
            candidates.append((lead["identity_key"], lead["title"],
                               f"lead:{lead['id']}"))
        return candidates

    @staticmethod
    def is_open(job):
        """Still awaiting a decision, so a status update plausibly applies."""
        return job["status"] in {"Pending", "Applied", "OA Received"}

    # --- resolution --------------------------------------------------------

    def resolve(self, message, extracted=None):
        """Place a message against a job identity.

        `extracted` carries whatever the model pulled out of the email -
        `title`, `company`, `location`, `board`, `board_job_id` - all optional.
        With none of it, only the domain tiers can fire.
        """
        extracted = extracted or {}

        found = self._by_board_reference(extracted)
        if found:
            return found

        found = self._by_identity(extracted)
        if found:
            return found

        return self._by_domain(message, extracted)

    def _by_board_reference(self, extracted):
        """Tier 1: the board's own ID. Exact, no ambiguity possible."""
        board = extracted.get("board")
        board_job_id = extracted.get("board_job_id")
        if not (board and board_job_id):
            return None
        job = self.store.job_by_board_reference(board, board_job_id)
        if job is None or not job["identity_key"]:
            return None
        return Resolution(
            identity_key=job["identity_key"],
            confidence=1.0,
            resolved_by=RESOLVED_BOARD_ID,
            reason=f"{board} posting {board_job_id}",
        )

    def _by_identity(self, extracted):
        """Tier 2: the computed identity key matches a job or a lead.

        Tries the location-qualified key first and the bare title+company key
        second, so rows written before locations existed are reachable.
        """
        title = extracted.get("title")
        company = extracted.get("company")
        if not (title and company):
            return None

        for key in candidate_keys(title, company, extracted.get("location")):
            if self.store.job_by_identity(key) is not None:
                return Resolution(
                    identity_key=key, confidence=0.95,
                    resolved_by=RESOLVED_IDENTITY,
                    reason="exact identity match against an application",
                )
            if self.mail.lead_by_identity(key) is not None:
                return Resolution(
                    identity_key=key, confidence=0.9,
                    resolved_by=RESOLVED_IDENTITY,
                    reason="exact identity match against a lead",
                )
        return None

    def _by_domain(self, message, extracted):
        """Tiers 3 and 4: infer from the sender's company, or refuse.

        This is where ambiguity has to be handled honestly. Several open roles
        at one company and nothing to tell them apart means the answer is
        "I don't know", not a coin flip.
        """
        domain = sender_domain(message.get("sender", ""))
        candidates = self.candidates_for_domain(domain)
        if not candidates:
            return Resolution(reason=f"nothing matches sender domain {domain!r}")

        # The subject is a usable fallback when the model read no title: "Your
        # Backend Engineer application" names the role perfectly well.
        title = extracted.get("title") or message.get("subject") or ""

        # Tier 3: exactly one role whose title the email plausibly names.
        scored = [(title_similarity(title, candidate_title), candidate)
                  for candidate in candidates
                  for candidate_title in (candidate[1],)]
        strong = [(score, candidate) for score, candidate in scored
                  if score >= TITLE_MATCH_THRESHOLD]

        if len(strong) == 1:
            score, (key, _, _) = strong[0]
            return Resolution(
                identity_key=key,
                # Degraded: a domain plus a fuzzy title is weaker evidence than
                # an identity or a board ID.
                confidence=min(0.85, 0.55 + score * 0.3),
                resolved_by=RESOLVED_DOMAIN_TITLE,
                reason=f"sender domain plus title similarity {score:.2f}",
            )

        if len(strong) > 1:
            return Resolution(
                candidates=[reference for _, (_, _, reference) in strong],
                reason=(f"{len(strong)} roles at this company match the title; "
                        f"cannot tell them apart"),
            )

        # Tier 4: exactly one role at this company and no title signal. Still a
        # guess, but a single-candidate one - safe to link at low confidence so
        # the email at least appears somewhere useful.
        if len(candidates) == 1:
            return Resolution(
                identity_key=candidates[0][0],
                confidence=0.5,
                resolved_by=RESOLVED_DOMAIN_ONLY,
                reason="only one open role at this company",
            )

        return Resolution(
            candidates=[reference for _, _, reference in candidates],
            reason=(f"{len(candidates)} roles at this company and nothing in "
                    f"the email distinguishes them"),
        )

    # --- writing the link --------------------------------------------------

    def link(self, message_id, resolution, link_type):
        """Record a resolved link. No-op when unresolved.

        Deliberately separate from `resolve` so a caller can act on the
        resolution (apply a status, promote a lead) and link independently -
        a confidence too low to change a job status is still high enough to
        show the email on that job's timeline.
        """
        if not resolution.resolved:
            return False
        return self.mail.link_message(
            message_id,
            resolution.identity_key,
            link_type,
            confidence=resolution.confidence,
            resolved_by=resolution.resolved_by,
        )


def identity_for(title, company, location=None):
    """Convenience for callers that just need the key."""
    return identity_key(title, company, location)
