"""The mailbox ingest pipeline.

Sits above `clients/` (which owns provider detail) and `utilities/` (which owns
persistence), and below `web/` (which owns presentation). Nothing here imports
a UI framework, so the whole pipeline runs from the CLI as well as the app.

Flow, one module per stage:

    sync        pull message IDs and headers from Gmail
    rough_filter    drop mail that is obviously not from a board or company
    router      classify what survives: alert | update | acknowledgement | irrelevant
    resolver    work out which job identity a message is about
    alerts      turn a job-alert digest into leads
    updates     apply a status change to a job
    acknowledgements  move a lead onto the applied list

`orchestrator` runs the stages in order; `scheduler` decides when.
"""
