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

from clients.llm_client import GroqRateLimited
from clients.research_client import ResearchNotConfigured, SpendCeilingReached
from pipeline.generate import ArtifactBuilder
from pipeline.relevance import RelevanceScorer
from utilities.mailstore import LEAD_PREPARING, LEAD_READY, waiting_note

log = logging.getLogger(__name__)


def _never():
    """
    Summary:
        Stand in for `available_in` on a research client that does not have
        one - None, or an object injected by a test or a research factory.

    Returns:
        float: 0.0, meaning "no known wait", which leaves the behaviour of
            those paths exactly as it was.
    """
    return 0.0


class LeadPreparer:
    """Scores new leads, then prepares the ones worth preparing."""

    def __init__(self, store, mail, groq_client=None, research_client=None,
                 threshold=None, output_dir=None, executor=None,
                 letter_client=None):
        self.store = store
        self.mail = mail
        self.scorer = RelevanceScorer(store, mail, groq_client,
                                      executor=executor,
                                      **({"threshold": threshold} if threshold
                                         is not None else {}))
        builder_kwargs = {"output_dir": output_dir} if output_dir else {}
        self.builder = ArtifactBuilder(store, mail, research_client,
                                       executor=executor,
                                       letter_client=letter_client,
                                       **builder_kwargs)

    async def run(self, score_limit=50, prepare_limit=5):
        """One preparation pass.

        `prepare_limit` is small on purpose. Each prepared lead is a real spend
        and a slow call; a cycle does a few and the scheduler comes back, which
        keeps a burst of fifty new leads from turning into fifty simultaneous
        Opus requests.

        Summary:
            Score the backlog, then build artifacts for the leads that cleared
            the bar.

        Parameters:
            score_limit (int): Most leads to score in this pass.
            prepare_limit (int): Most leads to prepare in this pass.

        Returns:
            dict: Counts under `scored`, `prepared`, and `failed`. Scoring runs
                first and unconditionally: it is a different task on a
                different chain, and a research provider being out is no reason
                to leave the backlog unscored.
        """
        scored = await self.scorer.run(score_limit)
        prepared = failed = 0

        # Research routes to its own short chain, so it can be entirely blocked
        # while the pool as a whole looks healthy and the group skip in
        # `orchestrator._model_stages` sees nothing wrong. Asking first costs
        # nothing and saves a batch of leads each discovering the same wall.
        wait = getattr(self.builder.research_client, "available_in", _never)()
        if wait > 0:
            log.info("Lead preparation skipped: no provider is available for "
                     "research for about %ds; retrying next cycle.", int(wait))
            return {"scored": scored, "prepared": 0, "failed": 0}

        for lead in self.scorer.worth_preparing(prepare_limit):
            outcome = await self.prepare(lead)
            if outcome is True:
                prepared += 1
            elif outcome is False:
                failed += 1
            else:
                # Budget exhausted, rate limited, or research unconfigured -
                # stop rather than marking every remaining lead as failed.
                break

        return {"scored": scored, "prepared": prepared, "failed": failed}

    async def prepare(self, lead):
        """Build artifacts for one lead.

        Returns True on success, False on a failure specific to this lead, and
        None when the whole stage should stop (no budget, not configured, or
        rate limited - none of which the next lead would fare better against).

        Summary:
            Move one lead through `preparing` to `ready`, recording the reason
            on the lead when it cannot be prepared.

        Parameters:
            lead (Mapping): The lead row to prepare.

        Returns:
            bool | None: True when the lead is ready, False on a failure
                specific to this lead, and None when the whole stage should
                stop.
        """
        self.mail.set_lead_status(lead["id"], LEAD_PREPARING)
        try:
            written = await self.builder.build(lead)
        except SpendCeilingReached as exc:
            log.warning("Stopping preparation: %s", exc)
            self.mail.set_lead_status(lead["id"], lead["status"], str(exc))
            return None
        except ResearchNotConfigured as exc:
            log.info("Stopping preparation: %s", exc)
            self.mail.set_lead_status(lead["id"], lead["status"], str(exc))
            return None
        except GroqRateLimited as exc:
            # Routine, not a failure: the same pause every other model stage
            # reports as one line. Above the generic handler so it does not
            # become a traceback, and None rather than False so the rest of the
            # batch is not each marked failed finding the same wall.
            log.info("Lead preparation paused by the rate limit; retrying next "
                     "cycle, in about %ss", exc.retry_after)
            self.mail.set_lead_status(lead["id"], lead["status"],
                                      waiting_note(exc.retry_after))
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

    async def prepare_now(self, lead_id):
        """Prepare one lead on demand, bypassing the relevance gate.

        The escape hatch for a lead the scorer rated too low. Without it, a
        threshold set slightly wrong makes a role permanently unreachable.

        Summary:
            Prepare one lead by id regardless of its relevance score.

        Parameters:
            lead_id (int): The lead to prepare.

        Returns:
            bool: True when the lead reached `ready`, False when it does not
                exist or could not be prepared.
        """
        lead = self.mail.lead(lead_id)
        if lead is None:
            return False
        return await self.prepare(lead) is True
