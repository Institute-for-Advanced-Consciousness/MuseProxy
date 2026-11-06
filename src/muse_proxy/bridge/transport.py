"""Transport seam: real bleak vs. an in-process fake headset.

``DeviceBridge`` needs only ``scan(timeout)`` and ``client(device, timeout=,
disconnected_callback=)`` (an async context manager exposing ``is_connected``,
``start_notify`` and ``write_gatt_char``). ``FakeTransport`` serves synthetic
Muse 2 / Athena devices through that exact surface, so the decode → LSL path,
the NaN gap handling and the reconnect supervisor are all testable with no
hardware — and ``--fake`` gives the operator a dry run of the whole chain.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import athena, muse2
from .athena_synth import SyntheticAthena


@dataclass
class Discovered:
    name: str
    address: str
    model: str            # "muse2" | "athena" | "unknown"
    device_id: str
    rssi: int | None = None
    ble_device: Any = None


def classify(name: str | None) -> str:
    """Advertised-name → model.

    ``Muse-XXXX`` is the Muse 2 (and the 2016 Muse, same protocol, no PPG);
    ``MuseS-XXXX`` is the Muse S family — Athena is assumed (Gen 1/2 Muse S
    also advertise ``MuseS`` but speak the Muse 2 protocol; select
    ``--model muse2`` explicitly for those).
    """
    if not name:
        return "unknown"
    n = name.strip().lower()
    if n.startswith("muses"):
        return "athena"
    if n.startswith("muse"):
        return "muse2"
    return "unknown"


def device_id_from(name: str | None, address: str) -> str:
    """``Muse-1A2B`` → ``1A2B``; fallback: last 4 hex digits of the address."""
    if name:
        m = re.search(r"([0-9A-Fa-f]{4})\s*$", name)
        if m:
            return m.group(1).upper()
    clean = re.sub(r"[^0-9A-Fa-f]", "", address)
    return (clean[-4:] or "MUSE").upper()


# --- real -----------------------------------------------------------------------
class BleakTransport:
    async def scan(self, timeout: float) -> list[Discovered]:
        from bleak import BleakScanner

        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        out: list[Discovered] = []
        for address, (dev, adv) in found.items():
            name = getattr(dev, "name", None) or getattr(adv, "local_name", None)
            model = classify(name)
            if model == "unknown":
                continue
            out.append(Discovered(name=name or "", address=address, model=model,
                                  device_id=device_id_from(name, address),
                                  rssi=getattr(adv, "rssi", None), ble_device=dev))
        out.sort(key=lambda d: d.rssi if d.rssi is not None else -999, reverse=True)
        return out

    def client(self, device: Any, *, timeout: float, disconnected_callback: Callable):
        from bleak import BleakClient

        return BleakClient(device, timeout=timeout,
                           disconnected_callback=disconnected_callback)


# --- fake -----------------------------------------------------------------------
class _Char:
    def __init__(self, uuid: str) -> None:
        self.uuid = uuid


def _decode_command(data: bytes) -> str:
    return data[1:].decode("ascii", errors="ignore").strip() if data else ""


class FakeClient:
    """Honours the start/stop commands of either model and pumps synthetic data.

    ``drop_after_s`` disconnects after that long (tests the reconnect path).
    """

    def __init__(self, model: str, disconnected_callback: Callable | None,
                 drop_prob: float = 0.0, drop_after_s: float | None = None) -> None:
        self.model = model
        self._cb_disc = disconnected_callback
        self._drop_after = drop_after_s
        self._callbacks: dict[str, Callable] = {}
        self._connected = False
        self._streaming = False
        self._t_stream: float | None = None
        self._t_conn = 0.0
        self._task: asyncio.Task | None = None
        self.commands: list[str] = []
        if model == "muse2":
            self._synth: Any = muse2.SyntheticMuse2(drop_prob=drop_prob)
            self._start_tok = "d"
        else:
            self._synth = SyntheticAthena("FAKE")
            self._start_tok = "dc001"
        self._prime = False

    async def __aenter__(self) -> FakeClient:
        self._connected = True
        self._t_conn = time.monotonic()
        self._task = asyncio.get_running_loop().create_task(self._pump())
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        await self._teardown(notify=False)
        return False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def start_notify(self, uuid: str, callback: Callable) -> None:
        self._callbacks[uuid.lower()] = callback

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = False) -> None:
        tok = _decode_command(bytes(data))
        self.commands.append(tok)
        if tok == "p21" and self.model == "athena":
            self._prime = True
        elif tok.startswith("p") and tok[1:].isdigit():
            self._prime = False
        if tok == self._start_tok and not self._prime and self._t_stream is None:
            self._t_stream = time.monotonic()
            self._streaming = True
        elif tok == "h":
            self._streaming = False
        elif tok in ("s", "v1", "v6"):
            cb = self._callbacks.get(muse2.CONTROL_UUID)
            if cb is not None:
                cb(_Char(muse2.CONTROL_UUID), bytearray(b'{"fw":"fake-1.0","bp":87.5}'))

    async def _pump(self) -> None:
        try:
            while self._connected:
                if self._drop_after is not None and \
                        time.monotonic() - self._t_conn >= self._drop_after:
                    await self._teardown(notify=True)
                    return
                if self._streaming and self._t_stream is not None:
                    elapsed = time.monotonic() - self._t_stream
                    for uuid, raw in self._synth.due_packets(elapsed):
                        cb = self._callbacks.get(uuid.lower())
                        if cb is not None:
                            cb(_Char(uuid.lower()), bytearray(raw))
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            pass

    async def _teardown(self, notify: bool) -> None:
        was = self._connected
        self._connected = False
        self._streaming = False
        if self._task is not None and asyncio.current_task() is not self._task:
            self._task.cancel()
        if notify and was and self._cb_disc is not None:
            self._cb_disc(self)


class FakeTransport:
    """One synthetic device. ``scenarios[attempt]`` → FakeClient kwargs."""

    def __init__(self, model: str, scenarios: dict[int, dict] | None = None,
                 drop_prob: float = 0.0, device_id: str = "FAKE") -> None:
        self.model = model
        name = f"Muse-{device_id}" if model == "muse2" else f"MuseS-{device_id}"
        self.device = Discovered(name=name, address="00:55:DA:00:FA:CE", model=model,
                                 device_id=device_id, rssi=-50)
        self._scenarios = scenarios or {}
        self._drop_prob = drop_prob
        self.attempts = 0
        self.clients: list[FakeClient] = []

    async def scan(self, timeout: float) -> list[Discovered]:
        await asyncio.sleep(0)
        return [self.device]

    def client(self, device: Any, *, timeout: float, disconnected_callback: Callable):
        kw = {"drop_prob": self._drop_prob, **self._scenarios.get(self.attempts, {})}
        self.attempts += 1
        c = FakeClient(self.model, disconnected_callback, **kw)
        self.clients.append(c)
        return c


# Re-exported for the CLI.
CONTROL_UUIDS = {"muse2": muse2.CONTROL_UUID, "athena": athena.CONTROL_UUID}
