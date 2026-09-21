"""Bounded, per-connection wordinput state; no GUI, OCR or input side effects."""
from dataclasses import dataclass, replace
import threading

FLOW_STALE_SEC = 5.0
# A verified popup can be quiet while the user solves it. Bound that grace by
# the capture engine's 30-second frame-silence watchdog; real invalidations still
# revoke it immediately. Only an open/refresh observation renews this grace.
WORDINPUT_QUIET_SEC = 30.0
STARTUP_SCAN_SEC = 3.0
OPEN_SCAN_SEC = 25.0
IDLE_PROBE_SEC = 2.0


@dataclass(frozen=True)
class WordinputReading:
    pid: int
    flow: object
    epoch: object
    last_ts: float
    open_id: int = 0
    open_ts: float | None = None
    revision: int = 0
    refresh_ts: float | None = None
    last_observation: tuple | None = None


def _fresh(rec: WordinputReading, now: float) -> bool:
    age = now - rec.last_ts
    if age < 0:
        return False
    if age <= FLOW_STALE_SEC:
        return True
    return (rec.open_ts is not None and rec.last_observation is not None
            and 0 <= now - rec.last_observation[0] < WORDINPUT_QUIET_SEC)


class WordinputPackets:
    def __init__(self):
        self._lock = threading.Lock()
        self._slots: dict[int, WordinputReading] = {}

    def flow(self, slot, pid, flow, ts, *, ready):
        with self._lock:
            if not ready or pid <= 0:
                self._slots.pop(slot, None)
                return
            previous = self._slots.get(slot)
            if (previous is None or previous.pid != pid or previous.flow is not flow
                    or not _fresh(previous, ts)):
                self._slots[slot] = WordinputReading(pid, flow, object(), ts)
            else:
                self._slots[slot] = replace(previous, last_ts=ts)

    def note(self, slot, pid, flow, observation):
        with self._lock:
            rec = self._slots.get(slot)
            if rec is None or rec.pid != pid or rec.flow is not flow:
                return
            identity = (observation.ts, observation.kind, observation.body)
            if rec.last_observation == identity:
                return
            if observation.kind == "open":
                rec = replace(rec, open_id=rec.open_id + 1, open_ts=observation.ts)
            elif observation.kind == "refresh":
                rec = replace(rec, refresh_ts=observation.ts)
            else:
                return
            self._slots[slot] = replace(rec, revision=rec.revision + 1,
                                        last_observation=identity)

    def snapshot(self, slot, pid, now):
        with self._lock:
            rec = self._slots.get(slot)
            if rec is None or rec.pid != pid or not _fresh(rec, now):
                return None
            return rec

    def invalidate(self, slot):
        with self._lock:
            self._slots.pop(slot, None)

    def reset(self):
        with self._lock:
            self._slots.clear()


@dataclass
class _ScanState:
    epoch: object
    until: float
    next_probe: float
    consumed: int = 0
    needs_clear: bool = False
    confirmation_left: int | None = None


class PacketScanGate:
    """Packet-first scans with occasional screen probes for a missed open."""
    def __init__(self):
        self._slots = {}
        self._after = 0.0

    def reset(self, *, after=0.0):
        self._slots.clear()
        self._after = after

    def pending(self, slot, reading, now):
        if reading is None or reading.open_ts is None:
            return False
        state = self._slots.get(slot)
        consumed = (state.consumed if state is not None
                    and state.epoch is reading.epoch else 0)
        return (reading.open_id > consumed and reading.open_ts >= self._after
                and 0 <= now - reading.open_ts <= OPEN_SCAN_SEC)

    def scan(self, slot, reading, now):
        if reading is None:
            self._slots.pop(slot, None)
            return True  # absent/unhealthy capture uses the existing screen path
        state = self._slots.get(slot)
        if state is None or state.epoch is not reading.epoch:
            until = now + STARTUP_SCAN_SEC
            state = _ScanState(reading.epoch, until, until + IDLE_PROBE_SEC)
            self._slots[slot] = state
        if (now < state.until or self.pending(slot, reading, now)
                or (state.confirmation_left or 0) > 0):
            return True
        if now >= state.next_probe:
            state.next_probe = now + IDLE_PROBE_SEC
            state.confirmation_left = None
            return True
        return False

    def screen_result(self, slot, reading, now, *, screen_state, debounce_count=1):
        """Keep positive probes contiguous for debounce; rearm only after closure."""
        if reading is None:
            return True
        state = self._slots[slot]  # scan() established this connection's state
        if state.needs_clear:
            if screen_state != "absent" and not self.pending(slot, reading, now):
                return False
            state.needs_clear = False
        if screen_state == "present":
            if state.confirmation_left is None:
                state.confirmation_left = max(0, debounce_count - 1)
            elif state.confirmation_left > 0:
                state.confirmation_left -= 1
        else:
            state.confirmation_left = None
        if screen_state == "present" and now >= state.until:
            # One isolated sample every two seconds would be reset by the runner
            # before reaching its normal debounce threshold. Allow a bounded
            # confirmation burst, preserving the configured sample count even
            # with slow polls/captures, then leave an idle interval on rejection.
            state.until = now + STARTUP_SCAN_SEC
            state.next_probe = state.until + IDLE_PROBE_SEC
        return True

    def consume(self, slot, reading):
        if reading is not None:
            # Continue probing for disappearance, but never submit the same
            # still-visible popup twice just because the next probe is due.
            state = self._slots.get(slot)
            if state is None or state.epoch is not reading.epoch:
                state = _ScanState(reading.epoch, 0, reading.last_ts + IDLE_PROBE_SEC)
                self._slots[slot] = state
            state.until = 0
            state.consumed = reading.open_id
            state.needs_clear = True
            state.confirmation_left = None
