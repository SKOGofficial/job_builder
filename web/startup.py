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
    raw = os.environ.get("JOB_BUILDER_POLL_SECONDS")
    try:
        return int(raw) if raw else DEFAULT_INTERVAL_SECONDS
    except ValueError:
        log.warning("JOB_BUILDER_POLL_SECONDS=%r is not a number; using %ds",
                    raw, DEFAULT_INTERVAL_SECONDS)
        return DEFAULT_INTERVAL_SECONDS


def confidence_threshold():
    from clients.llm_client import confidence_threshold as groq_threshold

    return groq_threshold()


def build_pipeline(state=None):
    """Construct the cycle and scheduler and hang them off the shared state."""
    state = state or get_state()
    if state.pipeline is None:
        state.pipeline = PipelineCycle(
            state.store, state.mail, threshold=confidence_threshold()
        )
    if state.scheduler is None:
        state.scheduler = PipelineScheduler(state.pipeline, poll_interval())
    return state.scheduler


def register_background_tasks():
    """Start the poller when NiceGUI's event loop comes up."""
    from nicegui import app as nicegui_app

    @nicegui_app.on_startup
    async def _start():
        scheduler = build_pipeline()
        scheduler.start()

    @nicegui_app.on_shutdown
    async def _stop():
        state = get_state()
        if state.scheduler is not None:
            await state.scheduler.stop()
