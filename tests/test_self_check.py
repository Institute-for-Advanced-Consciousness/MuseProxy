"""``--self-check`` exits 0 in a clean dev environment.

This is the install-time smoke test wired through INSTALL.bat. We run
it ONLY as a subprocess so it doesn't share a Tk init slot with the
``shared_app`` fixture.
"""

from __future__ import annotations

import subprocess
import sys


def test_self_check_via_subprocess() -> None:
    """Run as a fresh subprocess to mirror what INSTALL.bat does."""
    result = subprocess.run(
        [sys.executable, "-m", "muse_proxy.app", "--self-check"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"self-check failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "SELF-CHECK OK" in result.stdout
