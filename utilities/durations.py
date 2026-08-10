"""Spelling a wait in words a person can act on.

Every place that reports "retrying in about N" is quoting a provider's
retry-after, and those span four orders of magnitude: a pacing gap is seconds,
a minute-scoped 429 is a minute or two, and a daily ceiling is the rest of the
day. One unit cannot carry that. "in about 86400s" is technically correct and
tells the reader nothing, which is how it read before this module existed.

Deliberately coarse. This is a hint about when to look again, not a countdown,
so a single rounded unit is the whole vocabulary.
"""


def spell_duration(seconds):
    """
    Summary:
        Render a wait in seconds as a short phrase in one rounded unit.

    Parameters:
        seconds (float | int | None): The wait. None, a non-numeric value, or
            anything non-positive is treated as "no known wait".

    Returns:
        str: For example ``45s``, ``4m``, or ``24h``. Empty string when there
            is no wait to report, so the caller can drop the phrase entirely
            rather than print "in about 0s".
    """
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    if total < 90:
        return f"{total}s"
    if total < 90 * 60:
        return f"{round(total / 60)}m"
    return f"{round(total / 3600)}h"
