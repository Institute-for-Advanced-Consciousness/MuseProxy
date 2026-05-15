"""Shared pytest fixtures.

The uv-managed Python's Tk install runs out of init handles after a few
``Tk()`` constructions in the same process. We therefore create ONE
App per pytest session and share it across every Tk-touching test
module via this session-scoped fixture. (Pattern lifted from
SuperChillsOddball.)
"""

from __future__ import annotations

import tkinter as tk

import pytest


@pytest.fixture(scope="session")
def shared_app():
    """One App instance for the whole pytest run. Skips on no-Tk environments."""
    try:
        from muse_proxy.app import App

        app = App()
    except tk.TclError as e:
        pytest.skip(f"Tk display not available: {e}", allow_module_level=False)
    yield app
    app.shutdown()
