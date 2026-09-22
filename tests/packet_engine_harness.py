"""스니퍼 엔진 테스트 하네스 — DLL·스레드 없이 `PacketStateSource._capture()` 를 직접 돌린다.

`pcap_ffi`·`tcp_flow_map` 을 **모듈 네임스페이스 스텁**으로 통째 갈아끼우고(순수 함수는 실물에
위임), 스크립트된 가짜 핸들이 패킷·시계 전진·rc 를 차례로 내놓다가 소진되면 엔진을 멈춘다.
합성 Ethernet/IPv4/TCP 로 감싼 게임 프레임만 쓴다 — 실캡처 없음.

SEAssist 의 같은 하네스를 이식했다(PR-Y2'). 관측기에서 쓰는 부분만 가져왔다.
"""
from __future__ import annotations

import socket
import time
import unittest
from types import SimpleNamespace

from yuktracker.seassist import packet_state_source as pss
from yuktracker.seassist import pcap_ffi, tcp_flow_map
from yuktracker.seassist.packet_state_source import PacketStateSource

from unittest import mock

SERVER_IP = "121.9.9.9"
LOCAL_IP = "192.168.0.10"
#: 스텁 next_ex 가 돌려주는 벽시계 — 일부러 이질적인 값. 엔진이 이 값을 ts 로 쓰면
#: 발행 mono_ts 가 주입 단조시계와 달라져 즉시 발각된다.
BOGUS_WALL = 1.78e9


# ─────────────────────────── L2/L3/L4 빌더 ───────────────────────────


def wrap(payload: bytes, *, dst_port: int, seq: int, src_port: int = 8000,
         src_ip: str = SERVER_IP, dst_ip: str = LOCAL_IP, vlan: int = 0,
         ip_options: int = 0, tcp_options: int = 0, pad: int = 0,
         truncate: int = 0, frag: bool = False, proto: int = 6,
         version: int = 4) -> bytes:
    """게임 프레임 payload 를 Ethernet(+VLAN)/IPv4/TCP 로 감싼 합성 패킷."""
    ihl = 20 + ip_options
    dataofs = 20 + tcp_options
    total = ihl + dataofs + len(payload)
    parts = [b"\xaa" * 6 + b"\xbb" * 6]
    tpids = ([0x88A8, 0x8100] if vlan >= 2 else [0x8100])[:vlan] if vlan else []
    for t in tpids:
        parts.append(t.to_bytes(2, "big") + b"\x00\x01")
    parts.append((0x0800).to_bytes(2, "big"))
    ip = bytearray(ihl)
    ip[0] = (version << 4) | (ihl // 4)
    ip[2:4] = total.to_bytes(2, "big")
    if frag:
        ip[6] = 0x20  # MF 비트
    ip[8] = 64
    ip[9] = proto
    ip[12:16] = socket.inet_aton(src_ip)
    ip[16:20] = socket.inet_aton(dst_ip)
    for i in range(20, ihl):
        ip[i] = 0x01  # NOP 옵션
    tcp = bytearray(dataofs)
    tcp[0:2] = src_port.to_bytes(2, "big")
    tcp[2:4] = dst_port.to_bytes(2, "big")
    tcp[4:8] = seq.to_bytes(4, "big")
    tcp[12] = (dataofs // 4) << 4
    tcp[13] = 0x18  # PSH|ACK
    for i in range(20, dataofs):
        tcp[i] = 0x01
    # 패딩은 "그럴듯한 프레임 길이 프리픽스" 바이트 — caplen 기반 구현이면 유령 프레임으로 즉시 발각되게.
    pad_bytes = (b"\x0b\x00\x00" * pad)[:pad]
    frame = b"".join(parts) + bytes(ip) + bytes(tcp) + payload + pad_bytes
    return frame[: len(frame) - truncate] if truncate else frame


def _flow(pid: int, port: int, *, sip: str = SERVER_IP, sport: int = 8000) -> tcp_flow_map.GameFlow:
    return tcp_flow_map.GameFlow(pid=pid, local_ip=LOCAL_IP, local_port=port,
                                 server_ip=sip, server_port=sport)


# ─────────────────────────── 스텁 ───────────────────────────


class _FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, d: float) -> None:
        self.t += d


class _FakeHandle:
    """PcapHandle 대역 — 스크립트 구동.

    항목: bytes(패킷 rc=1) / ("advance", sec)(시계 전진) / ("rc", n) / callable(부작용 훅) /
    None(타임아웃 rc=0). 소진 시 engine stop (또는 endless_idle 이면 5ms 실수면 후 rc=0).
    """

    def __init__(self, clock: _FakeClock, script, *, endless_idle: bool = False) -> None:
        self.clock = clock
        self.script = list(script)
        self.endless_idle = endless_idle
        self.engine: PacketStateSource | None = None
        self.filters: list[str] = []
        self.events: list[str] = []
        self.filter_error = False
        self.last_error = ""

    def next_ex(self):
        while True:
            if not self.script:
                if self.endless_idle:
                    time.sleep(0.005)
                    return 0, BOGUS_WALL, b""
                if self.engine is not None:
                    self.engine._stop_event.set()
                return 0, BOGUS_WALL, b""
            item = self.script.pop(0)
            if callable(item):
                item()
                continue
            if item is None:
                return 0, BOGUS_WALL, b""
            if isinstance(item, tuple):
                kind, val = item
                if kind == "advance":
                    self.clock.advance(val)
                    continue
                if kind == "rc":
                    return val, BOGUS_WALL, b""
                raise AssertionError(f"알 수 없는 스크립트 항목: {item!r}")
            return 1, BOGUS_WALL, item

    def set_filter(self, bpf: str) -> None:
        self.events.append("set_filter")
        if self.filter_error:
            raise OSError("filter boom")
        self.filters.append(bpf)

    def breakloop(self) -> None:
        self.events.append("breakloop")

    def close(self) -> None:
        self.events.append("close")


class _PcapStub:
    """pcap_ffi 네임스페이스 대역 — 순수 함수는 실물 위임."""

    DEFAULT_TIMEOUT_MS = 100
    build_bpf = staticmethod(pcap_ffi.build_bpf)
    reason_message = staticmethod(pcap_ffi.reason_message)

    def __init__(self, handles=(), *, avail=(True, "ok"), device="dev0",
                 open_error: bool = False) -> None:
        self.handles = list(handles)
        self.avail = avail
        self.device = device
        self.open_error = open_error
        self.open_calls: list[str] = []
        self.PcapHandle = SimpleNamespace(open_live=self._open_live)

    def npcap_available(self):
        return self.avail

    def find_device_for_ip(self, _ip):
        return self.device

    def _open_live(self, dev, snaplen=65535, promisc=False, timeout_ms=100):
        self.open_calls.append(dev)
        if self.open_error:
            raise OSError("open boom")
        if not self.handles:
            raise OSError("스크립트된 핸들 없음")
        return self.handles.pop(0)


class _FlowStub:
    """tcp_flow_map 네임스페이스 대역 — 스냅샷 스크립트, 순수 함수는 실물 위임."""

    MAIN_PORT = tcp_flow_map.MAIN_PORT
    AUX_PORTS = tcp_flow_map.AUX_PORTS
    GameFlow = tcp_flow_map.GameFlow
    ambiguous_pids = staticmethod(tcp_flow_map.ambiguous_pids)
    derive_server_ip = staticmethod(tcp_flow_map.derive_server_ip)
    derive_server_ips = staticmethod(tcp_flow_map.derive_server_ips)

    def __init__(self, snapshots, *, stop_on_exhaust: bool = False) -> None:
        self.snapshots = [list(s) for s in snapshots]
        self.tail = list(self.snapshots[-1]) if self.snapshots else []
        self.stop_on_exhaust = stop_on_exhaust
        self.engine: PacketStateSource | None = None
        self.calls: list[set[int]] = []

    def find_game_flows(self, pids, **_kw):
        self.calls.append(set(pids))
        if self.snapshots:
            return list(self.snapshots.pop(0))
        if self.stop_on_exhaust and self.engine is not None:
            self.engine._stop_event.set()
        return list(self.tail)


class _EngineHarness(unittest.TestCase):
    """스레드 없이 `_capture` 를 직접 호출 — 스텁 스크립트가 종료를 결정."""

    def setUp(self) -> None:
        self.clock = _FakeClock()
        self.pids_box: dict[int, int] = {}
        self.status_lines: list[str] = []
        self.event_log: list[tuple[int, str, object]] = []
        self.engine = PacketStateSource(
            lambda: dict(self.pids_box),
            event_cb=self._on_event,
            status_cb=self.status_lines.append,
            now_fn=self.clock,
            retry_wait_sec=0.0,
        )

    def _on_event(self, idx: int, kind: str) -> None:
        # 발행 직후 호출 계약 — 이 시점 state_for 스냅샷이 곧 발행 레코드다.
        self.event_log.append((idx, kind, self.engine.state_for(idx)))

    def _patch(self, pcap_stub: _PcapStub, flow_stub: _FlowStub) -> None:
        flow_stub.engine = self.engine
        self.enterContext(mock.patch.object(pss, "pcap_ffi", pcap_stub))
        self.enterContext(mock.patch.object(pss, "tcp_flow_map", flow_stub))

    def _capture(self, script, snapshots, pids) -> _FakeHandle:
        self.pids_box.update(pids)
        handle = _FakeHandle(self.clock, script)
        handle.engine = self.engine
        self._patch(_PcapStub(), _FlowStub(snapshots))
        self.engine._capture(handle, SERVER_IP)
        return handle

    def _warns(self, token: str) -> list[str]:
        return [ln for ln in self.status_lines if token in ln]
