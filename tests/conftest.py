"""Pytest configuration.

Only the plugin registration lives here so it applies to the whole session. The
NiceGUI page fixtures are defined in tests/test_web_pages.py instead, because
they must not be pulled into the unittest-based backend tests.

The user plugin is loaded rather than nicegui.testing.plugin: the latter also
brings in the Screen fixtures, which need Selenium and a real browser.
"""

pytest_plugins = ["nicegui.testing.user_plugin"]
