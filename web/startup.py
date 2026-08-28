"""Wiring the background pipeline into the NiceGUI app lifecycle.

Kept out of `web/state.py` so importing shared state stays cheap for tests and
the CLI, neither of which wants a scheduler. `app.py` calls
`register_background_tasks()` before `ui.run`, and NiceGUI starts the task once
its event loop exists - creating the asyncio task any earlier would attach it to
no loop at all.
"""

import logging
import os

from pipeline.orchestrator import PipelineCycle
from pipeline.scheduler import DEFAULT_INTERVAL_SECONDS, PipelineScheduler
from web.state import get_state

log = logging.getLogger(__name__)


def poll_interval():
    """
    Summary:
        Read the background poll interval from the environment.

    Returns:
        int: `JOB_BUILDER_POLL_SECONDS` parsed as an integer, or
            `DEFAULT_INTERVAL_SECONDS` when it is unset or not a valid
            integer.

    Note:
        Never raises on a bad value - falls back and logs a warning instead,
        since a malformed environment variable should degrade rather than
        crash startup.
    """
    raw = os.environ.get("JOB_BUILDER_POLL_SECONDS")
    try:
        return int(raw) if raw else DEFAULT_INTERVAL_SECONDS
    except ValueError:
        log.warning("JOB_BUILDER_POLL_SECONDS=%r is not a number; using %ds",
                    raw, DEFAULT_INTERVAL_SECONDS)
        return DEFAULT_INTERVAL_SECONDS


def confidence_threshold():
    """
    Summary:
        Read the Groq auto-apply confidence threshold used to build the
        pipeline cycle.

    Returns:
        float: The configured threshold. See
            `clients.llm_client.confidence_threshold`.

    Note:
        Imported locally rather than at module level to avoid pulling
        `clients.llm_client` into every import of this module, including the
        CLI and test paths that never build a pipeline.
    """
    from clients.llm_client import confidence_threshold as groq_threshold

    return groq_threshold()


def build_pipeline(state=None):
    """Construct the cycle and scheduler and hang them off the shared state.

    Summary:
        Build (or reuse) the pipeline cycle and scheduler for a state object.

    Parameters:
        state (AppState | None): The state to build onto. None uses the
            process-wide shared state from `get_state()`.

    Returns:
        PipelineScheduler: The scheduler, ready to `start()`.

    Note:
        Idempotent - `state.pipeline` and `state.scheduler` are only built
        once and reused on subsequent calls, since NiceGUI can call startup
        wiring more than once in some reload scenarios.
    """
    state = state or get_state()
    if state.pipeline is None:
        # The pipeline draws from the same pool Settings displays, so the
        # budget and cooldowns shown are the ones actually in force.
        from pipeline.relevance import configured_threshold

        state.pipeline = PipelineCycle(
            state.store, state.mail,
            client_factory=lambda: state.pool,
            threshold=confidence_threshold(),
            # Was never passed, so the hardcoded default was the only value
            # this could ever have. It no longer gates spend - generation is a
            # click - but it does decide what the to-apply list shows first,
            # which is worth being able to change.
            relevance_threshold=configured_threshold(state.store),
        )
    if state.scheduler is None:
        state.scheduler = PipelineScheduler(state.pipeline, poll_interval())
    return state.scheduler


def register_background_tasks():
    """Start the poller when NiceGUI's event loop comes up.

    Summary:
        Register NiceGUI startup and shutdown hooks that start and stop the
        background pipeline scheduler.

    Note:
        Must be called before `ui.run`. NiceGUI does not create the
        asyncio event loop until `ui.run` starts it, so building the
        scheduler any earlier would attach its task to no loop at all.
    """
    from nicegui import app as nicegui_app

    @nicegui_app.on_startup
    async def _start():
        """
        Summary:
            Build the pipeline and start the scheduler once the event loop is
            running.
        """
        scheduler = build_pipeline()
        scheduler.start()

    @nicegui_app.on_shutdown
    async def _stop():
        """
        Summary:
            Stop the scheduler cleanly on app shutdown, if one was started.
        """
        state = get_state()
        if state.scheduler is not None:
            await state.scheduler.stop()
