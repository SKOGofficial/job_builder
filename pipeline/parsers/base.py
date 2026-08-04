"""Shared machinery for turning a job-alert email into postings.

Parsing HTML alert digests with the standard library rather than adding
BeautifulSoup. The job here is narrow - pull anchors and the text around them -
and `html.parser` covers it without a new dependency on a box that has to stay
up unattended.

The split of responsibility matters. Anchors, board names, and board job IDs
come out of the URL, which is stable and exactly reproducible. Titles usually
come from the anchor text, which is nearly as reliable. Company and location
live in whatever table soup the board felt like emitting this quarter, so a
parser reports what it is confident about and leaves the rest for the LLM
fallback to fill in. Guessing badly here is worse than not guessing: a wrong
company produces a wrong identity key, and the lead never matches the
acknowledgement email that should promote it.
"""

import re
from html import unescape
from html.parser import HTMLParser


class Posting:
    """One job extracted from an alert email.

    `title` and `apply_url` are the minimum for a usable lead. `company` may be
    None when the parser could not find it confidently - the caller then sends
    the posting to the LLM fallback rather than inventing one.
    """

    __slots__ = ("title", "company", "location", "apply_url", "tracking_url",
                 "board", "board_job_id")

    def __init__(self, title=None, company=None, location=None, apply_url=None,
                 tracking_url=None, board=None, board_job_id=None):
        self.title = (title or "").strip() or None
        self.company = (company or "").strip() or None
        self.location = (location or "").strip() or None
        self.apply_url = apply_url
        self.tracking_url = tracking_url
        self.board = board
        self.board_job_id = board_job_id

    @property
    def complete(self):
        """Enough to build an identity key and a working link."""
        return bool(self.title and self.company and self.apply_url)

    def as_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}

    def __repr__(self):
        return (f"<Posting {self.title!r} at {self.company!r} "
                f"({self.board}:{self.board_job_id})>")


class AnchorCollector(HTMLParser):
    """Collects anchors with their text and the text that follows them.

    Alert digests put the title inside the link and the company and location in
    the next cell or two, so the trailing text is where the remaining fields
    are if they are anywhere.
    """

    #: How much text after an anchor to keep as context for company/location.
    TRAILING_CHARS = 240

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = []
        self._depth = 0
        self._current = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
            return
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        # Nested anchors are invalid HTML but do appear; treat the outer one as
        # authoritative and ignore the inner.
        if self._current is None:
            self._current = {"href": unescape(href), "text": [], "trailing": []}
            self._depth = 1
        else:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if tag != "a" or self._current is None:
            return
        self._depth -= 1
        if self._depth <= 0:
            self.anchors.append(self._current)
            self._current = None
            self._depth = 0

    def handle_data(self, data):
        if self._skip:
            return
        text = data.strip()
        if not text:
            return
        if self._current is not None:
            self._current["text"].append(text)
            return
        # Text outside any anchor: attach it to the most recent one, up to the
        # trailing budget, as context for company and location.
        if self.anchors:
            last = self.anchors[-1]
            if sum(len(t) for t in last["trailing"]) < self.TRAILING_CHARS:
                last["trailing"].append(text)

    def results(self):
        collected = []
        for anchor in self.anchors:
            collected.append({
                "href": anchor["href"],
                "text": " ".join(anchor["text"]).strip(),
                "trailing": " ".join(anchor["trailing"]).strip(),
            })
        return collected


def collect_anchors(html_text):
    """Anchors in document order, each with its text and trailing context."""
    parser = AnchorCollector()
    try:
        parser.feed(html_text or "")
        parser.close()
    except Exception:  # malformed markup should not take the pipeline down
        return parser.results()
    return parser.results()


def strip_tags(html_text):
    """Rough plain-text rendering, for parsers working on the text half."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html_text or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|td|h[1-6]|li)\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


#: Boilerplate that shows up in the company/location slot and never belongs
#: in an identity key.
NOISE_PHRASES = re.compile(
    r"""^(?:
        view\s+job | see\s+all | apply\s+now | actively\s+recruiting
        | easy\s+apply | be\s+an\s+early\s+applicant | promoted
        | \d+\s+(?:new|other)\s+jobs? | unsubscribe | view\s+in\s+browser
        | \d+\s+(?:connection|alumni|school\s+alum)s?\s+work\s+here
    )\b""",
    re.VERBOSE | re.IGNORECASE,
)


def looks_like_noise(text):
    return bool(text and NOISE_PHRASES.match(text.strip()))


def split_company_location(text):
    """Split a "Company - City, ST" fragment into its two halves.

    Boards use an en dash, a hyphen, or a bullet, and sometimes reverse the
    order. Returns `(company, location)` with either side possibly None.
    """
    if not text:
        return None, None
    cleaned = " ".join(text.split())
    parts = re.split(r"\s+[–—·|-]\s+", cleaned, maxsplit=1)
    if len(parts) == 2:
        left, right = (p.strip() for p in parts)
        # A location almost always carries a comma or a remote marker; a
        # company rarely does. Use that to decide which side is which.
        if re.search(r",|\bremote\b|\bhybrid\b|\bon-?site\b", left, re.IGNORECASE) \
                and not re.search(r",|\bremote\b", right, re.IGNORECASE):
            return (right or None), (left or None)
        return (left or None), (right or None)
    return cleaned or None, None
