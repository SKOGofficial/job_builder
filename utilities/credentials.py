"""Credential storage that works on a desktop and on a headless server.

Two backends, chosen by `JOB_BUILDER_SECRETS`:

- `keyring` (default) - the OS credential store. Windows Credential Manager,
  gnome-keyring, macOS Keychain.
- `file` - a 0600 JSON file outside the repo, for a server with no secret
  service running. Opt-in on purpose: nobody should end up with a refresh
  token on disk without having chosen it.

keyring raises NoKeyringError on any machine without a usable credential store:
a bare Linux CI runner, a headless server, a desktop with no secret service
running. That is a normal state, not a failure. There is simply no stored
secret, so reads report nothing and the caller falls back to configuration or
reports "not connected".

Writes deliberately still raise. Silently failing to store a credential would
leave the user believing a secret was saved when it was not, which is worse
than an error message. `backend_available()` lets the UI disable a "save to
credential store" control rather than offering an action that will throw.
"""

import json
import logging
import os
import stat

log = logging.getLogger(__name__)

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

BACKEND_KEYRING = "keyring"
BACKEND_FILE = "file"


class CredentialStoreUnavailable(Exception):
    """Raised when a secret cannot be written because no backend is usable."""


def selected_backend():
    """Which backend this machine is configured to use."""
    choice = (os.environ.get("JOB_BUILDER_SECRETS") or BACKEND_KEYRING).strip().lower()
    return choice if choice in (BACKEND_KEYRING, BACKEND_FILE) else BACKEND_KEYRING


def file_store_path():
    """Where the file backend keeps secrets.

    Outside the repo by default, so a stray `git add -A` cannot commit it.
    """
    override = os.environ.get("JOB_BUILDER_SECRETS_PATH")
    if override:
        return os.path.expanduser(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "job_builder", "credentials.json")


def _read_file_store():
    path = file_store_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        log.warning("Credential file at %s is unreadable; treating as empty", path)
        return {}


def _write_file_store(data):
    path = file_store_path()
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        # Create with 0600 from the start rather than widening then narrowing,
        # so there is no window where the token is world-readable.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # Windows does not implement POSIX modes; the ACL inherited from
            # the user profile directory is the protection there.
            pass
    except OSError as exc:
        raise CredentialStoreUnavailable(
            f"Could not write the credential file at {path}: {exc}"
        ) from exc


def _file_key(service, username):
    return f"{service}:{username}"


def backend_available():
    """True when a credential store is present and answering.

    Probing with a read is the reliable check for keyring: the package can be
    installed while no backend is configured, and that only surfaces on the
    first call.
    """
    if selected_backend() == BACKEND_FILE:
        directory = os.path.dirname(file_store_path())
        try:
            os.makedirs(directory, exist_ok=True)
            return os.access(directory, os.W_OK)
        except OSError:
            return False
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
    if selected_backend() == BACKEND_FILE:
        return _read_file_store().get(_file_key(service, username))
    if keyring is None:
        return None
    try:
        return keyring.get_password(service, username)
    except KeyringError:
        return None


def write_secret(service, username, value):
    """Store a secret, raising if that is not possible."""
    if selected_backend() == BACKEND_FILE:
        data = _read_file_store()
        data[_file_key(service, username)] = value
        _write_file_store(data)
        return
    if keyring is None:
        raise CredentialStoreUnavailable(
            "The keyring package is not installed, so secrets cannot be stored. "
            "On a headless server, set JOB_BUILDER_SECRETS=file to use a 0600 "
            "file instead."
        )
    try:
        keyring.set_password(service, username, value)
    except KeyringError as exc:
        raise CredentialStoreUnavailable(
            "No usable credential store was found on this machine, so the secret "
            "was not saved. On Windows this should be Credential Manager; on Linux "
            "it needs a secret service such as gnome-keyring. On a headless "
            "server, set JOB_BUILDER_SECRETS=file to use a 0600 file instead."
        ) from exc


def delete_secret(service, username):
    """Remove a stored secret. Returns whether anything was deleted."""
    if selected_backend() == BACKEND_FILE:
        data = _read_file_store()
        if _file_key(service, username) not in data:
            return False
        del data[_file_key(service, username)]
        _write_file_store(data)
        return True
    if keyring is None:
        return False
    try:
        if keyring.get_password(service, username) is None:
            return False
        keyring.delete_password(service, username)
    except KeyringError:
        return False
    return True


def describe_backend():
    """One line for the Settings page, so the state is not a mystery."""
    backend = selected_backend()
    if backend == BACKEND_FILE:
        return f"File ({file_store_path()}, permissions 0600)"
    if not KEYRING_INSTALLED:
        return "Unavailable (keyring is not installed)"
    if not backend_available():
        return "Unavailable (no OS credential store is answering)"
    return "OS credential store"
