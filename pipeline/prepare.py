"""Getting leads to `ready` before the user opens the list.

The to-apply list is defined as application-ready: open it, click through, fill
in the form, send. No "generate" button in the middle. So preparation is
triggered by lead creation and runs in the background, moving a lead
`new -> preparing -> ready`.

Failures are visible rather than silent. A lead whose generation errored stays
out of `ready` and carries the reason, so the list never contains a row whose
"open resume" link is dead - which would be worse than the row not being there.

Ordering: score first (free), then research and render (expensive). That is the
gate from the plan, and it is what keeps the monthly bill proportional to the
roles actually worth pursuing.
"""

import logging

from clients.research_client import ResearchNotConfigured, SpendCeilingReached
from pipeline.generate import ArtifactBuilder
from pipeline.relevance import RelevanceScorer
from utilities.mailstore import LEAD_PREPARING, LEAD_READY

log = logging.getLogger(__name__)


class LeadPreparer:
    """Scores new leads, then prepares the ones worth preparing."""

    def __init__(self, store, mail, groq_client=None, research_client=None,
                 threshold=None, output_dir=None):
        self.store = store
        self.mail = mail
        self.scorer = RelevanceScorer(store, mail, groq_client,
                                      **({"threshold": threshold} if threshold
                                         is not None else {}))
        builder_kwargs = {"output_dir": output_dir} if output_dir else {}
        self.builder = ArtifactBuilder(store, mail, research_client,
                                       **builder_kwargs)

    def run(self, score_limit=50, prepare_limit=5):
        """One preparation pass.

        `prepare_limit` is small on purpose. Each prepared lead is a real spend
        and a slow call; a cycle does a few and the scheduler comes back, which
        keeps a burst of fifty new leads from turning into fifty simultaneous
        Opus requests.
        """
        scored = self.scorer.run(score_limit)
        prepared = failed = 0

        for lead in self.scorer.worth_preparing(prepare_limit):
            outcome = self.prepare(lead)
            if outcome is True:
                prepared += 1
            elif outcome is False:
                failed += 1
            else:
                # Budget exhausted or research unconfigured - stop rather than
                # marking every remaining lead as failed.
                break

        return {"scored": scored, "prepared": prepared, "failed": failed}

    def prepare(self, lead):
        """Build artifacts for one lead.

        Returns True on success, False on a failure specific to this lead, and
        None when the whole stage should stop (no budget, not configured).
        """
        self.mail.set_lead_status(lead["id"], LEAD_PREPARING)
        try:
            written = self.builder.build(lead)
        except SpendCeilingReached as exc:
            log.warning("Stopping preparation: %s", exc)
            self.mail.set_lead_status(lead["id"], lead["status"], str(exc))
            return None
        except ResearchNotConfigured as exc:
            log.info("Stopping preparation: %s", exc)
            self.mail.set_lead_status(lead["id"], lead["status"], str(exc))
            return None
        except Exception as exc:
            log.exception("Could not prepare lead %s", lead["id"])
            # Stays out of `ready`, carrying the reason, so the list never
            # offers a link that does not work.
            self.mail.set_lead_status(lead["id"], lead["status"], str(exc))
            return False

        self.mail.set_lead_status(lead["id"], LEAD_READY)
        log.info("Lead %s (%s at %s) is ready: %s", lead["id"], lead["title"],
                 lead["company"], ", ".join(sorted(written)))
        return True

    def prepare_now(self, lead_id):
        """Prepare one lead on demand, bypassing the relevance gate.

        The escape hatch for a lead the scorer rated too low. Without it, a
        threshold set slightly wrong makes a role permanently unreachable.
        """
        lead = self.mail.lead(lead_id)
        if lead is None:
            return False
        return self.prepare(lead) is True
