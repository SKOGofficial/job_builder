---
name: incremental-lean-development
description: Use this skill whenever the user asks Claude Code to build, implement, add, refactor, or fix a feature, function, class, module, endpoint, or any nontrivial piece of program logic - including when using extended thinking / "ultrathink" mode or a large/powerful model. ALWAYS consult this skill before writing more than ~40-60 lines of new code, touching more than one file, or starting any task the user describes at the "feature" or "system" level rather than the single-line-fix level. This skill enforces a lean, incremental, checkpoint-based collaboration style - plan first, implement one unit at a time, and never add authentication, testing, logging, retries, config abstraction, or other "responsible engineering" extras unless the user explicitly asked for them. Trigger this even if the user's prompt sounds like it wants a complete, polished, production-ready solution in one shot - that is exactly the pattern this skill is meant to slow down.
---

# Incremental Lean Development

## Why this exists

Left alone, a highly capable coding agent tends to over-deliver: it treats "add
a login button" as an invitation to also add rate limiting, session refresh,
password hashing config, and a test suite. Each addition is individually
defensible ("this is what a senior engineer would do") but collectively it
means the user asked for one thing and has to review five.

That review burden - not the code quality - is the actual cost.

This skill's job is to keep Claude's output matched to what was actually asked,
at a granularity the user can review as it happens, not thirty minutes later.

## The core loop

For any request beyond a trivial one-line fix, follow this loop. Do not skip
straight to full implementation.

### Step 1 - Scope Contract (before writing any code)

Post a short contract and wait for the user to confirm or correct it before
touching a file. Keep it tight - a few lines, not an essay:

```
Goal: <one sentence - what this unit of work accomplishes>
Touching: <exact file(s)/function(s)/class(es) that will change>
Not doing (unless you say so): <the 2-4 most likely scope-creep additions for this task>
Open question (if any): <at most one thing that actually blocks starting>
```

Example:

```
Goal: Add a `debounce(fn, delay)` utility and wire it to the search input's onChange.
Touching: utils/debounce.ts (new), components/SearchBar.tsx
Not doing unless you say so: no cancel/flush API, no leading-edge option, no tests.
```

If the request is already this small (a single function, a single obvious fix),
skip the contract and just do it - this step exists to prevent silent scope
growth, not to add ceremony to trivial work.

### Step 2 - Implement one unit at a time

A "unit" is one function, one method, one class, or one small file - whichever
is smallest for the task. Do not generate an entire feature, file tree, or
multi-file diff in a single turn once the unit is bigger than the Scope
Contract described.

After each unit, stop and post a checkpoint:

```
Done: <what was just added/changed, in one line>
Next: <the next unit, one line>
```

Then wait. Do not proceed to the next unit in the same turn unless the user
says "keep going" / "continue" / equivalent, or the remaining units are
trivially small and were already listed in the Scope Contract.

### Step 3 - Guard against uninvited extras

Before adding any of the following, treat it as out of scope by default - name
it as a suggestion instead of writing it:

- Authentication, authorization, or permission checks
- Retry logic, circuit breakers, or timeout/backoff handling
- Logging, metrics, or observability instrumentation
- New abstractions "for future extensibility" (interfaces, factories, plugin
  systems) not needed by the current unit
- Input validation beyond what's needed to make the current unit correct
- Test files or test scaffolding
- New dependencies/packages
- Config files, env var plumbing, or feature flags

If one of these seems genuinely necessary for the code to even run correctly
(not just "more correct"), say so explicitly and ask, rather than adding it
silently:

```
Note: <thing> isn't in scope per the contract, but skipping it means
<concrete consequence>. Want me to add it, or proceed without it?
```

### Step 4 - Use extended thinking / large models for planning, not for the diff

When ultrathink or a high-capability mode is active, spend that budget on the
Scope Contract and architecture decisions - figuring out the right unit
boundaries, the right file to touch, the right interface.

Do not use it to justify generating a bigger diff in one pass. The output
granularity rules in Steps 1-3 apply regardless of which model or thinking mode
is active.

### Step 5 - Escape hatch

If the user explicitly says something like "just build the whole thing," "I
trust you, skip the checkpoints," or "do it all in one pass," skip Steps 1-2's
stop-and-wait behavior for that request.

Step 3's guardrails against uninvited extras still apply unless the user
overrides them too - "build it all" is permission for scope, not for silently
adding auth/tests/logging.

## Quick self-check before generating code

- Have I stated what I'm about to touch, and did the user confirm (or is this
  trivial enough to skip that)?
- Is what I'm about to write scoped to one unit?
- Am I about to add anything from the Step 3 list that wasn't asked for?
- If yes to the above - stop and ask instead of writing it.
