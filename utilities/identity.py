"""Job identity: deriving a stable key from (title, company, location).

A posting URL is not a good identity. LinkedIn, Indeed, and a company's own
portal each hand out a different URL for the same role, and alert emails carry
tracking wrappers that change per send. What actually identifies a job is the
role itself, so the key is derived from the normalized triple instead.

Two rules shape everything here:

- **Under-merging beats over-merging.** Two rows for one job is untidy. One row
  for two jobs silently destroys the application history of whichever lost, and
  the user has no way to notice. So normalization only collapses spelling
  variants, never meaning: "Sr." becomes "senior" rather than being dropped,
  and "Engineer II" keeps its level.
- **Location is optional.** Alert emails usually carry it, older rows in this
  database never did, and a company reply often omits it. A key computed
  without location is a different key, so `identity_scheme` records which was
  used and `candidate_keys` produces both for lookup.

This module holds no SQLite and no UI so it can be imported and tested on its
own. `company_slug` and `COMPANY_SUFFIXES` moved here from `gmail_client`,
which still imports them for Gmail matching; `store.py` needs them too and must
not import from `clients/`.
"""

import hashlib
import re

#: Dropped from a company name before matching. A company is the same company
#: whether or not the sender spelled out "Limited".
COMPANY_SUFFIXES = {
    "inc", "inc.", "llc", "l.l.c.", "ltd", "ltd.", "limited", "corp", "corp.",
    "corporation", "co", "co.", "company", "plc", "gmbh", "ag", "sa", "nv", "bv",
    "group", "holdings", "technologies", "technology", "labs", "systems",
}

#: Spelling variants that mean the same seniority. These are normalized, never
#: dropped - a senior role and a non-senior role at the same company are
#: different jobs and must not collapse onto one key.
SENIORITY_SYNONYMS = {
    "sr": "senior",
    "snr": "senior",
    "jr": "junior",
    "jnr": "junior",
    "mgr": "manager",
    "eng": "engineer",
    "dev": "developer",
    "swe": "software engineer",
    "intern": "intern",
}

#: Trailing level markers, normalized to digits so "Engineer II" and
#: "Engineer 2" agree. Kept in the key, because level is part of the role.
ROMAN_LEVELS = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5"}

#: Noise that appears in titles from job boards and carries no role meaning.
TITLE_NOISE = re.compile(
    r"""
    \( \s* (?:m\s*/\s*f\s*/\s*[dx]|all\s+genders?|remote|hybrid|on[-\s]?site)
        \s* \)          # "(m/f/d)", "(Remote)" and friends
    | \b(?:job\s*)?(?:req(?:uisition)?|id)\s*[-:#]?\s*\w*\d\w*  # "REQ-12345", "req 4471"
    | \#\s*\d+                                                  # "#12345"
    """,
    re.VERBOSE | re.IGNORECASE,
)

#: Everything in this family means "not tied to an office".
REMOTE_MARKERS = re.compile(
    r"\b(?:fully\s+remote|100%\s*remote|remote|work\s+from\s+home|wfh|telecommute)\b",
    re.IGNORECASE,
)

US_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
    "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne",
    "nevada": "nv", "new hampshire": "nh", "new jersey": "nj",
    "new mexico": "nm", "new york": "ny", "north carolina": "nc",
    "north dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or",
    "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa",
    "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "district of columbia": "dc",
}

#: Countries and regions that show up as the whole location on remote postings.
REGION_ALIASES = {
    "united states": "us",
    "united states of america": "us",
    "usa": "us",
    "u.s.": "us",
    "u.s.a.": "us",
    "america": "us",
    "united kingdom": "uk",
    "u.k.": "uk",
    "great britain": "uk",
    "canada": "ca-country",
}

#: How the key was derived. Stored per row so a lookup knows which keys to try.
SCHEME_TITLE_COMPANY = "tc"
SCHEME_TITLE_COMPANY_LOCATION = "tcl"


def _collapse(text):
    """Lowercase, strip punctuation to spaces, collapse runs of whitespace."""
    text = re.sub(r"[^\w\s-]", " ", (text or "").lower())
    return " ".join(text.split())


def normalize_title(title):
    """Reduce a job title to a comparable form.

    Collapses spelling ("Sr." -> "senior", "SWE" -> "software engineer") and
    strips board noise (requisition IDs, "(m/f/d)", "(Remote)"). Deliberately
    keeps seniority and level: "Engineer II" and "Engineer III" must not agree.
    """
    text = TITLE_NOISE.sub(" ", title or "")
    text = _collapse(text)
    if not text:
        return ""
    words = []
    for word in text.split():
        bare = word.strip("-")
        if not bare:
            continue
        if bare in SENIORITY_SYNONYMS:
            words.extend(SENIORITY_SYNONYMS[bare].split())
        elif bare in ROMAN_LEVELS:
            words.append(ROMAN_LEVELS[bare])
        else:
            words.append(bare)
    return " ".join(words)


def company_slug(company):
    """Reduce a company name to a lowercase token used for matching.

    Moved from `clients/gmail_client.py`. Joining rather than space-separating
    is deliberate: it lets the Gmail matcher test the slug against a sender
    domain, where "acme corp" appears as "acmecorp".
    """
    cleaned = re.sub(r"[^\w\s-]", " ", (company or "").lower())
    words = [w for w in cleaned.split() if w and w not in COMPANY_SUFFIXES]
    return "".join(words)


def normalize_company(company):
    """Company form used inside an identity key. Alias of `company_slug`."""
    return company_slug(company)


def normalize_location(location):
    """Reduce a location to a comparable form.

    Handles the two variations that actually cause misses: the remote family
    ("Remote", "Fully Remote", "100% Remote", "Work From Home" all agree) and
    spelled-out state names ("California" -> "ca").

    A hybrid posting keeps both parts, so "San Francisco, CA (Remote)" becomes
    "san francisco ca|remote" - it matches neither a pure office posting nor a
    pure remote one, which is correct. They are different arrangements.
    """
    raw = location or ""
    is_remote = bool(REMOTE_MARKERS.search(raw))
    without_remote = REMOTE_MARKERS.sub(" ", raw)

    # Split on separators first. `_collapse` strips commas, so splitting after
    # it would merge "San Francisco, California" into one token and the state
    # lookup below would never fire.
    parts = []
    for part in re.split(r"[,/|]|\s+-\s+|\(|\)", without_remote):
        part = _collapse(part)
        if not part:
            continue
        parts.append(REGION_ALIASES.get(part, US_STATES.get(part, part)))

    text = " ".join(parts)
    if is_remote:
        return f"{text}|remote" if text else "remote"
    return text


def identity_scheme(location):
    """Which key scheme applies for this location value."""
    return SCHEME_TITLE_COMPANY_LOCATION if normalize_location(location) else SCHEME_TITLE_COMPANY


def identity_key(title, company, location=None):
    """Stable 12-hex identifier for a role.

    Same shape as the URL-derived `url_hash` in `store.py`, so IDs stay
    visually consistent across the two generations of the schema.

    Location is folded in only when it normalizes to something. A key computed
    without it is a *different* key, not a wildcard - use `candidate_keys` when
    looking a job up rather than assuming one form.
    """
    normalized_location = normalize_location(location)
    fields = [normalize_title(title), normalize_company(company)]
    if normalized_location:
        fields.append(normalized_location)
    joined = "|".join(fields)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12].upper()


def candidate_keys(title, company, location=None):
    """Keys to try when resolving a message or lead to an existing row.

    Ordered most to least specific. When a location is known this yields the
    location-qualified key first and the bare title+company key second, so a
    row stored before locations existed - every row in a pre-migration
    database - is still reachable.
    """
    keys = []
    if normalize_location(location):
        keys.append(identity_key(title, company, location))
    bare = identity_key(title, company, None)
    if bare not in keys:
        keys.append(bare)
    return keys
