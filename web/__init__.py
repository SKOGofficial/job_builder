"""NiceGUI front end for the Job Board Tracker.

The UI is a thin layer. Everything it shows comes from `utilities.store`, and
every external call goes through `clients.gmail_client` or `clients.llm_client`,
neither of which imports anything from here. That separation is what let the
Tkinter UI be swapped out without touching the Gmail or Groq work.
"""
