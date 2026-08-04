"""Indeed job-alert digests.

Indeed's stable identifier is the `jk` job key, which appears as a query
parameter on both the click-tracking URL and the direct one:

    https://www.indeed.com/rc/clk?jk=a1b2c3d4e5f6&from=ja&...
    https://www.indeed.com/viewjob?jk=a1b2c3d4e5f6

As with LinkedIn, the key is extracted rather than the redirect followed, and
the canonical `viewjob` URL is rebuilt from it so the apply link outlives the
tracking wrapper.
"""

import re
from urllib.parse import parse_qs, urlparse

from pipeline.parsers.base import (
    Posting,
    collect_anchors,
    looks_like_noise,
    split_company_location,
)

BOARD = "indeed"

INDEED_HOST = re.compile(r"(?:^|\.)indeed\.[a-z.]+$", re.IGNORECASE)
CHROME = re.compile(r"/(?:unsubscribe|preferences|survey|myjobs|account)",
                    re.IGNORECASE)


def matches(message):
    sender = (message.get("sender") or "").lower()
    body = (message.get("body_text") or "").lower()
    return "indeed.com" in sender or "indeed.com/rc/clk" in body


def job_key(url):
    """The `jk` parameter, or None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if not INDEED_HOST.search(parsed.hostname or ""):
        return None
    keys = parse_qs(parsed.query).get("jk")
    return keys[0] if keys else None


def canonical_url(key):
    return f"https://www.indeed.com/viewjob?jk={key}"


def parse(message):
    body = message.get("body_html") or message.get("body_text") or ""
    postings = {}

    for anchor in collect_anchors(body):
        href = anchor["href"]
        if CHROME.search(href):
            continue
        key = job_key(href)
        if not key:
            continue

        title = anchor["text"].strip()
        if not title or looks_like_noise(title):
            continue

        company, location = split_company_location(anchor["trailing"])
        candidate = Posting(
            title=title,
            company=company,
            location=location,
            apply_url=canonical_url(key),
            tracking_url=href,
            board=BOARD,
            board_job_id=key,
        )
        existing = postings.get(key)
        if existing is None or _score(candidate) > _score(existing):
            postings[key] = candidate

    return list(postings.values())


def _score(posting):
    return sum(1 for value in (posting.title, posting.company, posting.location) if value)
