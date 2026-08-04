# Codex Instructions

Please refer to the primary operating guide located in [.agents/CODEX.md](../.agents/CODEX.md) and [.agents/AGENTS.md](../.agents/AGENTS.md).

## Key Guidelines

- Operating guide: [.agents/CODEX.md](../.agents/CODEX.md)
- Agent handoff: [.agents/AGENTS.md](../.agents/AGENTS.md)
- Implementation plan: [.agents/IMPLEMENTATION_PLAN.md](../.agents/IMPLEMENTATION_PLAN.md)
- Project log: [.agents/PROJECT_LOG.md](../.agents/PROJECT_LOG.md)
- For every new function or method, include a docstring or structured comment with a short `Summary` describing what the method does, plus `Parameters`, `Returns`, and `Raises`. Omit any of those three that would be empty rather than writing "None." - only `Summary` is unconditional. Keep any existing prose that explains *why* and add the fields beneath it. Full rule in [.agents/AGENTS.md](../.agents/AGENTS.md); worked example in `utilities/store.py`.
