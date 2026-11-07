"""Direct BLE → LSL bridges for the Muse-2 and Muse-Athena.

Replaces muselsl (Muse-2) and the OpenMuse-.txt relay (Muse-Athena) for this study.
Run as a companion process alongside the experiment app::

    uv run python -m muse_proxy.bridge scan
    uv run python -m muse_proxy.bridge stream --model muse-2 --address <MAC>

See ``docs/BLE_BRIDGE.md`` for the protocol notes and the hardware bring-up
checklist.
"""

from .common import BRIDGE_VERSION

__all__ = ["BRIDGE_VERSION"]
