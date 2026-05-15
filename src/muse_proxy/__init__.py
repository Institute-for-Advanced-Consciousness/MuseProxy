"""MuseProxy — experiment control + LSL marker stream for the IACS Muse
Proxy Metrics and Denoising Study.

Two passes per visit (Muse 2 + Muse Athena), four blocks per pass,
sample-anchored audio cues with LSL string markers mirrored to disk as
CSV. See PROJECT_PLAN.md for the build spec and CLAUDE.md for the
scientific brief.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
