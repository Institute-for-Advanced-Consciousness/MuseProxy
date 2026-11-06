"""Supervised BLE → LSL session for one headset (Muse 2 or Athena).

Architecture follows the IACS relay's ``DeviceBridge``:

* **BLE thread** — a private asyncio loop running the supervisor, the bleak
  client, keep-alive and the stall watchdog. The notification callback does
  the minimum: stamp LSL arrival time + UTC and enqueue the raw bytes.
* **Decode thread** — drains the queue: raw log → decode → LSL push. Heavy
  work never touches the BLE loop, so a slow disk can't delay notifications.

Recovery: a disconnect, a failed handshake, or a data stall (no data for
``hard_stall_s``) funnels into a reconnect — straight back to the same handle
first, re-scan by address/name if that fails — with exponential backoff that
resets only once data actually flows again.

Safety ("secure connection"):

* The bridge is pinned to ONE device address chosen by the operator. It never
  silently adopts a different headset; if the address changes (macOS) it only
  re-binds to a device advertising the exact same name.
* Every notification is length/route-checked before decode; a malformed packet
  is counted and dropped, never allowed to raise into the BLE loop.
* The queue is bounded; overflow is counted and reported, not hidden.
* Everything the device sent is preserved in the raw log, so any decode bug can
  be fixed after the fact.
"""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pylsl

from . import athena, muse2
from .athena import ControlInfo
from .common import EventOutlet, RawPacketLog, utc_iso


def encode_command(token: str) -> bytes:
    """Length-prefixed ASCII ``[len(token+'\\n')] token '\\n'`` — both models."""
    if not token or not token.isascii() or "\n" in token:
        raise ValueError(f"bad command token {token!r}")
    payload = (token + "\n").encode("ascii")
    if len(payload) > 255:
        raise ValueError("command too long")
    return bytes([len(payload)]) + payload


def new_ble_loop() -> asyncio.AbstractEventLoop:
    """Proactor loop on Windows (bleak's WinRT backend breaks on Selector)."""
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()


class State:
    IDLE = "idle"
    SCANNING = "scanning"
    CONNECTING = "connecting"
    INITIALIZING = "initializing"
    STREAMING = "streaming"
    RECONNECTING = "reconnecting"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass
class Target:
    model: str               # "muse2" | "athena"
    name: str                # advertised name, e.g. "Muse-1A2B" / "MuseS-0DBB"
    address: str             # MAC (Windows/Linux) or CoreBluetooth UUID (macOS)
    device_id: str           # stable id used in stream names/source_ids
    rssi: int | None = None
    ble_device: Any = None   # bleak BLEDevice when fresh from a scan


@dataclass
class BridgeConfig:
    preset: str | None = None             # None → model default
    prime: bool = True                    # Athena cold-start prime
    include_aux: bool = False             # Muse 2 AUX channel
    include_imu: bool = True
    athena_gap_correction: bool = False   # see athena.TickGapDetector
    keepalive_interval: float = 5.0
    soft_stall_s: float = 2.5             # re-assert start
    hard_stall_s: float = 5.0             # force reconnect
    scan_timeout: float = 6.0
    reconnect_scan_timeout: float = 3.0
    connect_timeout: float = 20.0
    first_data_timeout: float = 5.0
    initial_backoff: float = 0.25
    max_backoff: float = 12.0
    queue_max: int = 16384
    stats_event_interval: float = 10.0
    handshake_delay_scale: float = 1.0    # tests only


class DeviceBridge:
    def __init__(
        self,
        target: Target,
        config: BridgeConfig | None = None,
        *,
        raw_dir: str | Path | None = None,
        transport: Any = None,
        on_event: Callable[[str, str], None] | None = None,
    ) -> None:
        self.target = target
        self.cfg = config or BridgeConfig()
        self._on_event = on_event
        if transport is None:
            from .transport import BleakTransport

            transport = BleakTransport()
        self._transport = transport
        self.model = target.model
        if self.model == "muse2":
            self.publisher: Any = muse2.Muse2Publisher(
                target.device_id, include_aux=self.cfg.include_aux,
                include_imu=self.cfg.include_imu,
                device_desc={"ble_name": target.name, "ble_address": target.address},
            )
            self._preset = self.cfg.preset or muse2.DEFAULT_PRESET
            self._keepalive = muse2.KEEPALIVE_TOKEN
            self._control_uuid = muse2.CONTROL_UUID
        elif self.model == "athena":
            self.publisher = athena.AthenaPublisher(
                target.device_id,
                gap_correction=self.cfg.athena_gap_correction,
                device_desc={"ble_name": target.name, "ble_address": target.address},
            )
            self._preset = self.cfg.preset or athena.DEFAULT_PRESET
            self._keepalive = athena.KEEPALIVE_TOKEN
            self._control_uuid = athena.CONTROL_UUID
        else:
            raise ValueError(f"unknown model {self.model!r}")
        self._known_uuids = {u.lower() for u in self.publisher.data_uuids}

        self.events = EventOutlet(target.device_id, self.model)
        self.raw: RawPacketLog | None = None
        if raw_dir is not None:
            self.raw = RawPacketLog(
                Path(raw_dir), f"raw_ble_{self.model}_{target.device_id}",
                {"model": self.model, "ble_name": target.name, "ble_address": target.address,
                 "device_id": target.device_id, "preset": self._preset,
                 "format": "utc_iso<TAB>characteristic_uuid<TAB>hex_payload",
                 "started_utc": utc_iso()},
            )

        self._lock = threading.Lock()
        self._state = State.IDLE
        self.control = ControlInfo()
        self.reconnects = 0
        self.queue_overflow = 0
        self.unknown_uuid = 0
        self.packets_total = 0
        self.last_error: str | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=self.cfg.queue_max)
        self._last_data = 0.0
        self._packets_since_connect = 0
        self._accept = False
        self._achieved_once = False
        self._fast_reconnect = False
        self._stop = False
        self._stopping = False
        self._stopped_evt = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._disc: asyncio.Event | None = None
        self._ble_thread: threading.Thread | None = None
        self._decode_thread: threading.Thread | None = None
        self._decode_stop = threading.Event()
        self._segment_pending = False

    # ------------------------------------------------------------------ API
    def start(self) -> None:
        if self._stop:
            raise RuntimeError("DeviceBridge is single-use")
        if self._ble_thread is not None:
            return
        self.events.push("bridge:start", {"model": self.model, "id": self.target.device_id,
                                          "address": self.target.address,
                                          "preset": self._preset})
        self._decode_thread = threading.Thread(target=self._decode_loop,
                                               name="musebridge-decode", daemon=True)
        self._ble_thread = threading.Thread(target=self._ble_main,
                                            name="musebridge-ble", daemon=True)
        self._decode_thread.start()
        self._ble_thread.start()

    def stop(self, timeout: float = 8.0) -> None:
        with self._lock:
            already = self._stopping
            self._stopping = True
        if already:
            # Wait OUTSIDE the lock — the first stop() needs it (snapshot/state).
            self._stopped_evt.wait(timeout)
            return
        self._stop = True
        self._set_state(State.STOPPING)
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._wake_set)
            except RuntimeError:
                pass
        if self._ble_thread is not None:
            self._ble_thread.join(timeout)
        self._decode_stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._decode_thread is not None:
            self._decode_thread.join(5.0)
        self.events.push("bridge:stop", self._summary())
        self.events.close()
        if self.raw is not None:
            self.raw.note(f"stopped; summary={self._summary()}")
            self.raw.close()
        self._set_state(State.STOPPED)
        self._stopped_evt.set()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def snapshot(self) -> dict[str, Any]:
        age = (time.monotonic() - self._last_data) if self._last_data else None
        with self._lock:
            return {
                "state": self._state,
                "model": self.model,
                "device_id": self.target.device_id,
                "address": self.target.address,
                "firmware": self.control.firmware,
                "battery": self.control.battery if self.control.battery is not None
                else self.publisher.battery,
                "reconnects": self.reconnects,
                "packets": self.packets_total,
                "queue": self._queue.qsize(),
                "queue_overflow": self.queue_overflow,
                "seconds_since_data": age,
                "last_error": self.last_error,
                "raw_log": str(self.raw.path) if self.raw else None,
                "streams": self.publisher.stats(),
            }

    def _summary(self) -> dict[str, Any]:
        snap = self.snapshot()
        out: dict[str, Any] = {"reconnects": snap["reconnects"], "packets": snap["packets"],
                               "overflow": snap["queue_overflow"]}
        for k, v in snap["streams"].items():
            if isinstance(v, dict) and "lost_pct" in v:
                out[f"{k.lower()}_lost_pct"] = v["lost_pct"]
        return out

    # --------------------------------------------------------- decode thread
    def _decode_loop(self) -> None:
        pub = self.publisher
        last_stats = time.monotonic()
        while True:
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                item = False
            if item is None:
                break
            if item is not False:
                kind = item[0]
                if kind == "segment":
                    pub.new_segment()
                else:
                    _, arrival, utc, uuid, raw = item
                    if self.raw is not None:
                        try:
                            self.raw.write(utc, uuid, raw)
                        except OSError as exc:
                            self._set_error(f"raw log write failed: {exc}")
                    try:
                        if self.model == "muse2":
                            pub.process(arrival, uuid, raw)
                        else:
                            pub.process(arrival, utc, uuid, raw)
                    except Exception as exc:  # noqa: BLE001 — never kill the stream
                        self._set_error(f"decode: {exc!r}")
            now = time.monotonic()
            if now - last_stats >= self.cfg.stats_event_interval:
                last_stats = now
                self.events.push("stats", self._summary())
            if self._decode_stop.is_set() and self._queue.empty():
                break
        try:
            pub.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ BLE thread
    def _ble_main(self) -> None:
        loop = new_ble_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._supervisor())
        except Exception as exc:  # noqa: BLE001
            self._set_error(f"ble thread: {exc!r}")
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            asyncio.set_event_loop(None)
            loop.close()
            self._loop = None

    def _wake_set(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _sleep(self, seconds: float) -> None:
        if self._stop:
            return
        assert self._wake is not None
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _supervisor(self) -> None:
        self._wake = asyncio.Event()
        backoff = self.cfg.initial_backoff
        while not self._stop:
            try:
                self._set_state(State.SCANNING)
                device = await self._find_device(fast=self._fast_reconnect)
                self._fast_reconnect = False
                if device is None and not self._stop:
                    device = await self._find_device(fast=False)
                if self._stop:
                    break
                if device is None:
                    self._event("warn", f"{self.target.name} not found; rescanning")
                    await self._sleep(backoff)
                    backoff = min(backoff * 2, self.cfg.max_backoff)
                    continue
                achieved, reason = await self._connect_and_stream(device)
                if self._stop or reason == "stop":
                    break
                if achieved:
                    backoff = self.cfg.initial_backoff
                    with self._lock:
                        self.reconnects += 1
                else:
                    backoff = min(backoff * 2, self.cfg.max_backoff)
                self._set_state(State.RECONNECTING)
                self.events.push("link:lost", {"reason": reason})
                self._event("warn", f"link lost ({reason}); reconnecting")
                if self.raw is not None:
                    self.raw.note(f"link lost: {reason}")
                await self._sleep(backoff)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self._set_error(f"supervisor: {exc!r}")
                self._set_state(State.RECONNECTING)
                await self._sleep(backoff)
                backoff = min(backoff * 2, self.cfg.max_backoff)

    async def _find_device(self, fast: bool) -> Any:
        if fast:
            return self.target.ble_device or self.target.address
        timeout = (self.cfg.reconnect_scan_timeout if self._achieved_once
                   else self.cfg.scan_timeout)
        try:
            found = await self._transport.scan(timeout)
        except Exception as exc:  # noqa: BLE001
            self._set_error(f"scan: {exc!r}")
            return None
        for d in found:
            if d.address == self.target.address:
                self.target.rssi = d.rssi
                self.target.ble_device = d.ble_device
                return d.ble_device or d.address
        # Address rotated (macOS CoreBluetooth): re-bind ONLY to the identical name.
        for d in found:
            if d.name and d.name == self.target.name and d.model == self.model:
                self._event("warn", f"address changed {self.target.address} -> {d.address}")
                self.target.address = d.address
                self.target.ble_device = d.ble_device
                return d.ble_device or d.address
        return None

    async def _send(self, client: Any, token: str) -> None:
        if not token:
            return
        try:
            await client.write_gatt_char(self._control_uuid, encode_command(token),
                                         response=False)
        except Exception as exc:  # noqa: BLE001
            self._event("warn", f"send {token}: {exc!r}")

    async def _handshake(self, client: Any) -> None:
        if self.model == "muse2":
            steps = muse2.handshake_tokens(self._preset)
        else:
            steps = athena.handshake_tokens(self._preset,
                                            prime=self.cfg.prime and not self._achieved_once)
        for token, delay in steps:
            await self._send(client, token)
            await asyncio.sleep(delay * self.cfg.handshake_delay_scale)

    async def _connect_and_stream(self, device: Any) -> tuple[bool, str]:
        self._disc = asyncio.Event()
        self._packets_since_connect = 0
        self._accept = False

        def _on_disconnect(_c: Any) -> None:
            if self._disc is not None and self._loop is not None:
                self._loop.call_soon_threadsafe(self._disc.set)

        self._set_state(State.CONNECTING)
        async with self._transport.client(device, timeout=self.cfg.connect_timeout,
                                          disconnected_callback=_on_disconnect) as client:
            self._set_state(State.INITIALIZING)
            self.events.push("link:connected", {"address": self.target.address})
            try:
                await client.start_notify(self._control_uuid, self._on_control)
            except Exception as exc:  # noqa: BLE001
                self._event("warn", f"control subscribe: {exc!r}")
            subscribed = 0
            for uuid in self.publisher.data_uuids:
                try:
                    await client.start_notify(uuid, self._on_data)
                    subscribed += 1
                except Exception as exc:  # noqa: BLE001
                    self._event("warn", f"subscribe {uuid[:8]}: {exc!r}")
            if subscribed == 0:
                return False, "subscribe_failed"
            # New link segment: flush/reset timelines before any new data lands.
            self._put(("segment",))
            # Muse 2 has no prime phase: accept from the start command onward.
            # Athena must discard the p21 prime burst until the target preset is live.
            self._accept = self.model == "muse2"
            await self._handshake(client)
            self._accept = True
            if not await self._await_first_data():
                await self._send(client, "d" if self.model == "muse2" else "dc001")
                if self.model == "athena":
                    await self._send(client, "L1")
                if not await self._await_first_data():
                    return False, "no_data"
            self._achieved_once = True
            self._fast_reconnect = True
            self._set_state(State.STREAMING)
            self.events.push("link:streaming", {"firmware": self.control.firmware or "?",
                                                "preset": self._preset})
            self._event("info", "streaming")
            reason = await self._monitor(client)
            if self._stop:
                await self._send(client, "h")
            return True, reason

    async def _await_first_data(self) -> bool:
        deadline = time.monotonic() + self.cfg.first_data_timeout
        while time.monotonic() < deadline and not self._stop:
            if self._packets_since_connect > 0:
                return True
            await self._sleep(0.1)
        return self._packets_since_connect > 0

    async def _monitor(self, client: Any) -> str:
        last_keepalive = time.monotonic()
        soft_done = False
        while not self._stop:
            if (self._disc is not None and self._disc.is_set()) or not client.is_connected:
                return "disconnected"
            now = time.monotonic()
            if now - last_keepalive >= self.cfg.keepalive_interval:
                await self._send(client, self._keepalive)
                last_keepalive = now
            age = now - self._last_data if self._last_data else 0.0
            if age > self.cfg.hard_stall_s:
                return "stalled"
            if age > self.cfg.soft_stall_s:
                if not soft_done:
                    self._event("warn", f"data quiet {age:.1f}s; re-asserting stream")
                    if self.model == "muse2":
                        await self._send(client, "d")
                    else:
                        await self._send(client, "dc001")
                        await self._send(client, "L1")
                    soft_done = True
            else:
                soft_done = False
            await self._sleep(0.25)
        return "stop"

    # ------------------------------------------------------ BLE callbacks
    def _put(self, item: tuple) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            with self._lock:
                self.queue_overflow += 1

    def _on_data(self, char: Any, data: bytearray) -> None:
        if not self._accept:
            return  # handshake/prime traffic (e.g. Athena p21 EEG4 burst)
        uuid = str(getattr(char, "uuid", char)).lower()
        if uuid not in self._known_uuids:
            self.unknown_uuid += 1
            return
        if len(data) == 0 or len(data) > 512:
            return
        self._last_data = time.monotonic()
        self._packets_since_connect += 1
        self.packets_total += 1
        self._put(("data", pylsl.local_clock(), utc_iso(), uuid, bytes(data)))

    def _on_control(self, _char: Any, data: bytearray) -> None:
        with self._lock:
            self.control.feed(bytes(data))

    # ------------------------------------------------------------ helpers
    def _set_state(self, state: str) -> None:
        with self._lock:
            if self._state == State.STOPPED and state != State.STOPPED:
                return
            self._state = state

    def _set_error(self, msg: str) -> None:
        with self._lock:
            self.last_error = msg
        self._event("error", msg)

    def _event(self, level: str, msg: str) -> None:
        if self._on_event:
            try:
                self._on_event(level, msg)
            except Exception:  # noqa: BLE001
                pass
