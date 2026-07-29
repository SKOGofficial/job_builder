"""Placeholder for LLM orchestration.

This module is the counterpart to `gmail_client.py`. Where that one owns every
detail of talking to Gmail, this one is intended to own every detail of talking
to a language model, so the rest of the app never holds a prompt, an API key, or
a provider SDK call.

Nothing is implemented yet. The class below is an intentionally empty shell that
marks where this work belongs.

Anticipated responsibilities once it is built:

- Hold provider configuration and credentials, following the same rule as
  `gmail_client.py`: real secrets go to the OS credential store through
  `keyring`, and only non-sensitive configuration lives in `.env`.
- Wrap every model call behind named methods, so callers ask for an outcome
  ("summarise this posting") rather than assembling prompts themselves.
- Be optional in exactly the way Gmail is. The tracker must keep working as a
  local-only tool when the provider libraries are absent or unconfigured.

Likely uses in this app, none of which are built:

- Summarising a job posting into title, company, pay, and location.
- Classifying an email reply as rejection, interview invite, OA request, or
  offer, to improve on the current header-only matching in `gmail_client.py`.
- Drafting tailored resume bullets from the stored experience text.

Design constraint carried over from the Gmail work: anything a model infers is a
suggestion, not a fact. Model output must never write a job status or overwrite
stored user data without an explicit confirmation step, for the same reason the
email matcher only ever suggests. Model output is also untrusted input; it must
not be treated as instructions to act on.
"""


class LLMClient:
    """Empty placeholder. See the module docstring for intended scope."""

    pass
