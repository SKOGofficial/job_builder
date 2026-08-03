"""LinkedIn job-alert digests.

The load-bearing detail is that the numeric job ID sits in the URL path, even
when the link is a tracking redirect:

    https://www.linkedin.com/comm/jobs/view/4123456789/?trackingId=abc&refId=...

So the ID can be pulled out without following the redirect - which matters for
two reasons. Following it costs a request and can burn a single-use tracking
token, and the canonical URL rebuilt from the ID keeps working long after the
tracking wrapper has expired. A to-apply list whose links 404 is worse than no
list at all.
"""

import re

from pipeline.parsers.base import (
    Posting,
    collect_anchors,
    looks_like_noise,
    split_company_location,
)

BOARD = "linkedin"

#: Matches both /jobs/view/<id> and the /comm/ tracking variant.
JOB_URL = re.compile(
    r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", re.IGNORECASE
)

#: Anchors that are chrome, not postings.
CHROME = re.compile(
    r"/(?:unsubscribe|settings|help|feed|company|in/|psettings|comm/psettings)",
    re.IGNORECASE,
)


def matches(message):
    sender = (message.get("sender") or "").lower()
    body = message.get("body_text") or ""
    return "linkedin.com" in sender or "linkedin.com/comm/jobs" in body.lower()


def canonical_url(job_id):
    """Stable link, rebuilt from the ID rather than reused from the email."""
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def parse(message):
    """Extract postings from one LinkedIn alert email.

    Returns a list of `Posting`. Entries missing a company are still returned:
    the caller sends those to the LLM fallback to complete rather than dropping
    a real opening.
    """
    body = message.get("body_html") or message.get("body_text") or ""
    postings = {}

    for anchor in collect_anchors(body):
        href = anchor["href"]
        found = JOB_URL.search(href)
        if not found or CHROME.search(href):
            continue

        job_id = found.group(1)
        title = anchor["text"].strip()
        if not title or looks_like_noise(title):
            # Some cards wrap the company logo in the same link; the title
            # anchor is the one carrying text.
            continue

        company, location = split_company_location(anchor["trailing"])

        # The same posting is often linked twice (logo card and title). Keep
        # whichever pass found the most fields.
        existing = postings.get(job_id)
        candidate = Posting(
            title=title,
            company=company,
            location=location,
            apply_url=canonical_url(job_id),
            tracking_url=href,
            board=BOARD,
            board_job_id=job_id,
        )
        if existing is None or _score(candidate) > _score(existing):
            postings[job_id] = candidate

    return list(postings.values())


def _score(posting):
    return sum(1 for value in (posting.title, posting.company, posting.location) if value)
