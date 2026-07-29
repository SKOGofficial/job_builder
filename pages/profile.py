"""Profile page: local contact and job search context."""

from pages.text_storage import TextStoragePage


class ProfilePage(TextStoragePage):
    name = "profile"
    title = "Profile"
    subtitle = "Store contact details, target roles, location preferences, and search notes."
    storage_key = "profile_text"
