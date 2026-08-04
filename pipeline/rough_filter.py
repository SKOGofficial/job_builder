"""The rough filter: a bouncer, not a triage nurse.

Runs on headers alone, before any body is downloaded, and drops a message only
when it is confidently **not** from a job board or a company. Everything it is
unsure about survives and goes to the model.

This is a deliberate trade of cost for recall. A precision-tuned prefilter
would drop most of a mailbox, but it would also silently lose the one-off
recruiter email that matches no keyword and no known board - and you never find
out about the interview you were not shown. Paying a fraction of a cent to have
the model reject a newsletter is the cheaper mistake.

Be honest about the ceiling: "is this from a company" is close to
unfalsifiable, because nearly all bulk mail is from some company. Rules 1 and 2
are cheap structural wins; the denylist is the rule that actually compounds, and
it only compounds if the UI makes "not job related" easy to click.

Every verdict is recorded on the message row. "Why didn't I see that recruiter
email" has to be answerable, and a dropped message with no recorded reason
is not.
"""

import json
import logging
import os
import re

from clients.gmail_client import GENERIC_DOMAINS, sender_domain
from utilities.mailstore import VERDICT_PASSED

log = logging.getLogger(__name__)

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "filter_rules.json")

# Verdict values. `passed` comes from mailstore so the column vocabulary has a
# single owner; the drop reasons are local because only this module produces
# them.
DROP_DENYLISTED = "dropped_denylisted"
DROP_SOCIAL = "dropped_social_or_forum"
DROP_PERSONAL = "dropped_personal_no_keyword"
DROP_BULK = "dropped_bulk_no_keyword"

DROP_REASONS = {
    DROP_DENYLISTED: "Sender domain is on your not-job-related list",
    DROP_SOCIAL: "Gmail filed it under Social or Forums",
    DROP_PERSONAL: "Personal mail account with no job wording in the subject",
    DROP_BULK: "Automated bulk mail with no job wording",
}

DEFAULT_RULES = {
    "job_keywords": ["job", "role", "position", "application", "interview",
                     "hiring", "recruiter", "career", "offer", "resume"],
    "job_board_domains": ["linkedin.com", "indeed.com", "greenhouse.io", "lever.co"],
    "drop_labels": ["CATEGORY_SOCIAL", "CATEGORY_FORUMS", "SPAM", "TRASH"],
    "seed_denylist": [],
}


def load_rules(path=DATA_PATH):
    """Read the tuning data, falling back to conservative defaults.

    A missing or malformed file must not take the pipeline down - it degrades
    to a filter that drops less, which is the safe direction.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("Could not read filter rules at %s (%s); using defaults", path, exc)
        return dict(DEFAULT_RULES)
    merged = dict(DEFAULT_RULES)
    for key in DEFAULT_RULES:
        if isinstance(data.get(key), list):
            merged[key] = data[key]
    return merged


def _keyword_pattern(keywords):
    """One alternation, word-bounded, so 'job' does not match 'jobbing'."""
    escaped = sorted((re.escape(k) for k in keywords), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)


def domain_matches(domain, listed):
    """True when `domain` is `listed` or a subdomain of it.

    Job boards send from subdomains - `jobs-noreply@linkedin.com` but also
    `e.indeed.com` and `mail.greenhouse.io` - so a plain equality check would
    miss most of them.
    """
    domain = (domain or "").lower()
    for entry in listed:
        entry = (entry or "").lower()
        if not entry:
            continue
        if domain == entry or domain.endswith("." + entry):
            return True
    return False


class RoughFilter:
    """Header-only gate in front of body downloads and classification."""

    def __init__(self, denied_domains=(), rules=None, known_company_slugs=()):
        self.rules = rules or load_rules()
        self.denied_domains = {d.lower() for d in denied_domains}
        self.known_company_slugs = set(known_company_slugs)
        self.keywords = _keyword_pattern(self.rules["job_keywords"])
        self.board_domains = self.rules["job_board_domains"]
        self.drop_labels = set(self.rules["drop_labels"])

    # --- signals -----------------------------------------------------------

    def has_job_keyword(self, header):
        """Keyword anywhere in the visible headers.

        The snippet counts. Gmail gives it to us for free with the metadata
        fetch, and a recruiter's first line often says "about your application"
        while the subject says only "Following up".
        """
        haystack = " ".join(
            str(header.get(field) or "")
            for field in ("subject", "snippet")
        )
        return bool(self.keywords.search(haystack))

    def is_job_board(self, domain):
        return domain_matches(domain, self.board_domains)

    def is_known_company(self, domain):
        """Sender domain looks like a company already in the applications list."""
        if not domain or domain in GENERIC_DOMAINS:
            return False
        root = domain.rsplit(".", 1)[0].replace(".", "").replace("-", "")
        return any(slug and (slug in root or root.endswith(slug))
                   for slug in self.known_company_slugs)

    # --- verdict -----------------------------------------------------------

    def verdict(self, header):
        """`VERDICT_PASSED`, or a `dropped_*` reason.

        Rule order is attribution order: the most specific and most
        user-intentional reason wins, so the stats say something actionable.
        """
        domain = sender_domain(header.get("sender", ""))
        labels = set(header.get("labels") or [])

        # A board or a company we are already tracking always passes, even if
        # a later rule would have dropped it. LinkedIn sets List-Unsubscribe on
        # every alert, and a company can perfectly well reply from Gmail.
        if self.is_job_board(domain) or self.is_known_company(domain):
            return VERDICT_PASSED

        if domain and domain in self.denied_domains:
            return DROP_DENYLISTED

        if labels & self.drop_labels:
            return DROP_SOCIAL

        has_keyword = self.has_job_keyword(header)

        if domain in GENERIC_DOMAINS and not has_keyword:
            # A friend emailing from Gmail. The keyword escape hatch matters:
            # small companies and independent recruiters do use free mail.
            return DROP_PERSONAL

        if header.get("list_unsubscribe") and not has_keyword:
            return DROP_BULK

        return VERDICT_PASSED

    def explain(self, verdict):
        return DROP_REASONS.get(verdict, "Passed to the classifier")


SEED_CURSOR = "denylist_seeded"


def seed_denylist(mail, rules=None):
    """Load the shipped starter denylist into the database, once.

    `filter_rules.json` carries a `seed_denylist` of obvious non-job senders,
    but nothing read it until now - so the denylist rule started empty on every
    install and never fired. Combined with the unstored List-Unsubscribe
    header, that left the filter passing ~97% of a real mailbox through to the
    classifier.

    Seeded once and recorded, so a domain the user deliberately removes does
    not come back on the next start.
    """
    if mail.get_cursor(SEED_CURSOR):
        return 0
    rules = rules or load_rules()
    added = 0
    for domain in rules.get("seed_denylist", []):
        # Entries may be written as an address; only the domain is matched.
        domain = domain.split("@")[-1].strip().lower()
        if domain and mail.deny_sender(domain, "shipped default"):
            added += 1
    mail.set_cursor(SEED_CURSOR, "1")
    if added:
        log.info("Seeded %d domain(s) into the sender denylist", added)
    return added


def build_filter(store, mail):
    """Construct a filter wired to the current database state.

    Company slugs come from the applications table, so a reply from a company
    the user has applied to is never dropped by the generic rules.
    """
    from utilities.identity import company_slug

    seed_denylist(mail)
    slugs = {
        company_slug(row["company"])
        for row in store.list_jobs()
        if row["company"]
    }
    slugs.discard("")
    return RoughFilter(denied_domains=mail.denied_domains(),
                       known_company_slugs=slugs)
