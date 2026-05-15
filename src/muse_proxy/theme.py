"""IACS color palette + widget helpers for GUI chrome.

Two visual modes coexist in the app:

  Chrome mode — intake, pre-launch, volume check, settle screens,
                pass switch, completion. Dark grey ``CHROME_BG`` with
                an IACS purple ``ACCENT``, off-white text. This is the
                operator + between-task participant surface.

  Task mode  — anything where the participant should be focused on
                the stimulus (block in-progress "eyes closed" screens,
                between-block rest, block instructions). Pure black
                background with pure white text — maximum contrast,
                minimum distraction.

Borrowed wholesale from SuperChillsOddball's theme for institutional
continuity. Constants are authoritative — config YAML's ``ui`` block
is no longer read by the runtime.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Mapping
from typing import Any

# --- Chrome palette -----------------------------------------------------

ACCENT = "#5d5179"           # IACS purple — primary brand color
ACCENT_LIGHT = "#8a7ca5"     # lighter purple — hover, accent text
ACCENT_DARK = "#3d3550"      # darker purple — pressed states, secondary btn
CHROME_BG = "#1a1a1a"        # dark grey — chrome background
SURFACE = "#252525"          # slightly lighter — text boxes, panels
SURFACE_RAISED = "#2f2735"   # purple-tinted raised surface (radio, slider)
DIVIDER = "#3a3540"          # subtle separator
TEXT = "#e8e6ec"             # off-white — chrome body text
TEXT_MUTED = "#9b95a3"       # captions, hints, footers
TEXT_ACCENT = "#a89bc4"      # purple-tinted — emphasized chrome text
ERROR = "#ff8888"            # validation / error red

# --- Task palette (pure contrast) --------------------------------------

TASK_BG = "#000000"
TASK_TEXT = "#FFFFFF"

# --- Backwards-compatible aliases (existing tests reference these names)
PURPLE = ACCENT
PURPLE_DARK = ACCENT_DARK
PURPLE_LIGHT = ACCENT_LIGHT
WHITE = "#FFFFFF"
BLACK = "#000000"
GRAY_LIGHT = "#444444"
GRAY_MID = TEXT_MUTED

# --- Typography --------------------------------------------------------

FONT_FAMILY = "Segoe UI"

FONT_HEADING = (FONT_FAMILY, 28, "bold")
FONT_SUBHEADING = (FONT_FAMILY, 16, "bold")
FONT_PANEL_HEADER = (FONT_FAMILY, 11, "bold")
FONT_BODY = (FONT_FAMILY, 12)
FONT_BODY_BOLD = (FONT_FAMILY, 12, "bold")
FONT_LABEL = (FONT_FAMILY, 11)
FONT_BUTTON = (FONT_FAMILY, 12, "bold")
FONT_BUTTON_LARGE = (FONT_FAMILY, 14, "bold")
FONT_SMALL = (FONT_FAMILY, 10)
FONT_TASK = (FONT_FAMILY, 60, "bold")        # in-task on-screen cues
FONT_TASK_MED = (FONT_FAMILY, 36, "bold")    # block instructions
FONT_TIMER = (FONT_FAMILY, 48, "bold")       # countdown timers
FONT_MONO = ("Consolas", 11)

# --- Button styling -----------------------------------------------------

PRIMARY_BUTTON: Mapping[str, Any] = dict(
    bg=ACCENT,
    fg="#FFFFFF",
    activebackground=ACCENT_LIGHT,
    activeforeground="#FFFFFF",
    disabledforeground=TEXT_MUTED,
    relief="flat",
    borderwidth=0,
    cursor="hand2",
)

SECONDARY_BUTTON: Mapping[str, Any] = dict(
    bg=ACCENT_DARK,
    fg=TEXT,
    activebackground=ACCENT,
    activeforeground="#FFFFFF",
    disabledforeground=TEXT_MUTED,
    relief="flat",
    borderwidth=0,
    cursor="hand2",
)

DISABLED_BUTTON: Mapping[str, Any] = dict(
    bg=SURFACE,
    fg=TEXT_MUTED,
    activebackground=SURFACE,
    activeforeground=TEXT_MUTED,
    relief="flat",
    borderwidth=0,
)

# --- Legacy ttk style name placeholders (kept for tests that look them up) ---
STYLE_PRIMARY_BUTTON = "Purple.TButton"
STYLE_DISABLED_BUTTON = "Disabled.TButton"
STYLE_ABORT_BUTTON = "Abort.TButton"
STYLE_SECONDARY_BUTTON = "Secondary.TButton"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def configure_ttk_styles(style: Any) -> None:
    """Configure ttk styles used by Combobox and Scrollbar.

    We don't use ttk.Button (tk.Button + theme.PRIMARY_BUTTON looks
    cleaner on Windows), but Combobox + Scrollbar are still ttk so we
    style them to match the dark chrome.
    """
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(
        "Dark.TCombobox",
        fieldbackground=SURFACE,
        background=SURFACE_RAISED,
        foreground=TEXT,
        arrowcolor=TEXT,
        bordercolor=DIVIDER,
        lightcolor=DIVIDER,
        darkcolor=DIVIDER,
        insertcolor=ACCENT_LIGHT,
        selectbackground=ACCENT_DARK,
        selectforeground="#FFFFFF",
        padding=6,
    )
    style.map(
        "Dark.TCombobox",
        fieldbackground=[("readonly", SURFACE)],
        foreground=[("readonly", TEXT), ("disabled", TEXT_MUTED)],
        background=[("active", ACCENT_DARK), ("readonly", SURFACE_RAISED)],
        bordercolor=[("focus", ACCENT_LIGHT)],
    )

    style.configure(
        "Dark.Vertical.TScrollbar",
        background=SURFACE_RAISED,
        troughcolor=CHROME_BG,
        bordercolor=CHROME_BG,
        arrowcolor=TEXT,
        gripcount=0,
    )
    style.map(
        "Dark.Vertical.TScrollbar",
        background=[("active", ACCENT_DARK)],
    )

    # Legacy ttk styles — only present in case any leftover code looks
    # them up. The button styles map to the dark palette but the modules
    # we ship use raw tk.Button + theme dicts.
    style.configure(
        STYLE_PRIMARY_BUTTON,
        background=ACCENT,
        foreground="#FFFFFF",
        font=FONT_BUTTON,
        padding=(12, 10),
        borderwidth=0,
    )
    style.map(
        STYLE_PRIMARY_BUTTON,
        background=[("active", ACCENT_LIGHT), ("disabled", SURFACE)],
        foreground=[("disabled", TEXT_MUTED)],
    )
    style.configure(
        STYLE_DISABLED_BUTTON,
        background=SURFACE,
        foreground=TEXT_MUTED,
        font=FONT_BUTTON,
        padding=(12, 10),
        borderwidth=0,
    )
    style.configure(
        STYLE_SECONDARY_BUTTON,
        background=ACCENT_DARK,
        foreground=TEXT,
        font=FONT_BUTTON,
        padding=(12, 10),
        borderwidth=0,
    )
    style.map(
        STYLE_SECONDARY_BUTTON,
        background=[("active", ACCENT)],
        foreground=[("active", "#FFFFFF")],
    )
    style.configure(
        STYLE_ABORT_BUTTON,
        background=SURFACE_RAISED,
        foreground=TEXT,
        font=FONT_SMALL,
        padding=(8, 4),
        borderwidth=0,
    )


# ---------------------------------------------------------------------------
# Widget builders — keeps stages succinct and consistent.
# ---------------------------------------------------------------------------


def heading(parent: tk.Misc, text: str, *, bg: str = CHROME_BG) -> tk.Label:
    return tk.Label(
        parent, text=text,
        font=FONT_HEADING,
        fg=ACCENT_LIGHT,
        bg=bg,
    )


def subheading(parent: tk.Misc, text: str, *, bg: str = CHROME_BG) -> tk.Label:
    return tk.Label(
        parent, text=text,
        font=FONT_SUBHEADING,
        fg=TEXT_ACCENT,
        bg=bg,
    )


def body(
    parent: tk.Misc, text: str, *, bg: str = CHROME_BG, fg: str = TEXT,
    wraplength: int = 0, justify: str = "left",
) -> tk.Label:
    return tk.Label(
        parent, text=text,
        font=FONT_BODY,
        fg=fg,
        bg=bg,
        wraplength=wraplength,
        justify=justify,
    )


def muted(parent: tk.Misc, text: str, *, bg: str = CHROME_BG) -> tk.Label:
    return tk.Label(
        parent, text=text,
        font=FONT_SMALL,
        fg=TEXT_MUTED,
        bg=bg,
    )


def panel(parent: tk.Misc, *, padx: int = 14, pady: int = 10) -> tk.Frame:
    """A dark surface panel with a subtle border. Use for grouped fields."""
    f = tk.Frame(
        parent,
        bg=SURFACE,
        padx=padx,
        pady=pady,
        highlightthickness=1,
        highlightbackground=DIVIDER,
    )
    return f


def panel_header(parent: tk.Misc, text: str) -> tk.Label:
    """Small uppercase header for a panel."""
    return tk.Label(
        parent, text=text,
        font=FONT_PANEL_HEADER,
        fg=ACCENT_LIGHT,
        bg=SURFACE,
        anchor="w",
    )


def field_label(parent: tk.Misc, text: str, *, bg: str = SURFACE) -> tk.Label:
    return tk.Label(
        parent, text=text,
        font=FONT_LABEL,
        fg=TEXT,
        bg=bg,
        anchor="w",
    )


def dark_entry(
    parent: tk.Misc, textvariable: tk.Variable, *, width: int = 18,
    readonly: bool = False,
) -> tk.Entry:
    e = tk.Entry(
        parent,
        textvariable=textvariable,
        font=FONT_BODY,
        width=width,
        bg=SURFACE if not readonly else SURFACE_RAISED,
        fg=TEXT,
        insertbackground=ACCENT_LIGHT,
        relief="flat",
        highlightthickness=1,
        highlightbackground=DIVIDER,
        highlightcolor=ACCENT_LIGHT,
        disabledbackground=SURFACE_RAISED,
        disabledforeground=TEXT_MUTED,
    )
    if readonly:
        e.configure(state="readonly", readonlybackground=SURFACE_RAISED)
    return e


def dark_text(parent: tk.Misc, *, height: int = 4, width: int = 50) -> tk.Text:
    return tk.Text(
        parent,
        height=height,
        width=width,
        font=FONT_BODY,
        bg=SURFACE,
        fg=TEXT,
        insertbackground=ACCENT_LIGHT,
        relief="flat",
        highlightthickness=1,
        highlightbackground=DIVIDER,
        highlightcolor=ACCENT_LIGHT,
        wrap="word",
        padx=8,
        pady=6,
    )


def dark_checkbutton(
    parent: tk.Misc, text: str, variable: tk.BooleanVar, *,
    bg: str = SURFACE, command: Any = None, wraplength: int = 0,
) -> tk.Checkbutton:
    return tk.Checkbutton(
        parent,
        text=text,
        variable=variable,
        command=command,
        font=FONT_BODY,
        fg=TEXT,
        bg=bg,
        activebackground=bg,
        activeforeground=ACCENT_LIGHT,
        selectcolor=ACCENT_DARK,
        anchor="w",
        justify="left",
        wraplength=wraplength,
        padx=4,
        pady=3,
        cursor="hand2",
        highlightthickness=0,
        borderwidth=0,
    )


def primary_button(
    parent: tk.Misc, text: str, command: Any, *,
    width: int = 22, font: tuple = FONT_BUTTON, padx: int = 16, pady: int = 10,
    state: str = "normal",
) -> tk.Button:
    return tk.Button(
        parent,
        text=text,
        command=command,
        width=width,
        font=font,
        padx=padx,
        pady=pady,
        state=state,
        **PRIMARY_BUTTON,
    )


def secondary_button(
    parent: tk.Misc, text: str, command: Any, *,
    width: int = 18, font: tuple = FONT_BUTTON, padx: int = 12, pady: int = 8,
) -> tk.Button:
    return tk.Button(
        parent,
        text=text,
        command=command,
        width=width,
        font=font,
        padx=padx,
        pady=pady,
        **SECONDARY_BUTTON,
    )


def hr(parent: tk.Misc, *, bg: str = CHROME_BG) -> tk.Frame:
    """Horizontal divider line."""
    return tk.Frame(parent, bg=DIVIDER, height=1, bd=0)
