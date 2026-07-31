"""Credential store access that degrades gracefully when there is no backend.

keyring raises NoKeyringError on any machine without a usable credential store:
a bare Linux CI runner, a headless server, a desktop with no secret service
running. That is a normal state, not a failure. There is simply no stored
secret, so reads report nothing and the caller falls back to configuration or
reports "not connected".

Writes deliberately still raise. Silently failing to store a credential would
leave the user believing a secret was saved when it was not, which is worse
than an error message.
"""

try:
    import keyring
    from keyring.errors import KeyringError

    KEYRING_INSTALLED = True
except ImportError:  # pragma: no cover - exercised only without the package
    keyring = None
    KeyringError = Exception
    KEYRING_INSTALLED = False

#: Used only to probe whether a backend answers at all.
PROBE_SERVICE = "job_builder_probe"
PROBE_USERNAME = "probe"


class CredentialStoreUnavailable(Exception):
    """Raised when a secret cannot be written because no backend is usable."""


def backend_available():
    """True when a credential store is present and answering.

    Probing with a read is the reliable check: the package can be installed
    while no backend is configured, and that only surfaces on the first call.
    """
    if keyring is None:
        return False
    try:
        keyring.get_password(PROBE_SERVICE, PROBE_USERNAME)
    except KeyringError:
        return False
    except Exception:
        return False
    return True


def read_secret(service, username):
    """Return a stored secret, or None when none is available.

    A locked or missing store is reported as "no secret" rather than raised.
    The caller's fallback path is the same either way, and a read that throws
    would take down whatever page happened to be rendering.
    """
    if keyring is None:
        return None
    try:
        return keyring.get_password(service, username)
    except KeyringError:
        return None


def write_secret(service, username, value):
    """Store a secret, raising if that is not possible."""
    if keyring is None:
        raise CredentialStoreUnavailable(
            "The keyring package is not installed, so secrets cannot be stored."
        )
    try:
        keyring.set_password(service, username, value)
    except KeyringError as exc:
        raise CredentialStoreUnavailable(
            "No usable credential store was found on this machine, so the secret "
            "was not saved. On Windows this should be Credential Manager; on Linux "
            "it needs a secret service such as gnome-keyring."
        ) from exc


def delete_secret(service, username):
    """Remove a stored secret. Returns whether anything was deleted."""
    if keyring is None:
        return False
    try:
        if keyring.get_password(service, username) is None:
            return False
        keyring.delete_password(service, username)
    except KeyringError:
        return False
    return True
