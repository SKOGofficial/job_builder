"""Shared base for the free-text storage pages backed by the profile table."""

import tkinter as tk
from tkinter import messagebox, ttk

from pages.base import BasePage


class TextStoragePage(BasePage):
    #: Key in the profile table that this page reads and writes.
    storage_key = ""

    def render(self):
        ttk.Label(self.content, text=self.title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(self.content, text=self.subtitle, style="Muted.TLabel").pack(
            anchor="w", pady=(4, 14)
        )
        card = self.card(self.content)
        card.pack(fill="both", expand=True)
        text = tk.Text(
            card,
            wrap="word",
            bg=self.theme["surface"],
            fg=self.theme["text"],
            insertbackground=self.theme["text"],
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground=self.theme["border"],
            font=("Segoe UI", 10),
        )
        text.insert("1.0", self.store.get_profile_value(self.storage_key, ""))
        text.pack(fill="both", expand=True)
        ttk.Button(
            card,
            text="Save",
            style="Primary.TButton",
            command=lambda: self.save(text.get("1.0", "end").strip()),
        ).pack(anchor="e", pady=(14, 0))

    def save(self, value):
        self.store.save_profile_value(self.storage_key, value)
        messagebox.showinfo("Saved", "Your information was saved locally.")
