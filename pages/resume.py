"""Resume & Experiences page: stored experience text for future resume tooling."""

from pages.text_storage import TextStoragePage


class ResumePage(TextStoragePage):
    name = "resume"
    title = "Resume & Experiences"
    subtitle = (
        "Store experience bullets, project details, resume notes, and CV context for future "
        "resume builder work."
    )
    storage_key = "resume_text"
