"""Board parser registry.

Same pattern `web/pages/` uses: add a module, register it here, and nothing
else needs editing. A parser is a module exposing `matches(message)` and
`parse(message)`.

Order matters only in that the first parser claiming a message wins, so keep
specific boards ahead of anything broad.
"""

import logging

from clients.llm_client import GroqRateLimited
from clients.providers.base import ProviderUnavailable
from pipeline.parsers import generic, indeed, linkedin
from pipeline.parsers.base import Posting, collect_anchors, strip_tags

log = logging.getLogger(__name__)

#: Deterministic parsers, tried in order.
PARSERS = (linkedin, indeed)

__all__ = ["Posting", "collect_anchors", "strip_tags", "parse_alert",
           "parser_for", "PARSERS"]


def parser_for(message):
    """The first registered parser that claims this message, or None."""
    for parser in PARSERS:
        try:
            if parser.matches(message):
                return parser
        except Exception:
            log.exception("Parser %s failed its matches() check", parser.__name__)
    return None


def parse_alert(message, client=None):
    """Turn one job-alert email into postings.

    Deterministic first: a board parser gets the URL, the board job ID, and
    usually the title exactly right, for free and reproducibly. The LLM is only
    used where that leaves gaps, or where no parser recognises the sender.

    `client` is a `GroqClient`. Without one this degrades to whatever the
    deterministic parsers found, which is the correct behaviour when the model
    is unconfigured - fewer complete leads, never wrong ones.
    """
    parser = parser_for(message)

    if parser is not None:
        try:
            postings = parser.parse(message)
        except Exception:
            log.exception("Parser %s raised; falling back to the model",
                          parser.__name__)
            postings = []

        if postings:
            if client is not None and any(not p.company for p in postings):
                try:
                    postings = generic.complete(message, postings, client)
                except (GroqRateLimited, ProviderUnavailable):
                    # Not a parse failure - see the note on the outer handler.
                    raise
                except Exception:
                    log.exception("Gap-filling failed; keeping partial postings")
            return [p for p in postings if p.title and p.company]

    # No parser, or the parser found nothing in an email the classifier already
    # called an alert - that mismatch usually means a format change, so let the
    # model try rather than silently dropping the leads.
    if client is None:
        return []
    try:
        postings = generic.extract(message, client)
    except (GroqRateLimited, ProviderUnavailable):
        # Neither is a parse failure, and swallowing them here was actively
        # misleading: the alert got logged as unparseable and produced no
        # leads, so the handler read "this digest contains nothing".
        #
        # For a rate limit that only wasted a cycle. For a provider failure it
        # destroyed data - the handler stamped `handled_at` and permanently
        # retired 70 real alerts while a decommissioned model name in `.env`
        # returned 404 on every call. Letting both reach the handler is what
        # lets the batch stop cleanly and resume once the provider is back.
        raise
    except Exception:
        # Still swallowed: a genuine parse bug is specific to this message, and
        # blocking the queue on it would be a worse failure than dropping it.
        log.exception("Model extraction failed for %s",
                      message.get("gmail_message_id"))
        return []
    return [p for p in postings if p.title and p.company]
