# Claude Code Instructions

Please refer to the primary agent handoff instructions located in [.agents/AGENTS.md](../.agents/AGENTS.md).

## Key Guidelines

- Read [.agents/AGENTS.md](../.agents/AGENTS.md), `README.md`, [.agents/IMPLEMENTATION_PLAN.md](../.agents/IMPLEMENTATION_PLAN.md), [.agents/CODEX.md](../.agents/CODEX.md).
- **Follow git and CI management practices** defined in [.agents/AGENTS.md](../.agents/AGENTS.md#git-and-ci-management):
  - Create feature branches from main, not direct commits
  - Make intermittent commits for each phase of work
  - Automatically analyze and fix CI failures
- Do not modify or remove `job_applications.sqlite3`.
- Do not introduce networked services without approval.
- Keep code organized into single-responsibility modules under relevant directories (e.g. `pages/`), keeping `app.py` as an orchestrator.
