"""Profile and Resume: free-text pages backed by the profile key/value table."""

from nicegui import ui

from web.shell import card, page_shell
from web.state import get_state


def text_storage_page(title, subtitle, storage_key):
    store = get_state().store
    with page_shell(title, subtitle, active=""):
        with card():
            text = ui.textarea(
                value=store.get_profile_value(storage_key, "")
            ).props("outlined autogrow").classes("w-full min-h-[24rem]")

            def save():
                store.save_profile_value(storage_key, (text.value or "").strip())
                ui.notify("Your information was saved locally.", type="positive")

            ui.button("Save", on_click=save).props("unelevated no-caps").classes("self-end")


@ui.page("/profile")
def profile_page():
    text_storage_page(
        "Profile",
        "Store contact details, target roles, location preferences, and search notes.",
        "profile_text",
    )


@ui.page("/resume")
def resume_page():
    text_storage_page(
        "Resume & Experiences",
        "Store experience bullets, project details, resume notes, and CV context for future "
        "resume builder work.",
        "resume_text",
    )
