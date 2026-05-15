"""Allow ``python -m muse_proxy`` to launch the GUI.

The canonical entry point per PROJECT_PLAN.md is
``python -m muse_proxy.app``, but ``python -m muse_proxy`` works too.
"""

from __future__ import annotations

from .app import main

if __name__ == "__main__":
    raise SystemExit(main())
