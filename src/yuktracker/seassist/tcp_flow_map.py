"""거상 클라이언트 TCP 흐름 조회 — PID → 게임 서버 연결 매핑 (2026-07-30, 패킷 감지 P2).

전투 진입/종료를 패킷으로 판정하려면 "이 바이트 스트림이 **어느 슬롯의 것인가"를 알아야
한다. 캡처 쪽이 아는 것은 (로컬 IP, 로컬 포트) 뿐이고, 러너가 아는 것은 슬롯의 창 → PID 다.
둘을 잇는 유일한 연결고리가 OS 의 TCP 연결 테이블이다: PID → (로컬 포트, 서버 포트).

`psutil` 을 쓰지 않는다. 레포에 없는 의존성이고, 필요한 것은 `iphlpapi` 호출 하나뿐이다
(형제 프로젝트 `17. Packet study` 는 psutil 을 쓰지만 그쪽은 분석 전용 환경이다).

**하지 않기로 한 것**
- **서버 IP 하드코딩 금지.** 사용자가 다른 서버/채널을 쓸 수 있다. 원격 포트가 게임 포트이고
  PID 가 일치하는 연결에서 IP 를 역산한다. 이 파일에는 IP 리터럴이 하나도 없어야 하고
  `tests/test_tcp_flow_map.py` 가 그것을 소스 핀으로 검사한다.
- **상태를 갖지 않는다.** 재접속하면 로컬 포트가 바뀌므로 주기 갱신(약 5초)이 필요하지만,
  그 타이머는 호출자 책임이다. 여기는 매번 새로 찍는 스냅샷 조회 함수다.
- **IPv6 를 다루지 않는다** (AF_INET 만). 게임은 IPv4 로 붙는다.
- **모호한 PID 의 흐름을 버리지 않는다.** 한 PID 에 게임 포트 연결이 둘 이상이면
  `ambiguous_pids()` 가 그 PID 를 알려주고, 호출자는 **그 슬롯만** 화면 감지로 강등한다.
  전체 기능을 끄지 않는다 (2026-07-15 "감지 실패 기반 자동 정지 금지" 와 같은 결).
- **`pcap_ffi` 를 import 하지 않는다.** 레이어 관계가 아니다 — 이쪽은 Windows 가 항상
  제공하는 API 이고 저쪽은 아예 없을 수도 있는 서드파티 드라이버다. 합성은 소비자가 한다.
"""
from __future__ import annotations

import ctypes
import socket
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterable, Sequence

#: 거상 클라이언트가 서버와 맺는 두 연결의 원격 포트 (실측: 클라 1개당 정확히 2개).
#: 8000 = 게임 스트림(전투 신호가 여기), 4011 = 부차 연결 — 정체는 **미확정**(채팅 연결 후보,
#: 근거는 제3자 도구 라벨뿐·내용 미관찰: docs/PACKET-PROCESS.md §3 H-4011-02, 등급 C).
#: 4011 의 정체 판단은 이 주석 한 곳에만 둔다 — 아래 AUX_PORTS 주석은 여기를 가리킨다.
GAME_SERVER_PORTS = (8000, 4011)

#: 흐름↔슬롯 매핑의 모호성을 판정할 때 기준으로 삼는 포트. 8000 만 실제로 디코드한다.
MAIN_PORT = 8000
#: 부차 연결 포트 — 캡처는 하되(패킷 PR-D, 2026-08-19) 디코드하지 않고 원시 세그먼트로
#: 발굴 원장에만 남긴다. 정체는 GAME_SERVER_PORTS 주석(H-4011-02) — 확정 전엔 디코드하지 않는다.
AUX_PORTS = (4011,)

_AF_INET = 2
_TCP_TABLE_OWNER_PID_ALL = 5
_TCP_STATE_ESTABLISHED = 5  # MIB_TCP_STATE_ESTAB

#: MIB_TCPROW_OWNER_PID = DWORD 6개. 전 필드가 4바이트라 정렬 패딩이 없다.
_ROW_SIZE = 24
_ERROR_INSUFFICIENT_BUFFER = 122
#: 1차(크기 질의)와 2차(수집) 사이에 연결이 늘어날 수 있다 — 여유분을 얹어 경합을 흡수.
_TABLE_SLACK_BYTES = 4096
_TABLE_MAX_RETRIES = 4

_dll: ctypes.WinDLL | None = None
_dll_lock = threading.Lock()
_dll_attempted = False


@dataclass(frozen=True)
class TcpRow:
    """MIB_TCPROW_OWNER_PID 1행을 디코드한 것 (필터 전 원본)."""

    state: int
    local_ip: str
    local_port: int
    remote_ip: str
    remote_port: int
    pid: int


@dataclass(frozen=True)
class GameFlow:
    """게임 서버로 향하는 연결 1개. frozen — 후속 계층이 set/dict 키로 쓴다."""

    pid: int
    local_ip: str
    local_port: int
    server_ip: str
    server_port: int


def parse_tcp_table(buf: bytes) -> list[TcpRow]:
    """`GetExtendedTcpTable` 버퍼를 행 목록으로. 어떤 입력에도 예외를 던지지 않는다.

    버퍼 = `dwNumEntries`(DWORD) + N × 24B.

    ⚠️ **포트 함정**: `dwLocalPort`/`dwRemotePort` 는 DWORD 지만 포트는 **network byte
    order 로 하위 2바이트**에만 들어 있고 상위 2바이트는 정의되지 않았다. 시프트로 꺼내면
    쓰레기가 섞이거나 바이트 오더를 뒤집기 쉬워서(1 ↔ 256), 하위 2바이트만 잘라 big-endian
    으로 읽는다. 주소 DWORD 는 이미 network order 라 `inet_ntoa` 에 그대로 넘긴다.
    """
    buf = bytes(buf)
    if len(buf) < 4:
        return []
    declared = int.from_bytes(buf[:4], "little")
    # 선언 개수를 그대로 믿지 않는다 — 조회 경합이나 잘린 버퍼에서 거대 루프/IndexError 방지.
    n = min(declared, (len(buf) - 4) // _ROW_SIZE)
    rows: list[TcpRow] = []
    for i in range(n):
        o = 4 + i * _ROW_SIZE
        rows.append(
            TcpRow(
                state=int.from_bytes(buf[o : o + 4], "little"),
                local_ip=socket.inet_ntoa(buf[o + 4 : o + 8]),
                local_port=int.from_bytes(buf[o + 8 : o + 10], "big"),
                remote_ip=socket.inet_ntoa(buf[o + 12 : o + 16]),
                remote_port=int.from_bytes(buf[o + 16 : o + 18], "big"),
                pid=int.from_bytes(buf[o + 20 : o + 24], "little"),
            )
        )
    return rows


def select_game_flows(
    rows: Iterable[TcpRow],
    pids: Iterable[int],
    ports: Sequence[int] = GAME_SERVER_PORTS,
) -> list[GameFlow]:
    """확립된 게임 서버 연결만 추린다. **원격 포트**로만 판정한다.

    로컬 포트가 우연히 8000 인 무관한 연결(브라우저 등)을 잡지 않기 위함이다.
    """
    want_ports = set(ports)
    want_pids = set(pids)
    out: list[GameFlow] = []
    for r in rows:
        if r.state != _TCP_STATE_ESTABLISHED:
            continue
        if r.pid not in want_pids:
            continue
        if r.remote_port not in want_ports:
            continue
        out.append(
            GameFlow(
                pid=r.pid,
                local_ip=r.local_ip,
                local_port=r.local_port,
                server_ip=r.remote_ip,
                server_port=r.remote_port,
            )
        )
    return out


def ambiguous_pids(flows: Iterable[GameFlow], port: int = MAIN_PORT) -> set[int]:
    """게임 스트림 연결이 2개 이상인 PID — 흐름↔슬롯 매핑이 갈리지 않는 경우.

    실측(클라 3개)에서는 항상 빈 집합이다. 한 프로세스가 창을 여럿 호스팅하면 발생할 수
    있고, 그때 호출자는 **해당 슬롯만** 화면 감지로 강등한다.
    """
    counts: dict[int, int] = {}
    for f in flows:
        if f.server_port != port:
            continue
        counts[f.pid] = counts.get(f.pid, 0) + 1
    return {pid for pid, n in counts.items() if n > 1}


def derive_server_ip(
    flows: Iterable[GameFlow], port: int = MAIN_PORT
) -> str | None:
    """관측된 흐름에서 게임 서버 IP 를 역산. 흐름이 없으면 None.

    ⚠️ **게임 스트림 포트(8000)의 흐름만 먼저 센다.** 전 흐름을 뭉쳐 다수결하면 부차
    연결(4011)이 다른 호스트에 붙어 있을 때 그쪽이 이길 수 있고, 그러면 BPF 가 정작
    전투 신호가 흐르는 호스트를 빼고 컴파일되어 **조용히 패킷 0개**가 된다. 클라가
    3개면 8000/4011 이 3:3 동점이라 사전순 tiebreak 로 갈리기까지 한다.
    8000 흐름이 하나도 없을 때만 전체로 폴백한다.

    폴백 **상수**는 두지 않는다 — 틀린 IP 는 조용한 실패고, 모른다고 답해야 호출자가
    화면 감지를 유지한다.
    """
    flows = list(flows)
    primary = [f for f in flows if f.server_port == port]
    counts: dict[str, int] = {}
    for f in primary or flows:
        counts[f.server_ip] = counts.get(f.server_ip, 0) + 1
    if not counts:
        return None
    # 동점이어도 결정적이도록 IP 문자열을 2차 키로.
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def derive_server_ips(
    flows: Iterable[GameFlow], ports: Sequence[int] = GAME_SERVER_PORTS
) -> tuple[str, ...]:
    """관측된 게임 흐름의 서버 IP **전부**(중복 제거, 사전순). 없으면 빈 튜플.

    `derive_server_ip` 가 "전투 스트림이 흐르는 호스트 1개"를 고르는 함수라면, 이건
    BPF 의 host 절을 짜기 위한 것이다 — 부차 연결(4011)이 8000 과 다른 호스트에
    붙어 있어도 둘 다 캡처하려면 호스트를 전부 넣어야 한다(패킷 PR-D). 호출자는
    여전히 `derive_server_ip` 로 8000 흐름 존재를 먼저 게이트한다.
    """
    want = set(ports)
    return tuple(sorted({f.server_ip for f in flows if f.server_port in want}))


def find_game_flows(
    pids: Iterable[int], *, table_bytes: bytes | None = None
) -> list[GameFlow]:
    """주어진 PID 들의 게임 서버 연결 스냅샷.

    `table_bytes` 는 테스트 주입 지점이다 (None 이면 실제 `iphlpapi` 조회).
    조회 실패는 예외가 아니라 빈 목록 — 호출자는 "이번 틱엔 흐름을 모른다" 로 다룬다.
    """
    want = set(pids)
    if not want:
        # 빈 집합은 "전체" 가 아니라 "없음" 이다. 조회 자체를 생략한다.
        return []
    buf = _query_tcp_table_bytes() if table_bytes is None else bytes(table_bytes)
    return select_game_flows(parse_tcp_table(buf), want)


def _iphlpapi() -> ctypes.WinDLL | None:
    """iphlpapi 지연 로드 + argtypes 바인딩. 실패는 None (예외 아님)."""
    global _dll, _dll_attempted
    with _dll_lock:
        if _dll_attempted:
            return _dll
        _dll_attempted = True
        try:
            # ⚠️ stdcall(WINAPI) 이므로 WinDLL 이 맞다. 같은 PR 의 pcap_ffi 는 cdecl 이라
            # CDLL 을 쓴다 — 두 파일이 나란히 구분의 레퍼런스가 된다.
            dll = ctypes.WinDLL("iphlpapi", use_last_error=True)
            dll.GetExtendedTcpTable.argtypes = (
                ctypes.c_void_p,
                ctypes.POINTER(wintypes.DWORD),
                wintypes.BOOL,
                wintypes.ULONG,
                ctypes.c_int,
                wintypes.ULONG,
            )
            dll.GetExtendedTcpTable.restype = wintypes.DWORD
            _dll = dll
        except Exception:
            _dll = None
        return _dll


def _query_tcp_table_bytes() -> bytes:
    """IPv4 TCP 연결 테이블 원본 바이트. 유일한 DLL 접점. 실패 시 b"".

    2-call 패턴(크기 질의 → 수집) 사이에 테이블이 커질 수 있어 여유분 + 재시도를 둔다.
    """
    dll = _iphlpapi()
    if dll is None:
        return b""
    try:
        size = wintypes.DWORD(0)
        # bOrder=False — 정렬이 필요 없으니 커널 부하를 줄인다.
        dll.GetExtendedTcpTable(
            None, ctypes.byref(size), False, _AF_INET, _TCP_TABLE_OWNER_PID_ALL, 0
        )
        for _ in range(_TABLE_MAX_RETRIES):
            need = int(size.value) + _TABLE_SLACK_BYTES
            buf = ctypes.create_string_buffer(need)
            size = wintypes.DWORD(need)
            rc = int(
                dll.GetExtendedTcpTable(
                    buf,
                    ctypes.byref(size),
                    False,
                    _AF_INET,
                    _TCP_TABLE_OWNER_PID_ALL,
                    0,
                )
            )
            if rc == 0:
                return buf.raw[: int(size.value)]
            if rc != _ERROR_INSUFFICIENT_BUFFER:
                return b""
            # size 에 새 필요 크기가 담겨 돌아왔다 — 다음 루프가 그걸 쓴다.
        return b""
    except Exception:
        return b""
