"""Getting a lead to `ready`, when the user asks for it.

This module used to open by saying the to-apply list was application-ready with
"no 'generate' button in the middle": preparation was triggered by lead
creation and ran unattended for anything scoring above 0.45. That is no longer
true, and the reason is worth keeping.

A relevance score is a guess about whether a person wants a role. Spending a
research call and a letter on every lead that clears a threshold means the bill
is set by how well the model guesses, and the guess is wrong often enough that
most of what it produced was never opened - 363 leads, eleven documents, none
of them asked for. The score is a good way to *rank* a list. It is a poor way
to authorise spending.

So preparation is now pull, not push. Scoring stays automatic because it is
free and it is what makes the list worth reading. Research and letter-writing
happen when someone clicks Generate on a specific role, through `prepare_now`.

Failures are visible rather than silent. A lead whose generation errored stays
out of `ready` and carries the reason, so the list never contains a row whose
"open resume" link is dead - which would be worse than the row not being there.
"""

import logging

from clients.llm_client import GroqRateLimited
from clients.research_client import ResearchNotConfigured, SpendCeilingReached
from pipeline.generate import ArtifactBuilder
from pipeline.relevance import RelevanceScorer
from utilities.durations import spell_duration
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

    async def run(self, score_limit=50, prepare_limit=0):
        """One preparation pass.

        Summary:
            Score the backlog, and - only if asked - build artifacts for the
            leads that cleared the bar.

        Parameters:
            score_limit (int): Most leads to score in this pass.
            prepare_limit (int): Most leads to prepare in this pass. **Zero,
                the default, means score only.**

        Returns:
            dict: Counts under `scored`, `prepared`, and `failed`. Scoring runs
                first and unconditionally: it is a different task on a
                different chain, and a research provider being out is no reason
                to leave the backlog unscored.

        Note:
            The default is zero because the pipeline no longer generates
            documents on its own. Scoring is free and is what makes the list
            worth reading; research and letter-writing are the real spend and
            now happen only when a person asks for a specific role, through
            `prepare_now`.

            The parameter is kept rather than removed, so a caller that does
            want unattended preparation - a supervised catch-up run, a test -
            can still ask for it explicitly. What changed is the default, and
            the fact that nothing in the scheduled path passes anything else.
        """
        scored = await self.scorer.run(score_limit)
        prepared = failed = 0
        if not prepare_limit:
            return {"scored": scored, "prepared": 0, "failed": 0}

        # Research routes to its own short chain, so it can be entirely blocked
        # while the pool as a whole looks healthy and the group skip in
        # `orchestrator._model_stages` sees nothing wrong. Asking first costs
        # nothing and saves a batch of leads each discovering the same wall.
        wait = getattr(self.builder.research_client, "available_in", _never)()
        if wait > 0:
            log.info("Lead preparation skipped: no provider is available for "
                     "research for about %s; retrying next cycle.",
                     spell_duration(wait))
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
                     "cycle, in about %s", spell_duration(exc.retry_after))
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
