"""Model providers, and the shared vocabulary they speak.

One module per provider, each owning every detail of talking to it and leaking
none of it upward - the same shape `clients/gmail_client.py` follows for Gmail.
`base.py` holds what they share: the exceptions, the pacing, and the token
arithmetic.

Concrete providers are imported lazily inside `build_provider` rather than at
module level. Importing this package must stay cheap and must not fail because
one provider's optional dependency is missing, since `clients/llm_client.py`
imports `base` on every start.
"""

from clients.providers.base import (
    CHARS_PER_TOKEN,
    DEFAULT_REQUESTS_PER_MINUTE,
    ESTIMATED_TOKENS_PER_CALL,
    TOKENS_PER_MINUTE,
    Pacer,
    ProviderBudgetExhausted,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderRequestTooLarge,
    estimate_tokens,
    retry_after_seconds,
)

#: Provider name -> the module that owns it. Names are persisted in
#: `provider_settings`, so renaming one orphans a user's saved routing.
PROVIDER_MODULES = {
    "groq": "clients.llm_client",
    "gemini": "clients.providers.gemini",
    "anthropic": "clients.research_client",
}

__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_REQUESTS_PER_MINUTE",
    "ESTIMATED_TOKENS_PER_CALL",
    "PROVIDER_MODULES",
    "TOKENS_PER_MINUTE",
    "Pacer",
    "ProviderBudgetExhausted",
    "ProviderNotConfigured",
    "ProviderRateLimited",
    "ProviderRequestTooLarge",
    "estimate_tokens",
    "provider_module",
    "retry_after_seconds",
]


def provider_module(name):
    """Import and return the module owning a provider.

    Summary:
        Resolve a provider name to its implementing module.

    Parameters:
        name (str): A key of `PROVIDER_MODULES`.

    Returns:
        module: The provider's module.

    Raises:
        ProviderNotConfigured: When the name is unknown, or when the module
            cannot be imported because an optional dependency is missing. Both
            are reported the same way because both mean the same thing to a
            caller: this provider cannot serve a request right now.
    """
    import importlib

    path = PROVIDER_MODULES.get(name)
    if path is None:
        raise ProviderNotConfigured(f"Unknown model provider: {name!r}")
    try:
        return importlib.import_module(path)
    except ImportError as exc:
        raise ProviderNotConfigured(
            f"The {name} provider could not be loaded: {exc}"
        ) from exc
