"""Route registration.

Importing this module registers every page with NiceGUI. Adding a page means
creating a module here and importing it below.
"""

from web.pages import (  # noqa: F401  (imported for the @ui.page side effect)
    add_application,
    dashboard,
    email_matches,
    experiences,
    jobs,
    leads,
    referrals,
    settings,
    text_storage,
)

__all__ = [
    "jobs",
    "add_application",
    "dashboard",
    "leads",
    "referrals",
    "email_matches",
    "experiences",
    "settings",
    "text_storage",
]
