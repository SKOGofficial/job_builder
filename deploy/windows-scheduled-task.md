# Draining the pipeline with the app closed (Windows)

The scheduler is an asyncio task created on NiceGUI startup. It exists only
while `app.py` is running, and there is no service, no cron, and no daemon
behind it. Close the window and nothing syncs, nothing is classified, and
nothing is extracted.

That is not hypothetical. This mailbox went from **21 August to 28 August**
with no cycle at all — six days of mail arriving and none of it looked at —
and the only symptom was a backlog that never went down.

On Linux, `deploy/job-builder.service` already solves this by keeping the app
running. On Windows the equivalent is a Scheduled Task calling the CLI, which
runs the same `PipelineCycle` the scheduler does.

## Register it

Run this once, in an **elevated** PowerShell, with the paths adjusted:

```powershell
$python = "C:\Users\SKOGo\Desktop\To_Move\CODE\job_builder\.venv\Scripts\python.exe"
$repo   = "C:\Users\SKOGo\Desktop\To_Move\CODE\job_builder"

$action  = New-ScheduledTaskAction -Execute $python -Argument "cli.py sync" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

Register-ScheduledTask -TaskName "job-builder-sync" -Action $action -Trigger $trigger -Settings $settings -Description "Drain the job_builder pipeline while the app is closed."
```

`-StartWhenAvailable` is the setting that matters on a laptop: it runs a missed
occurrence after the machine wakes, instead of silently skipping every trigger
that fired while it was asleep.

Every thirty minutes rather than the app's ten. The task pays process startup
each time, and the queues are bounded per cycle anyway — a tighter interval
buys throughput the per-cycle limits will not give it.

## Catching up after a gap

For a backlog rather than a routine drain, run cycles back to back until one
changes nothing:

```bash
.venv/Scripts/python.exe cli.py sync --until-empty
```

It stops when a cycle moves nothing, not when the queues read zero — a queue
can be legitimately non-empty, waiting out a rate limit, and looping until it
cleared would spin until the limit did. `--max-cycles` (default 20) is the
backstop.

## Checking it is working

```bash
.venv/Scripts/python.exe cli.py diagnostics
```

Queue depths, per-stage timings, and provider outcomes — the same numbers the
`/diagnostics` page shows, available with the app closed, which is exactly when
the question comes up. If `Stage timings` says nothing was recorded, no cycle
has run in the window and the task is not firing.

## Removing it

```powershell
Unregister-ScheduledTask -TaskName "job-builder-sync" -Confirm:$false
```

## Safe to run alongside the app

WAL mode plus a five-second busy timeout (`JobStore.configure_connection`) is
what makes concurrent access work rather than deadlock. If both the task and
the app run a cycle at once, one waits.
