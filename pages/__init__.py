"""Page registry.

Adding a page means creating a module here and listing its class below. The app
builds its navigation and routing from PAGE_CLASSES, so nothing else needs to
change.
"""

from pages.add_application import AddApplicationPage
from pages.all_jobs import AllJobsPage
from pages.base import BasePage
from pages.dashboard import DashboardPage
from pages.email_matches import EmailMatchesPage
from pages.profile import ProfilePage
from pages.resume import ResumePage
from pages.settings import SettingsPage

PAGE_CLASSES = [
    AllJobsPage,
    AddApplicationPage,
    DashboardPage,
    SettingsPage,
    EmailMatchesPage,
    ProfilePage,
    ResumePage,
]

# Tabs across the top of the main area.
NAV_TABS = [
    ("All jobs", AllJobsPage.name),
    ("Add application", AddApplicationPage.name),
    ("Dashboard", DashboardPage.name),
]

# Entries in the hamburger drawer.
DRAWER_ENTRIES = [
    ("Settings", SettingsPage.name),
    ("Email matches", EmailMatchesPage.name),
    ("Profile", ProfilePage.name),
    ("Resume & Experiences", ResumePage.name),
]

__all__ = [
    "BasePage",
    "PAGE_CLASSES",
    "NAV_TABS",
    "DRAWER_ENTRIES",
    "AddApplicationPage",
    "AllJobsPage",
    "DashboardPage",
    "EmailMatchesPage",
    "ProfilePage",
    "ResumePage",
    "SettingsPage",
]
