"""Live LSL stream-health monitor.

This module does NOT display signals — that stays out of scope per the
project brief. It answers one question: *is each expected LSL stream
present and still producing samples right now?* That is enough to (a)
gate a block from starting while a stream is down and (b) detect a
mid-block dropout so the RA can re-run just that block.

A second inlet per stream does not disturb Lab Recorder's recording —
LSL fans every outlet out to all inlets independently.

Design — two layers, split so the status logic is testable without LSL:

  * ``Poller`` — resolves streams and pulls samples. ``LslPoller`` is the
    real one; tests inject a fake. One ``poll()`` returns, per stream,
    whether it is currently resolvable and how many samples arrived
    since the previous poll.
  * ``StreamHealthMonitor`` — owns a background thread that calls
    ``poll()`` on an interval, derives ALIVE / SILENT / MISSING per
    stream, and keeps a bounded history so ``window_report()`` can say
    whether a stream degraded at any point during a block.

Status rules per poll:
  * not resolvable                         -> MISSING
  * resolvable, irregular (Markers)        -> ALIVE  (presence only)
  * resolvable, samples arrived            -> ALIVE
  * resolvable, no samples for N polls     -> SILENT (N = ``silent_polls``)
"""

from __future__ import annotations

import enum
import logging
import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

import pylsl

logger = logging.getLogger(__name__)

__all__ = [
    "StreamStatus",
    "StreamSpec",
    "StreamHealth",
    "PollResult",
    "Poller",
    "LslPoller",
    "StreamHealthMonitor",
    "specs_from_stream_refs",
    "all_alive",
    "block_health_summary",
]


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


class StreamStatus(enum.Enum):
    """Health of one stream. Ordered ALIVE < SILENT < MISSING by ``rank``."""

    ALIVE = "alive"
    SILENT = "silent"
    MISSING = "missing"

    @property
    def rank(self) -> int:
        return _STATUS_RANK[self]

    @property
    def ok(self) -> bool:
        return self is StreamStatus.ALIVE


_STATUS_RANK = {
    StreamStatus.ALIVE: 0,
    StreamStatus.SILENT: 1,
    StreamStatus.MISSING: 2,
}


@dataclass(frozen=True)
class StreamSpec:
    """One stream the monitor expects to see.

    ``key`` uniquely identifies the spec. ``name`` is a case-insensitive
    substring matched against the LSL stream name; ``type`` (if set) must
    equal the LSL stream type exactly. ``expect_continuous`` is False for
    irregular streams (markers) — presence alone counts as alive.
    """

    key: str
    name: str
    type: str | None = None
    description: str = ""
    expect_continuous: bool = True


@dataclass
class PollResult:
    """What one ``poll()`` observed for a single stream."""

    present: bool
    new_samples: int = 0


@dataclass
class StreamHealth:
    """Current monitor state for one stream."""

    spec: StreamSpec
    status: StreamStatus = StreamStatus.MISSING
    last_alive_lsl: float | None = None
    new_samples_last_poll: int = 0
    silent_polls: int = 0


# ---------------------------------------------------------------------------
# Poller — the LSL-touching seam
# ---------------------------------------------------------------------------


class Poller(Protocol):
    """Resolves streams and pulls samples. One poll per call."""

    def poll(self) -> dict[str, PollResult]:
        """Return a PollResult per spec key."""
        ...

    def close(self) -> None:
        """Release any inlets."""
        ...


class LslPoller:
    """Real ``Poller``: pylsl inlets, one per spec.

    Inlets are opened lazily and cached. A lost stream drops its inlet;
    the next poll re-resolves so a reconnect is picked up automatically.
    ``recover=False`` is essential — with recovery on, a lost inlet
    blocks silently instead of surfacing as MISSING.
    """

    def __init__(self, specs: Iterable[StreamSpec], *, resolve_timeout_s: float = 0.2) -> None:
        self._specs: tuple[StreamSpec, ...] = tuple(specs)
        self._resolve_timeout_s = resolve_timeout_s
        self._inlets: dict[str, pylsl.StreamInlet | None] = {s.key: None for s in self._specs}

    def poll(self) -> dict[str, PollResult]:
        self._resolve_missing()
        results: dict[str, PollResult] = {}
        for spec in self._specs:
            inlet = self._inlets.get(spec.key)
            if inlet is None:
                results[spec.key] = PollResult(present=False)
                continue
            try:
                results[spec.key] = PollResult(
                    present=True, new_samples=self._drain(inlet)
                )
            except Exception:  # noqa: BLE001 — LostError and friends
                logger.debug("inlet for %s lost", spec.key, exc_info=True)
                self._close_inlet(spec.key)
                results[spec.key] = PollResult(present=False)
        return results

    def close(self) -> None:
        for key in list(self._inlets):
            self._close_inlet(key)

    # ----- internal -------------------------------------------------------

    def _resolve_missing(self) -> None:
        unmatched = [s for s in self._specs if self._inlets.get(s.key) is None]
        if not unmatched:
            return
        try:
            infos = pylsl.resolve_streams(wait_time=self._resolve_timeout_s)
        except Exception:  # noqa: BLE001
            logger.debug("resolve_streams failed", exc_info=True)
            return
        for spec in unmatched:
            info = _match_info(spec, infos)
            if info is None:
                continue
            try:
                self._inlets[spec.key] = pylsl.StreamInlet(
                    info, max_buflen=2, recover=False
                )
            except Exception:  # noqa: BLE001
                logger.debug("StreamInlet open failed for %s", spec.key, exc_info=True)
                self._inlets[spec.key] = None

    @staticmethod
    def _drain(inlet: pylsl.StreamInlet) -> int:
        """Pull and discard every buffered sample; return the count."""
        total = 0
        while True:
            samples, _ts = inlet.pull_chunk(timeout=0.0, max_samples=8192)
            n = len(samples)
            total += n
            if n < 8192:
                break
        return total

    def _close_inlet(self, key: str) -> None:
        inlet = self._inlets.get(key)
        if inlet is not None:
            try:
                inlet.close_stream()
            except Exception:  # noqa: BLE001
                pass
        self._inlets[key] = None


def _match_info(spec: StreamSpec, infos: Iterable[pylsl.StreamInfo]) -> pylsl.StreamInfo | None:
    needle = spec.name.lower()
    for info in infos:
        try:
            if needle not in info.name().lower():
                continue
            if spec.type is not None and info.type() != spec.type:
                continue
        except Exception:  # noqa: BLE001
            continue
        return info
    return None


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


@dataclass
class _HistoryEntry:
    lsl_time: float
    statuses: dict[str, StreamStatus] = field(default_factory=dict)


class StreamHealthMonitor:
    """Background thread that tracks per-stream health over time.

    Construct with the streams to expect, ``start()`` it, read
    ``snapshot()`` for live status, bracket a block with ``begin_window()``
    / ``window_report()`` to learn whether anything degraded during it,
    and ``stop()`` it at end of pass. Safe to ``stop()`` more than once.
    """

    def __init__(
        self,
        specs: Iterable[StreamSpec],
        poller: Poller | None = None,
        *,
        poll_interval_s: float = 1.0,
        silent_polls: int = 3,
        history_len: int = 1200,
    ) -> None:
        self._specs: tuple[StreamSpec, ...] = tuple(specs)
        self._poll_interval_s = poll_interval_s
        self._silent_polls = silent_polls
        self._poller: Poller = poller if poller is not None else LslPoller(self._specs)

        self._lock = threading.Lock()
        self._health: dict[str, StreamHealth] = {
            s.key: StreamHealth(spec=s) for s in self._specs
        }
        self._history: deque[_HistoryEntry] = deque(maxlen=history_len)

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ----- spec access ----------------------------------------------------

    @property
    def specs(self) -> tuple[StreamSpec, ...]:
        return self._specs

    def spec_by_key(self, key: str) -> StreamSpec | None:
        for s in self._specs:
            if s.key == key:
                return s
        return None

    # ----- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        if not self._specs:
            logger.info("StreamHealthMonitor: no specs — not starting")
            return
        self._thread = threading.Thread(
            target=self._run, name="StreamHealthMonitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.5, self._poll_interval_s * 2))
            self._thread = None
        try:
            self._poller.close()
        except Exception:  # noqa: BLE001
            logger.debug("poller.close failed", exc_info=True)

    def poll_once(self) -> None:
        """Run a single poll synchronously. Used by tests and on demand."""
        try:
            results = self._poller.poll()
        except Exception:  # noqa: BLE001
            logger.debug("poll failed", exc_info=True)
            results = {s.key: PollResult(present=False) for s in self._specs}
        now = pylsl.local_clock()
        with self._lock:
            self._apply(results, now)

    # ----- reads ----------------------------------------------------------

    def snapshot(self) -> list[StreamHealth]:
        """Current health of every stream (copies — safe to read off-thread)."""
        with self._lock:
            return [
                StreamHealth(
                    spec=h.spec,
                    status=h.status,
                    last_alive_lsl=h.last_alive_lsl,
                    new_samples_last_poll=h.new_samples_last_poll,
                    silent_polls=h.silent_polls,
                )
                for h in self._health.values()
            ]

    def all_alive(self) -> bool:
        with self._lock:
            return all(h.status is StreamStatus.ALIVE for h in self._health.values())

    def begin_window(self) -> float:
        """Return an opaque token marking the start of a block window."""
        return pylsl.local_clock()

    def window_report(self, token: float) -> dict[str, StreamStatus]:
        """Worst status seen for each stream since ``token``.

        If no poll landed inside the window (e.g. a window shorter than
        the poll interval), falls back to the current status.
        """
        with self._lock:
            worst: dict[str, StreamStatus] = {}
            for entry in self._history:
                if entry.lsl_time < token:
                    continue
                for key, status in entry.statuses.items():
                    cur = worst.get(key)
                    if cur is None or status.rank > cur.rank:
                        worst[key] = status
            for key, health in self._health.items():
                if key not in worst:
                    worst[key] = health.status
            return worst

    def window_report_values(self, token: float) -> dict[str, str]:
        """``window_report`` with plain string statuses — JSON/marker safe."""
        return {key: status.value for key, status in self.window_report(token).items()}

    # ----- internal -------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._poll_interval_s)

    def _apply(self, results: dict[str, PollResult], now: float) -> None:
        statuses: dict[str, StreamStatus] = {}
        for key, health in self._health.items():
            res = results.get(key, PollResult(present=False))
            status = self._derive(health, res)
            health.status = status
            health.new_samples_last_poll = res.new_samples
            if status is StreamStatus.ALIVE:
                health.last_alive_lsl = now
            statuses[key] = status
        self._history.append(_HistoryEntry(lsl_time=now, statuses=statuses))

    def _derive(self, health: StreamHealth, res: PollResult) -> StreamStatus:
        if not res.present:
            health.silent_polls = 0
            return StreamStatus.MISSING
        if not health.spec.expect_continuous:
            health.silent_polls = 0
            return StreamStatus.ALIVE
        if res.new_samples > 0:
            health.silent_polls = 0
            return StreamStatus.ALIVE
        health.silent_polls += 1
        if health.silent_polls >= self._silent_polls:
            return StreamStatus.SILENT
        return StreamStatus.ALIVE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def specs_from_stream_refs(refs: Iterable[object]) -> list[StreamSpec]:
    """Build monitor specs from ``config.StreamRef`` objects.

    A ref needs ``name`` and ``description``; ``type`` is optional. The
    spec key is ``"{name}|{type}"`` so two streams sharing a name (Muse
    EEG vs Muse PPG) get distinct keys. Streams typed ``Markers`` are
    presence-only (``expect_continuous=False``).
    """
    specs: list[StreamSpec] = []
    for ref in refs:
        name = getattr(ref, "name", None)
        if not name:
            continue
        rtype = getattr(ref, "type", None)
        desc = getattr(ref, "description", "") or ""
        expect_continuous = (rtype or "").lower() not in ("", "markers")
        specs.append(
            StreamSpec(
                key=f"{name}|{rtype or '*'}",
                name=name,
                type=rtype,
                description=desc,
                expect_continuous=expect_continuous,
            )
        )
    return specs


def all_alive(snapshot: Iterable[StreamHealth]) -> bool:
    return all(h.status is StreamStatus.ALIVE for h in snapshot)


def block_health_summary(blocks: Iterable[object]) -> list[str]:
    """One coverage line per block — the latest attempt of each block wins.

    ``blocks`` are objects exposing ``block_id``, ``attempt``, ``label`` and
    ``stream_health`` (a ``{stream_key: status_value}`` dict or None) — i.e.
    ``persistence.BlockRecord``. Used for the end-of-pass RA summary.
    """
    latest: dict[int, object] = {}
    for b in blocks:
        bid = getattr(b, "block_id", None)
        if bid is None:
            continue
        if bid not in latest or getattr(b, "attempt", 1) >= getattr(
            latest[bid], "attempt", 1
        ):
            latest[bid] = b
    lines: list[str] = []
    for bid in sorted(latest):
        b = latest[bid]
        health = getattr(b, "stream_health", None) or {}
        degraded = sorted(k for k, v in health.items() if v != "alive")
        attempt = getattr(b, "attempt", 1)
        note = f" (attempt {attempt})" if attempt > 1 else ""
        label = getattr(b, "label", "")
        if degraded:
            names = ", ".join(
                k.replace("|*", "").replace("|", " ") for k in degraded
            )
            lines.append(f"Block {bid} {label}{note}: DEGRADED — {names}")
        else:
            lines.append(f"Block {bid} {label}{note}: streams OK")
    return lines
