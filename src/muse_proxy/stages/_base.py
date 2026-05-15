"""Base class for every stage frame.

Lives in its own module so stage modules can import it without creating
a circular dependency with ``app.py`` (which itself imports the stage
modules).

Each stage:
- Sets ``NAME`` (used as the key in ``App._frames`` and in marker
  payloads such as ``session_abort:pass=A:stage=<NAME>``).
- Overrides ``build()`` to create static widgets once at construction.
- Overrides ``on_enter(**payload)`` to (re)initialise state when shown.
- Overrides ``on_leave()`` to tear down any worker threads or timers.

Stages reach the rest of the app via ``self.app`` — the App owns
``outlet``, ``writer``, ``audio_scheduler``, ``ui_queue``, ``config``,
and the current pass label.
"""

from __future__ import annotations

import tkinter as tk
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..app import App


class StageFrame(tk.Frame):
    """Base for every stage in the flow."""

    NAME: str = "__abstract__"

    def __init__(self, app: App) -> None:
        super().__init__(app.root, bg=app.theme_bg)
        self.app = app
        self._wrappables: list[tuple[tk.Misc, float, int]] = []
        self.build()
        self.bind("<Configure>", self._on_stage_resize)

    # ----- subclass hooks -------------------------------------------------

    def build(self) -> None:
        """Create the static widgets. Called once at construction."""

    def on_enter(self, **payload: Any) -> None:
        """Called every time this frame is shown. ``payload`` is forwarded
        from ``App.show_stage(name, **payload)``.
        """

    def on_leave(self) -> None:
        """Called every time this frame is hidden. Tear down workers."""

    # ----- responsive text wrapping --------------------------------------

    def register_wrappable(
        self,
        widget: tk.Misc,
        ratio: float = 0.85,
        min_px: int = 300,
    ) -> None:
        """Register a Label so its ``wraplength`` tracks the stage width.

        ``ratio`` is the fraction of stage width to wrap at; ``min_px`` is
        a floor so very narrow windows still wrap something sensible.
        """
        self._wrappables.append((widget, ratio, min_px))

    def _on_stage_resize(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        width = event.width
        for widget, ratio, min_px in self._wrappables:
            try:
                widget.configure(wraplength=max(min_px, int(width * ratio)))
            except tk.TclError:
                pass
