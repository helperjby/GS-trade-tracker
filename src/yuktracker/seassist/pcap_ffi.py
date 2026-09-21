"""Npcap(libpcap) ctypes 바인딩 — 라이브 패킷 캡처 (2026-07-30, 패킷 감지 P2).

전투 진입/종료를 화면 캡처 대신 서버 패킷으로 판정하기 위한 최하층. 이 모듈은 **바이트와
시각만** 돌려준다 — Ethernet/IP/TCP 파싱도, 프레이밍도, 슬롯 매핑도 하지 않는다.

새 pip 의존성을 만들지 않으려고 scapy 를 쓰지 않고 `wpcap.dll` 을 직접 바인딩한다
(onefile exe 용량 + 무서명 AV 오탐 회피). 레포는 이미 ctypes 관용구가 지배적이다.

**하지 않기로 한 것**
- **모듈 최상단에서 DLL 을 로드하지 않는다.** `window_manager` 의 최상단 싱글턴 관례와
  의도적으로 다르다 — user32 는 항상 있지만 wpcap 은 아예 없을 수 있고, 최상단 `CDLL` 은
  `import src.core.pcap_ffi` 자체를 미설치 PC 와 CI 러너에서 터뜨린다. 지연 싱글턴을 쓴다.
- **`tcp_flow_map` 을 import 하지 않는다.** 레이어 관계가 아니다. 합성은 소비자가 한다:
  `find_game_flows()` → `find_device_for_ip()` → `build_bpf()`.
- **미설치를 예외로 다루지 않는다.** `npcap_available()` 이 `(False, 사유토큰)` 을 돌려주고
  호출자는 조용히 화면 감지로 강등한다. `not_elevated` 도 같은 등급이다 — `src/main.py` 가
  UAC 거부 시 "일반 권한으로 계속" 을 제공하므로 비승격 실행이 정상 경로다.
- **헤더 타임스탬프를 이벤트 시각으로 쓰지 않는다.** `next_ex()` 가 돌려주는 시각은
  `pcap_next_ex` 반환 직후의 `time.time()` 이다. 헤더 ts 는 `last_hdr_ts` 로 **진단용**
  으로만 노출한다(클럭 오프셋 측정 — 형제 프로젝트 `17. Packet study` 선례).
- **`pcap_pkthdr` 레이아웃을 상수로 박지 않는다.** 아래 자가보정 참조.

`pcap_pkthdr` 자가보정 — 이 파일에서 제일 중요한 부분
-----------------------------------------------------
흔히 쓰는 "Windows timeval = 32비트 long 2개 → pcap_pkthdr 16바이트" 가정은 **Npcap
1.80+ 에서 틀리다**(64-bit time_t 빌드). 틀려도 **예외가 나지 않고 조용히 쓰레기 길이**를
읽어 패킷 슬라이스가 깨진다 — 디버깅 최악의 부류다. 그래서:

1. 후보 3종을 둔다. `tv64` 와 `tv64_usec32` 는 sizeof 가 24 로 같아 **크기로는 못 가른다**
   (MSVC 에서 `long` 은 32비트라 `struct timeval {time_t tv_sec; long tv_usec;}` 가 실제로
   가능하다). 2종만 두면 실제가 `tv64_usec32` 일 때 전 패킷이 드랍되어 기능이 사망한다.
   ⚠️ **2026-07-30 실측에서 이 장치가 실제로 일했다** — Npcap 1.87 이 버전 문자열에
   "64-bit time_t" 를 달고 나오는데도 진짜 배치는 **`tv32`(16바이트)** 였다. 문자열을
   믿고 상수를 박았다면 전 패킷을 오독했을 것이다. `default_layout_order()` 참조.
2. 첫 몇 패킷에서 `0 < caplen <= snaplen`, `caplen <= wirelen`, wirelen 상한, 에폭 건전성,
   `0 <= tv_usec < 1e6` 을 모두 만족하는 후보로 합의 확정한다. 오독은 서로 반대 방향으로
   깨진다 — tv32 바이트를 tv64 로 읽으면 초가 에폭에서 광년 이탈하고, tv64 를 tv32 로
   읽으면 `len` 자리에 tv_usec 상위 32비트(=0)가 와서 `caplen <= wirelen` 이 무너진다.
3. **진짜 안전망은 매 패킷의 `admit_caplen` 클램프다.** 보정이 틀려도 결말은 "조용한
   쓰레기 읽기" 가 아니라 "드랍 + 카운터 + 경고 1줄" 이다.
"""
from __future__ import annotations

import ctypes
import ipaddress
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Sequence

from .admin import is_admin
from .logger import get_logger

# ─────────────────────────── 상수 ───────────────────────────

#: 가용성 사유 토큰. **한국어 문장이 아니라 토큰**인 이유는 후속 GUI/러너가 이걸로
#: 분기하기 때문이다 — 문자열 분기는 오타 하나로 조용히 깨진다.
REASON_OK = "ok"
REASON_NO_DLL = "npcap_not_installed"
REASON_NO_SYMBOL = "wpcap_symbol_missing"
REASON_DRIVER = "npcap_driver_unavailable"
REASON_NO_DEVICE = "no_capture_device"
REASON_NOT_ELEVATED = "not_elevated"

REASON_MESSAGES: dict[str, str] = {
    REASON_OK: "Npcap 사용 가능",
    REASON_NO_DLL: "Npcap 미설치 — 화면 감지를 계속 사용합니다",
    REASON_NO_SYMBOL: "wpcap.dll 이 예상 심볼을 제공하지 않습니다 (구버전 WinPcap?)",
    REASON_DRIVER: "Npcap 드라이버가 응답하지 않습니다 (서비스 중지 가능성)",
    REASON_NO_DEVICE: "캡처 가능한 네트워크 어댑터가 없습니다",
    REASON_NOT_ELEVATED: "관리자 권한이 아닙니다 — 화면 감지를 계속 사용합니다",
}

_PCAP_ERRBUF_SIZE = 256
_PCAP_NETMASK_UNKNOWN = 0xFFFFFFFF
_PCAP_IF_LOOPBACK = 0x00000001
_AF_INET = 2

#: `to_ms`. 패킷이 없어도 이 주기로 리턴하므로 stop_event 반응성이 확보된다.
DEFAULT_TIMEOUT_MS = 100
#: `pcap_setmintocopy` 를 못 쓸 때의 강등값 — 커널 버퍼가 to_ms 로만 비워지기 때문.
FALLBACK_TIMEOUT_MS = 10

#: 후보 레이아웃 중 최대 크기. 헤더 스냅샷을 이만큼 뜬다.
_HDR_PROBE_BYTES = 24
#: Windows LSO/GSO 코얼레싱 여유를 둔 wirelen 상한.
_MAX_PLAUSIBLE_WIRE_LEN = 262144
#: 헤더 초가 현재 시각에서 이만큼 이상 벗어나면 오독으로 본다.
_TS_SANITY_WINDOW_SEC = 86400.0
_CALIBRATION_MAX_SAMPLES = 8
_CALIBRATION_MIN_AGREE = 3

_NPCAP_DIR = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"), "System32", "Npcap"
)

_REQUIRED_SYMBOLS = (
    "pcap_findalldevs",
    "pcap_freealldevs",
    "pcap_open_live",
    "pcap_compile",
    "pcap_setfilter",
    "pcap_freecode",
    "pcap_next_ex",
    "pcap_breakloop",
    "pcap_close",
    "pcap_geterr",
    "pcap_lib_version",
)
#: 있으면 쓰고 없으면 우회하는 심볼. `pcap_setmintocopy` 는 Npcap 확장(없으면 to_ms 강등),
#: `pcap_snapshot` 은 libpcap 표준이지만 없어도 요청 snaplen 으로 폴백 가능하다.
_OPTIONAL_SYMBOLS = ("pcap_setmintocopy", "pcap_snapshot")

#: 드랍이 지속될 때 경고를 다시 낼 최소 간격 — "조용히 0 패킷" 을 막되 도배는 안 한다.
_DROP_WARN_THROTTLE_SEC = 60.0
#: 이만큼 연속으로 드랍되면 경고. 보정 구간(최대 8패킷)은 정상이라 그보다 넉넉히 잡는다.
_DROP_WARN_STREAK = 32


# ────────────────── [A] 순수 계층 (ctypes 무의존) ──────────────────


@dataclass(frozen=True)
class PkthdrLayout:
    """`struct pcap_pkthdr` 의 후보 메모리 배치. tv_sec 는 항상 offset 0."""

    name: str
    ts_size: int
    usec_off: int
    usec_size: int
    caplen_off: int
    len_off: int
    size: int


LAYOUT_TV32 = PkthdrLayout("tv32", 4, 4, 4, 8, 12, 16)
LAYOUT_TV64 = PkthdrLayout("tv64", 8, 8, 8, 16, 20, 24)
#: `{int64 tv_sec; long tv_usec;}` — 중첩 timeval 이 8바이트 정렬이라 tv_usec 뒤에 4바이트
#: tail padding 이 붙는다. 따라서 caplen/len 오프셋은 `tv64` 와 **같고**, 다른 것은 usec 폭
#: 하나뿐이다(그 padding 이 0 이 아닐 때 `tv64` 는 usec 과대로 기각되지만 이쪽은 통과).
#: ⚠️ 초안은 caplen@12/len@16 이었는데 그건 `#pragma pack(4)` 에서만 나오는 배치이고
#: 그러면 sizeof 가 20 이라 자기모순이었다 — 어떤 실제 ABI 와도 맞지 않는 죽은 후보였다.
LAYOUT_TV64_USEC32 = PkthdrLayout("tv64_usec32", 8, 8, 4, 16, 20, 24)

_LAYOUTS = (LAYOUT_TV32, LAYOUT_TV64, LAYOUT_TV64_USEC32)


def _fields(raw: bytes, layout: PkthdrLayout) -> tuple[int, int, int, int] | None:
    """(tv_sec, tv_usec, caplen, wirelen). 버퍼가 짧으면 None."""
    if len(raw) < layout.len_off + 4:
        return None
    sec = int.from_bytes(raw[0 : layout.ts_size], "little")
    usec = int.from_bytes(
        raw[layout.usec_off : layout.usec_off + layout.usec_size], "little"
    )
    caplen = int.from_bytes(raw[layout.caplen_off : layout.caplen_off + 4], "little")
    wirelen = int.from_bytes(raw[layout.len_off : layout.len_off + 4], "little")
    return sec, usec, caplen, wirelen


def decode_header(raw: bytes, layout: PkthdrLayout) -> tuple[float, int, int] | None:
    """(ts, caplen, wirelen) 또는 None. ts 는 진단용 — 이벤트 시각으로 쓰지 말 것."""
    f = _fields(raw, layout)
    if f is None:
        return None
    sec, usec, caplen, wirelen = f
    return sec + usec / 1_000_000.0, caplen, wirelen


def layout_plausible(
    raw: bytes, layout: PkthdrLayout, snaplen: int, now: float
) -> bool:
    """이 레이아웃으로 읽은 헤더가 물리적으로 말이 되는가."""
    f = _fields(raw, layout)
    if f is None:
        return False
    sec, usec, caplen, wirelen = f
    if not 0 < caplen <= snaplen:
        return False
    if caplen > wirelen:
        return False
    if wirelen > _MAX_PLAUSIBLE_WIRE_LEN:
        return False
    if not 0 <= usec < 1_000_000:
        return False
    return abs(sec - now) <= _TS_SANITY_WINDOW_SEC


def default_layout_order() -> tuple[PkthdrLayout, ...]:
    """보정이 가려내지 못했을 때 채택할 순서. 1순위는 `tv32`.

    ⚠️ **2026-07-30 실측이 초안 가설을 뒤집었다.** Npcap 1.87 은
    `"Npcap version 1.87, based on libpcap version 1.10.6 (64-bit time_t)"` 를
    보고하지만, 실제 `pcap_pkthdr` 는 **tv32(16바이트)** 였다 (라이브 20패킷,
    보정 `measured`, 헤더 ts 와 벽시계 오차 0.04ms — 오독이면 나올 수 없는 값).

    이유: Windows 의 `struct timeval` 은 `{long tv_sec; long tv_usec;}` 이고 MSVC 에서
    `long` 은 **time_t 폭과 무관하게 32비트**다. 즉 버전 문자열의 "64-bit time_t" 는
    헤더 배치의 근거가 되지 못한다 — 그걸 근거로 tv64 를 1순위로 두려던 초안은
    틀렸고, 보정기가 없었다면 전 패킷을 오독했을 것이다.

    그래서 문자열로 순서를 정하지 않는다. 넓은 후보 2종은 남겨 두되(다른 빌드가
    존재할 가능성) 순서는 실측된 사실을 따른다.
    """
    return _LAYOUTS


class LayoutCalibrator:
    """첫 몇 패킷으로 `pcap_pkthdr` 배치를 결정한다. 결정 후에는 멱등."""

    def __init__(
        self,
        order: Sequence[PkthdrLayout] | None = None,
        *,
        max_samples: int = _CALIBRATION_MAX_SAMPLES,
        min_agree: int = _CALIBRATION_MIN_AGREE,
    ) -> None:
        self._order = tuple(order) if order else _LAYOUTS
        self._max_samples = max_samples
        self._min_agree = min_agree
        self._pass = {ly.name: 0 for ly in self._order}
        self._fail = {ly.name: 0 for ly in self._order}
        self.samples = 0
        self.decided: PkthdrLayout | None = None
        #: "measured" = 실측 합의, "assumed" = 표본 소진 후 hint 채택.
        self.confidence = "pending"

    def feed(self, raw: bytes, snaplen: int, now: float) -> PkthdrLayout | None:
        if self.decided is not None:
            return self.decided
        self.samples += 1
        for ly in self._order:
            if layout_plausible(raw, ly, snaplen, now):
                self._pass[ly.name] += 1
            else:
                self._fail[ly.name] += 1
        for ly in self._order:
            if self._pass[ly.name] < self._min_agree:
                continue
            # 가려냈다고 하려면 **슬라이스가 달라지는** 다른 후보들이 전부 한 번 이상
            # 실패해야 한다. caplen/len 오프셋이 같은 후보끼리는 어느 쪽을 골라도
            # 페이로드가 동일하므로(차이는 진단용 ts 정밀도뿐) 모호해도 무해하다 —
            # 그걸 요구하면 tv64 ↔ tv64_usec32 가 영영 확정되지 않고 assumed 로 떨어진다.
            if all(
                self._fail[o.name] > 0
                for o in self._order
                if (o.caplen_off, o.len_off) != (ly.caplen_off, ly.len_off)
            ):
                self.decided = ly
                self.confidence = "measured"
                return ly
        if self.samples >= self._max_samples:
            self.decided = self._order[0]
            self.confidence = "assumed"
            return self.decided
        return None


def admit_caplen(caplen: int, wirelen: int, snaplen: int) -> int | None:
    """슬라이스에 쓸 수 있는 길이인지 최종 관문. 아니면 None(=드랍).

    보정이 틀렸을 때 거대/음수 슬라이스가 나가는 것을 여기서 막는다.
    """
    if caplen <= 0 or caplen > snaplen or caplen > wirelen:
        return None
    return caplen


def build_bpf(host: str | Sequence[str], ports: Sequence[int]) -> str:
    """`tcp and host <ip> and (port A or port B)` — 호스트 여럿이면
    `tcp and (host A or host B) and (port …)` (패킷 PR-D).

    호스트를 IPv4 로 검증해 필터 문법 주입을 막고, 호스트·포트를 정렬해 문자열을
    결정적으로 만든다(호출자가 "필터가 바뀌었나?" 를 문자열 비교로 판단할 수 있게).
    호스트 1개는 문자열이든 길이 1 시퀀스든 **같은 문자열**을 낸다(기존 핀 불변).

    로컬 포트는 넣지 않는다 — 재접속마다 필터를 다시 컴파일해야 하고 그 창에 패킷을
    놓친다. 서버 IP + 게임 포트면 클라 3개를 합쳐도 트래픽이 미미하다.
    """
    hosts = [host] if isinstance(host, str) else sorted({str(h) for h in host})
    if not hosts:
        raise ValueError("호스트가 비어 있습니다")
    for h in hosts:
        ipaddress.IPv4Address(h)  # 실패 시 ValueError
    ps = sorted({int(p) for p in ports})
    if not ps:
        raise ValueError("포트가 비어 있습니다")
    if any(not 0 < p < 65536 for p in ps):
        raise ValueError(f"포트 범위 밖: {ps}")
    joined = " or ".join(f"port {p}" for p in ps)
    if len(hosts) == 1:
        return f"tcp and host {hosts[0]} and ({joined})"
    hj = " or ".join(f"host {h}" for h in hosts)
    return f"tcp and ({hj}) and ({joined})"


def sockaddr_to_ipv4(raw: bytes) -> str | None:
    """`sockaddr` 바이트에서 IPv4 문자열. AF_INET 이 아니거나 짧으면 None."""
    if len(raw) < 8:
        return None
    if int.from_bytes(raw[0:2], "little") != _AF_INET:
        return None
    return socket.inet_ntoa(raw[4:8])


def decode_c_str(raw: bytes | str | None) -> str:
    """C 문자열 디코드 — 어떤 바이트에도 예외를 던지지 않는다.

    어댑터 description 은 레지스트리 유래라 지역화 문자열("이더넷")이 올 수 있다.
    strict UTF-8 로 읽으면 한국어 Windows 에서 **디바이스 열거 전체가 죽는다**.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "mbcs", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    description: str
    ipv4s: tuple[str, ...]
    flags: int


def pick_device_for_ip(
    devices: Sequence[DeviceInfo], local_ip: str
) -> DeviceInfo | None:
    """주어진 로컬 IP 를 가진 캡처 디바이스.

    GUID 하드코딩 금지 — 어댑터가 바뀌면 **조용히 0 패킷**이 된다. 주소가 없는 WAN
    Miniport, 다른 대역의 VPN 터널, loopback 은 IP 불일치로 자동 탈락한다.
    """
    cands = [d for d in devices if local_ip in d.ipv4s]
    cands.sort(key=lambda d: (bool(d.flags & _PCAP_IF_LOOPBACK), d.name))
    return cands[0] if cands else None


def reason_message(reason: str) -> str:
    return REASON_MESSAGES.get(reason, reason)


def _dll_candidates() -> tuple[str, ...]:
    """로드 시도 순서.

    Npcap 전용 폴더를 **먼저** 보는 이유는 탐색 경로 문제가 아니다(CPython 3.8+ 는 경로가
    섞인 이름에 `LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR` 를 자동으로 붙여 옆의 `Packet.dll` 을
    찾아준다). `System32\\wpcap.dll` 이 **레거시 WinPcap 4.1.3 일 수 있어서**다 —
    그걸 집으면 pkthdr 배치도 기능도 구식이 된다.
    """
    return (os.path.join(_NPCAP_DIR, "wpcap.dll"), "wpcap.dll")


# ────────────────── [B] ctypes 타입 (DLL 없이 정의된다) ──────────────────


class _pcap_addr(ctypes.Structure):
    pass


_pcap_addr._fields_ = [
    ("next", ctypes.POINTER(_pcap_addr)),
    ("addr", ctypes.c_void_p),
    ("netmask", ctypes.c_void_p),
    ("broadaddr", ctypes.c_void_p),
    ("dstaddr", ctypes.c_void_p),
]


class _pcap_if(ctypes.Structure):
    pass


_pcap_if._fields_ = [
    ("next", ctypes.POINTER(_pcap_if)),
    ("name", ctypes.c_char_p),
    ("description", ctypes.c_char_p),
    ("addresses", ctypes.POINTER(_pcap_addr)),
    ("flags", ctypes.c_uint),
]


class _bpf_program(ctypes.Structure):
    """⚠️ 배치를 틀리면 `pcap_freecode` 가 쓰레기 포인터를 free 해 힙이 손상된다.

    x64 에서는 `bf_len` 뒤에 4바이트 패딩이 자동으로 들어가 총 16바이트가 된다.
    """

    _fields_ = [("bf_len", ctypes.c_uint), ("bf_insns", ctypes.c_void_p)]


def _walk_pcap_if(head) -> list[DeviceInfo]:
    """`pcap_findalldevs` 연결리스트 → DeviceInfo 목록. 순수 포인터 워크."""
    out: list[DeviceInfo] = []
    node = head
    while node:
        p = node.contents
        ips: list[str] = []
        addr = p.addresses
        while addr:
            a = addr.contents
            if a.addr:
                ip = sockaddr_to_ipv4(ctypes.string_at(a.addr, 16))
                if ip:
                    ips.append(ip)
            addr = a.next
        out.append(
            DeviceInfo(
                name=decode_c_str(p.name),
                description=decode_c_str(p.description),
                ipv4s=tuple(ips),
                flags=int(p.flags),
            )
        )
        node = p.next
    return out


# ────────────────── [C] 지연 로더 ──────────────────

_lib: ctypes.CDLL | None = None
_lib_lock = threading.RLock()
_load_attempted = False
_missing_symbols: tuple[str, ...] = ()
_has_mintocopy = False


def _bind(lib: ctypes.CDLL) -> tuple[str, ...]:
    """argtypes/restype 를 지정하고 없는 필수 심볼 목록을 돌려준다."""
    missing: list[str] = []
    for name in _REQUIRED_SYMBOLS:
        if not hasattr(lib, name):
            missing.append(name)
    if missing:
        return tuple(missing)

    lib.pcap_findalldevs.argtypes = (
        ctypes.POINTER(ctypes.POINTER(_pcap_if)),
        ctypes.c_char_p,
    )
    lib.pcap_findalldevs.restype = ctypes.c_int
    lib.pcap_freealldevs.argtypes = (ctypes.POINTER(_pcap_if),)
    lib.pcap_freealldevs.restype = None
    lib.pcap_open_live.argtypes = (
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_char_p,
    )
    lib.pcap_open_live.restype = ctypes.c_void_p
    lib.pcap_compile.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_bpf_program),
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_uint,
    )
    lib.pcap_compile.restype = ctypes.c_int
    lib.pcap_setfilter.argtypes = (ctypes.c_void_p, ctypes.POINTER(_bpf_program))
    lib.pcap_setfilter.restype = ctypes.c_int
    lib.pcap_freecode.argtypes = (ctypes.POINTER(_bpf_program),)
    lib.pcap_freecode.restype = None
    lib.pcap_next_ex.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    )
    lib.pcap_next_ex.restype = ctypes.c_int
    lib.pcap_breakloop.argtypes = (ctypes.c_void_p,)
    lib.pcap_breakloop.restype = None
    lib.pcap_close.argtypes = (ctypes.c_void_p,)
    lib.pcap_close.restype = None
    lib.pcap_geterr.argtypes = (ctypes.c_void_p,)
    lib.pcap_geterr.restype = ctypes.c_char_p
    lib.pcap_lib_version.argtypes = ()
    lib.pcap_lib_version.restype = ctypes.c_char_p

    global _has_mintocopy
    _has_mintocopy = hasattr(lib, "pcap_setmintocopy")
    if _has_mintocopy:
        lib.pcap_setmintocopy.argtypes = (ctypes.c_void_p, ctypes.c_int)
        lib.pcap_setmintocopy.restype = ctypes.c_int
    if hasattr(lib, "pcap_snapshot"):
        lib.pcap_snapshot.argtypes = (ctypes.c_void_p,)
        lib.pcap_snapshot.restype = ctypes.c_int
    return ()


def _load_wpcap() -> ctypes.CDLL | None:
    """wpcap.dll 지연 로드. 실패는 예외가 아니라 None. 프로세스 1회만 시도."""
    global _lib, _load_attempted, _missing_symbols
    with _lib_lock:
        if _load_attempted:
            return _lib
        _load_attempted = True
        for cand in _dll_candidates():
            try:
                # ⚠️ libpcap 은 cdecl 이므로 CDLL 이다 (WinDLL 아님). x64 에서는 두 규약이
                # 사실상 같아 WinDLL 로도 "동작해버리는" 게 함정 — 32-bit 에서 즉사한다.
                if os.path.isdir(_NPCAP_DIR):
                    with os.add_dll_directory(_NPCAP_DIR):
                        lib = ctypes.CDLL(cand)
                else:
                    lib = ctypes.CDLL(cand)
                missing = _bind(lib)
                if missing:
                    _missing_symbols = missing
                    continue
                _missing_symbols = ()
                _lib = lib
                return _lib
            except Exception:
                continue
        return None


def _reset_load_cache() -> None:
    """테스트 전용 — 로더/가용성 캐시를 비운다."""
    global _lib, _load_attempted, _missing_symbols, _has_mintocopy
    with _lib_lock:
        _lib = None
        _load_attempted = False
        _missing_symbols = ()
        _has_mintocopy = False
    _reset_availability_cache()


# ────────────────── [D] 가용성 프로브 ──────────────────

_avail_cache: tuple[bool, str] | None = None
_avail_lock = threading.Lock()


def _reset_availability_cache() -> None:
    """테스트 전용 (그리고 미래의 [재검사] 버튼용)."""
    global _avail_cache
    with _avail_lock:
        _avail_cache = None


def _probe_availability() -> tuple[bool, str]:
    """근본 원인을 먼저 본다 — 승격 여부는 **마지막**."""
    try:
        lib = _load_wpcap()
    except Exception:
        return False, REASON_NO_DLL
    if lib is None:
        return False, REASON_NO_SYMBOL if _missing_symbols else REASON_NO_DLL
    try:
        rc, devices = _findalldevs()
    except Exception:
        return False, REASON_DRIVER
    if rc != 0:
        return False, REASON_DRIVER
    if not devices:
        return False, REASON_NO_DEVICE
    if not is_admin():
        return False, REASON_NOT_ELEVATED
    return True, REASON_OK


def npcap_available() -> tuple[bool, str]:
    """(라이브 캡처가 될 것으로 기대되는가, 사유 토큰). 프로세스 1회 캐시.

    `pcap_findalldevs` 는 비관리자에서도 동작하므로 GUI 부팅 시점에 안전하게 부를 수 있다
    (실측 확인).

    ⚠️ `not_elevated` 는 **보수적인 게이트지 물리적 제약이 아니다.** 2026-07-30 실측에서
    비승격 셸의 `pcap_open_live` 가 성공했다 (Npcap 설치 시 "관리자 전용" 을 끄면 그렇게
    된다). 그래도 False 로 두는 이유: 매크로가 비승격이면 게임 창 캡처 자체가 UIPI 로
    막혀(2026-07-29 실측 capfail 56/56) 어차피 정상 운용이 아니고, 운영 경로는 항상
    UAC 승격이라 이 분기를 타지 않는다.
    """
    global _avail_cache
    with _avail_lock:
        if _avail_cache is not None:
            return _avail_cache
    result = _probe_availability()
    with _avail_lock:
        _avail_cache = result
    return result


# ────────────────── [E] 디바이스 열거 ──────────────────


def _findalldevs() -> tuple[int, list[DeviceInfo]]:
    lib = _load_wpcap()
    if lib is None:
        return -1, []
    head = ctypes.POINTER(_pcap_if)()
    errbuf = ctypes.create_string_buffer(_PCAP_ERRBUF_SIZE)
    rc = int(lib.pcap_findalldevs(ctypes.byref(head), errbuf))
    if rc != 0:
        return rc, []
    if not head:
        return 0, []
    try:
        return 0, _walk_pcap_if(head)
    finally:
        try:
            lib.pcap_freealldevs(head)
        except Exception:
            pass


def list_devices() -> list[tuple[str, str]]:
    """(디바이스명, 설명) 목록. Npcap 이 없으면 빈 목록."""
    _rc, devices = _findalldevs()
    return [(d.name, d.description) for d in devices]


def device_infos() -> list[DeviceInfo]:
    _rc, devices = _findalldevs()
    return devices


def find_device_for_ip(local_ip: str) -> str | None:
    """로컬 IP 를 가진 캡처 디바이스명 (`\\Device\\NPF_{...}`)."""
    dev = pick_device_for_ip(device_infos(), local_ip)
    return dev.name if dev else None


# ────────────────── [F] 캡처 핸들 ──────────────────


def effective_snaplen(lib, ptr: int, requested: int) -> int:
    """핸들이 실제로 쓰는 snaplen. `pcap_snapshot()` 이 있으면 그 값을 믿는다.

    libpcap 은 `snaplen <= 0` 을 262144 로 승격하는 등 요청값을 조용히 바꾼다. 요청값을
    그대로 들고 있으면 `admit_caplen` 의 상한이 실제보다 작아져 **전 패킷이 드랍**된다
    (요청 0 으로 열면 100% 재현). 심볼이 없거나 비정상 값이면 요청값으로 폴백.
    """
    try:
        got = int(lib.pcap_snapshot(ptr))
    except Exception:
        return requested
    if 0 < got <= _MAX_PLAUSIBLE_WIRE_LEN:
        return got
    return requested


class PcapHandle:
    """라이브 캡처 핸들 1개.

    **종료 순서**: `breakloop()` → 캡처 스레드 `join()` → `close()`.
    `close()` 는 `next_ex()` 와 상호배제되므로 최대 `timeout_ms` 만큼 기다릴 수 있다.
    이 순서를 어기고 캡처 스레드가 도는 중에 `close()` 하면 해제된 포인터로 복귀할
    위험이 있다 — 락과 `_closed` 가드가 막지만 순서를 지키는 편이 옳다.

    정지 응답성의 진실 원천은 `breakloop` 이 아니라 **`timeout_ms` 주기 폴**이다.
    `breakloop` 은 best-effort 로만 취급한다.
    """

    def __init__(
        self,
        ptr: int,
        snaplen: int,
        timeout_ms: int,
        lib: ctypes.CDLL,
        *,
        calibrator: LayoutCalibrator | None = None,
    ) -> None:
        self._ptr: int | None = ptr
        self._lib = lib
        self._snaplen = snaplen
        # `_lock`: next_ex/set_filter 직렬화 (최대 timeout_ms 만큼 잡힌다).
        # `_ptr_lock`: 포인터 수명 전용 — 항상 짧게만 잡히므로 breakloop 이 블록되지
        # 않으면서도 close 와의 use-after-free 경합이 닫힌다.
        # 락 순서는 항상 `_lock` → `_ptr_lock` (역순 취득 경로 없음).
        self._lock = threading.Lock()
        self._ptr_lock = threading.Lock()
        self._closed = False
        # 보정기를 주입받는다 — hint 순서를 DLL 조회에 의존시키면 테스트가 환경에 따라
        # 다른 순서로 돌게 된다.
        self._calib = calibrator or LayoutCalibrator()
        self.timeout_ms = timeout_ms
        self.layout: PkthdrLayout | None = None
        self.layout_confidence = "pending"
        #: 진단 전용 — 이벤트 시각으로 쓰지 말 것 (클럭 오프셋 측정용).
        self.last_hdr_ts = 0.0
        self.last_error = ""
        self.dropped_uncalibrated = 0
        self.dropped_bad_hdr = 0
        self.mintocopy_ok = False
        self._drop_streak = 0
        self._last_drop_warn_ts = 0.0

    @staticmethod
    def open_live(
        dev: str,
        snaplen: int = 65535,
        promisc: bool = False,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> "PcapHandle":
        """캡처 시작. 실패 시 OSError.

        `pcap_setmintocopy` 가 없으면 `timeout_ms` 를 낮춘다 — Npcap 은 기본적으로
        커널 버퍼에 16KB 가 쌓이거나 `to_ms` 가 만료돼야 사용자 공간으로 올린다.
        게임 흐름은 저대역이라 전투 패킷 한 발이 `to_ms` 만큼 붙잡히면 **패킷 감지의
        존재 이유인 리드타임을 그대로 반납**한다.
        """
        lib = _load_wpcap()
        if lib is None:
            raise OSError(reason_message(REASON_NO_DLL))
        if not _has_mintocopy:
            timeout_ms = min(timeout_ms, FALLBACK_TIMEOUT_MS)
        errbuf = ctypes.create_string_buffer(_PCAP_ERRBUF_SIZE)
        ptr = lib.pcap_open_live(
            dev.encode("ascii"),
            int(snaplen),
            1 if promisc else 0,
            int(timeout_ms),
            errbuf,
        )
        if not ptr:
            raise OSError(decode_c_str(errbuf.value) or "pcap_open_live 실패")
        handle = PcapHandle(
            int(ptr),
            effective_snaplen(lib, int(ptr), int(snaplen)),
            int(timeout_ms),
            lib,
            calibrator=LayoutCalibrator(default_layout_order()),
        )
        handle._apply_mintocopy()
        return handle

    def _apply_mintocopy(self) -> None:
        if not _has_mintocopy or self._ptr is None:
            return
        try:
            self.mintocopy_ok = int(self._lib.pcap_setmintocopy(self._ptr, 0)) == 0
        except Exception:
            self.mintocopy_ok = False
        if not self.mintocopy_ok:
            get_logger().warning(
                "pcap_setmintocopy 실패 — 커널 버퍼 지연이 남습니다 "
                f"(to_ms={self.timeout_ms}ms)"
            )

    def set_filter(self, bpf: str) -> None:
        with self._lock:
            if self._closed or self._ptr is None:
                raise OSError("닫힌 핸들에 필터를 설정할 수 없습니다")
            prog = _bpf_program()
            rc = int(
                self._lib.pcap_compile(
                    self._ptr,
                    ctypes.byref(prog),
                    bpf.encode("ascii"),
                    1,
                    _PCAP_NETMASK_UNKNOWN,
                )
            )
            if rc < 0:
                raise OSError(f"BPF 컴파일 실패: {self._geterr()}")
            try:
                rc = int(self._lib.pcap_setfilter(self._ptr, ctypes.byref(prog)))
                if rc < 0:
                    raise OSError(f"BPF 적용 실패: {self._geterr()}")
            finally:
                try:
                    self._lib.pcap_freecode(ctypes.byref(prog))
                except Exception:
                    pass

    def _geterr(self) -> str:
        try:
            return decode_c_str(self._lib.pcap_geterr(self._ptr))
        except Exception:
            return ""

    def _raw_next(self) -> tuple[int, int, int]:
        """(rc, 헤더 주소, 데이터 주소). **DLL 을 만지는 유일한 지점.**

        테스트는 이 메서드만 오버라이드해 실 DLL 없이 `next_ex()` 전 파이프라인
        (보정·클램프·슬라이싱·rc 분기)을 실행한다.
        """
        hdr = ctypes.c_void_p()
        data = ctypes.c_void_p()
        rc = int(self._lib.pcap_next_ex(self._ptr, ctypes.byref(hdr), ctypes.byref(data)))
        return rc, hdr.value or 0, data.value or 0

    def next_ex(self) -> tuple[int, float, bytes]:
        """(rc, 수신 시각, 페이로드).

        rc: 1=패킷, 0=타임아웃 또는 드랍(보정 중·불량 헤더), -1=에러, -2=breakloop.
        시각은 **`pcap_next_ex` 반환 직후의 `time.time()`** 이다 — 헤더 ts 가 아니다.
        """
        with self._lock:
            if self._closed or self._ptr is None:
                return -1, time.time(), b""
            rc, hdr_addr, data_addr = self._raw_next()
            ts = time.time()
            if rc != 1:
                if rc == -1:
                    self.last_error = self._geterr()
                return rc, ts, b""
            if not hdr_addr or not data_addr:
                self.dropped_bad_hdr += 1
                self._note_drop(ts, "헤더/데이터 포인터 없음")
                return 0, ts, b""
            layout = self.layout
            # 보정 전에는 후보 최대 크기를 떠야 하지만, 확정된 뒤에는 그 배치만큼만 읽어
            # 불필요한 out-of-object read 를 없앤다 (tv32 면 16바이트).
            raw = ctypes.string_at(
                hdr_addr, _HDR_PROBE_BYTES if layout is None else layout.size
            )
            if layout is None:
                layout = self._calib.feed(raw, self._snaplen, ts)
                if layout is None:
                    self.dropped_uncalibrated += 1
                    return 0, ts, b""
                self.layout = layout
                self.layout_confidence = self._calib.confidence
                msg = (
                    f"pcap_pkthdr 배치 = {layout.name} ({self.layout_confidence}, "
                    f"{self._calib.samples}표본) / {lib_version()}"
                )
                if self.layout_confidence == "measured":
                    get_logger().info(msg)
                else:
                    get_logger().warning(msg + " — 실측 합의 실패, 추정값 사용")
            fields = _fields(raw, layout)
            if fields is None:
                self.dropped_bad_hdr += 1
                self._note_drop(ts, f"헤더가 {layout.name} 크기에 못 미침")
                return 0, ts, b""
            sec, usec, caplen, wirelen = fields
            self.last_hdr_ts = sec + usec / 1_000_000.0
            n = admit_caplen(caplen, wirelen, self._snaplen)
            if n is None:
                self.dropped_bad_hdr += 1
                self._note_drop(
                    ts, f"길이 부적합 caplen={caplen} wirelen={wirelen}"
                )
                return 0, ts, b""
            self._drop_streak = 0
            return 1, ts, ctypes.string_at(data_addr, n)

    def _note_drop(self, ts: float, why: str) -> None:
        """드랍이 **지속**되면 스로틀 경고. 산발 드랍은 조용히 카운터만 올린다.

        이 레포의 최악 실패 모드는 "조용히 0 패킷" 이다. 보정이 틀리거나 스트림 성격이
        바뀌어 100% 드랍에 빠졌을 때, 카운터만 두면 아무도 안 읽는다.
        """
        self._drop_streak += 1
        if self._drop_streak < _DROP_WARN_STREAK:
            return
        if ts - self._last_drop_warn_ts < _DROP_WARN_THROTTLE_SEC:
            return
        self._last_drop_warn_ts = ts
        get_logger().warning(
            f"패킷 연속 드랍 {self._drop_streak}회 — {why} "
            f"(배치={self.layout.name if self.layout else '미확정'}/"
            f"{self.layout_confidence}, snaplen={self._snaplen}). "
            "패킷 감지가 사실상 멈춘 상태입니다"
        )

    def breakloop(self) -> None:
        """best-effort 깨우기. `_lock`(장기) 대신 `_ptr_lock`(단기)만 잡는다.

        `_lock` 을 잡으면 `next_ex` 가 최대 `timeout_ms` 동안 들고 있어서 깨우기 자체가
        블록된다 — 그러면 존재 의의가 없다. 그렇다고 락 없이 부르면 `_ptr` 을 읽은 직후
        다른 스레드의 `close()` 가 `pcap_close` 를 끝내버릴 수 있고, libpcap 의
        `pcap_breakloop` 은 `p->breakloop_op(p)` 로 **디스패치**하므로 해제된 구조체의
        함수 포인터를 호출하게 된다(리뷰에서 재현됨). `_ptr_lock` 은 DLL 호출 구간만
        감싸고 `next_ex` 는 잡지 않으므로 둘 다 해결된다.
        """
        with self._ptr_lock:
            ptr = self._ptr
            if ptr is None:
                return
            try:
                self._lib.pcap_breakloop(ptr)
            except Exception:
                pass

    def close(self) -> None:
        """멱등. 진행 중인 `next_ex` 가 끝날 때까지 기다린다(최대 `timeout_ms`)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # 해제는 반드시 `_ptr_lock` 안에서 — 진행 중인 breakloop 과 직렬화한다.
            with self._ptr_lock:
                ptr, self._ptr = self._ptr, None
                if ptr is None:
                    return
                try:
                    self._lib.pcap_close(ptr)
                except Exception:
                    pass


def lib_version() -> str:
    """`pcap_lib_version()` 문자열. Npcap 이 없으면 빈 문자열."""
    lib = _load_wpcap()
    if lib is None:
        return ""
    try:
        return decode_c_str(lib.pcap_lib_version())
    except Exception:
        return ""
