"""패킷 상태 소스 — 스니퍼 스레드 1개가 슬롯별 전투 상태를 발행한다.

패킷 기반 warfield IN/OUT 로드맵의 P5 산출물. P2~P4 빌딩 블록(pcap_ffi /
tcp_flow_map / gersang_protocol.FlowDecoder / state_source 심)을 처음으로 합성해
``StateSource`` 계약의 첫 구현체 ``PacketBackedSource`` 를 제공한다. App이 엔진을 소유하고 Warfield/Monster 러너가 상태를 소비한다.

구조
----
- ``PacketStateSource`` (엔진): 전역 스니퍼 스레드 1개가 모든 슬롯을 담당.
  DISCOVER(흐름/디바이스 탐색·핸들 오픈) ⇄ CAPTURE(next_ex 폴 + 5s 하우스키핑)
  상태기계. FlowDecoder 인스턴스와 발행 dict 쓰기는 **전부 이 스레드 소유**
  (single-writer — gersang_protocol 의 무락 계약과 발행 원자성 논증의 근거).
- ``PacketBackedSource`` (어댑터): 러너 폴 스레드(warfield/monster 2개 동시)가
  부르는 ``poll()``. 엔진 발행 dict 단일 읽기 + ``slot.info.pid`` 재검증뿐 —
  구조적으로 비블로킹·예외 불가.

미관측 표 (None이면 Warfield는 동기화 대기, Monster만 화면으로 폴백)
----------------------------------------------------------------------
| 패킷 소스 상태                              | 동작                          |
|---------------------------------------------|-------------------------------|
| 락 + 첫 enter/exit 관측 후                  | 상태 반환 (합성 점수 1.0/0.0) |
| 락 전 / 첫 이벤트 전 / 스니퍼 미가동        | None                          |
| 흐름 소실·npcap 에러·디코더 reset           | None + 경고 1회               |
| 기대 opcode 소멸(락 후 600s 무이벤트)       | None + 경고 1회 · **정지 금지** |
| 프레임 침묵 30s (half-open — idle 틱 1.2Hz) | None (상태 클리어)            |
| slot.info.pid ≠ 발행 pid (재시작/재등록)    | None                          |

Warfield의 행동 정책은 warfield_packet_tracker.py에서 처리한다.
초기 엔진 상태는 unknown 이 아니라 "모름 → None" — 스니퍼가 붙기 전 이미 전투 중일
수 있고 진입 프레임을 놓쳤다. 첫 EXIT 가 오면 "out" 이 자연 수립된다. 어떤
실패 경로도 슬롯을 정지시키지 않는다(2026-07-15 감지 실패 기반 정지 금지 정책).

마스터 플랜 대비 편차
--------------------
- ``start(slot_pids: dict)`` 정적 스냅샷 → **``pids_provider`` 클로저**. 클라
  재시작은 로컬 포트뿐 아니라 PID 도 바꾼다(#194 실기기) — 시작 시점 박제는
  state_source.py 계약 위반이라 매 조회 시 라이브 슬롯을 읽는 provider 로 대체.
- 발행 레코드에 유래 pid 를 함께 실어 어댑터가 매 폴 ``slot.info.pid`` 와
  대조한다 — "매 폴 재참조" 계약의 기계적 실현.

시각 축
-------
``next_ex()`` 의 2번째 반환값은 벽시계다. FlowDecoder 의 ts 는 단조 시계
계약이므로 이 모듈은 수신 직후 ``now_fn``(기본 time.monotonic)을 찍어 쓰고
**벽시계 반환값은 버린다**. 이 모듈은 벽시계를 만지지 않는다(테스트가 소스 핀).

이더넷 파싱 함정 (실측 근거)
---------------------------
페이로드 길이는 반드시 IP total_length 기반(``total − ihl − dataofs``)이다.
caplen 기반이면 60B 최소 프레임의 패딩이 스트림에 섞이는데, 이 게임은 idle
틱 페이로드가 9~16B 라 거의 모든 패킷이 패딩된다 = 그 버그는 100% 재현된다.
선언 길이가 캡처보다 길면(트렁케이션) 부분 feed 금지·통째 드랍 — 짧은 데이터를
먹이면 seq 부기가 어긋나 가짜 홀→재락 창이 전투 이벤트를 삼킬 수 있다.
"""
from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Optional

from . import pcap_ffi
from . import tcp_flow_map
from .gersang_protocol import ENTER, JOCHUL_OPCODES, MARKET_OPCODES, FlowDecoder
from .logger import format_exc_brief, get_logger
from .packet_market import parse_market_page

# ─────────────────────────── 튜닝 상수 ───────────────────────────

#: DISCOVER 재시도 간격 — 흐름/디바이스 미발견·핸들 오픈 실패 시.
DISCOVER_RETRY_SEC = 5.0
#: CAPTURE 중 흐름/PID 하우스키핑 주기. 재접속 시 로컬 포트가 바뀌므로
#: 주기 갱신은 소비자 책임(tcp_flow_map 계약).
REFRESH_SEC = 5.0
#: 흐름 소실 판정에 필요한 연속 미스 스냅샷 수 — iphlpapi 조회 실패는 빈
#: 목록으로 와서 "흐름 없음"과 구별 불가하다. 1회 실패로 락된 디코더를
#: 파기하면 다음 에지까지 패킷 커버리지를 통째로 잃는다(비대칭 비용).
FLOW_MISS_LIMIT = 2
#: 기대 opcode 소멸 워치독 — 락 후 이 시간 동안 enter/exit 0건이면 화면 위임.
#: 장시간 마을 대기와 클라 패치(opcode 개편)를 시간만으로 구별할 수 없지만,
#: 양쪽 모두 화면 위임이 무해하고 이벤트가 다시 오면 자가 치유된다.
EVENT_EXTINCT_SEC = 600.0
#: 프레임 침묵 워치독 — idle 에도 0x03ee 틱이 1.2Hz 로 오므로 이 시간 동안
#: 프레임 0 이면 half-open(케이블 뽑힘 등)이다. TCP 테이블엔 ESTABLISHED 로
#: 수 분 남아 흐름-소멸 감지가 못 잡는다.
FRAME_SILENCE_SEC = 30.0
#: 헬스 요약 INFO 로그 주기 — "조철이 안 울렸다"를 사후에 3분법(미도착/판별 거부/
#: 앱 분기)으로 가르려면 스니퍼가 살아서 뭘 봤는지가 로그에 남아야 한다(패킷 PR-C).
HEALTH_LOG_SEC = 300.0
#: 같은 슬롯의 조철 재발화 억제 폭. 초기 락 앵커는 2프레임으로 락하므로, 흐름이 재생성되거나
#: 디코더가 reset 된 직후 같은 "마커+조철" 세그먼트가 TCP 재전송으로 다시 오면(09-15 F 창은
#: ~20ms 뒤 재전송이 있었다) 새 디코더가 그것을 첫 데이터로 받아 한 번 더 발화한다 —
#: 원장에 0전투 조철 기록이 남고 알람이 두 번 뜬다. 실제 조철은 수백 전투 간격이라 이 폭 안에
#: 두 번 올 수 없다.
JOCHUL_DEDUP_SEC = 5.0
#: 미상 팝업(마커 옆에 아는 번호 없음) 알림의 슬롯별 속도 제한 폭 — 같은 슬롯에서 이 폭 안에 다시
#: 센 미상 팝업(재락·재전송 뒤 재계수든 별개 팝업이든)은 카운터·`popup_unknown_suppressed` 에만 남고
#: status 줄·앱 이벤트는 내지 않는다. 팝업 한 번에 빨간 상태바 한 번이 목적이다.
POPUP_UNKNOWN_DEDUP_SEC = JOCHUL_DEDUP_SEC
_JOIN_TIMEOUT_SEC = 2.0

#: 조철 번호 표기 — 헬스 줄 키(`30dd=`)와 경고 문구가 런타임 집합에서 파생한다.
_JOCHUL_OPS = tuple(sorted(JOCHUL_OPCODES))
_JOCHUL_OPS_TEXT = "/".join(f"0x{op:04x}" for op in _JOCHUL_OPS)
#: 육의전 번호 표기 — 헬스 줄 키(`321f=`)와 경고 문구(패킷 PR-Y2).
_MARKET_OPS = tuple(sorted(MARKET_OPCODES))
_MARKET_OPS_TEXT = "/".join(f"0x{op:04x}" for op in _MARKET_OPS)

#: 파서 드랍 사유 토큰 (health 카운터 키 — 사전 시드해 dict 크기를 고정,
#: health_snapshot 의 무락 복사가 안전해진다).
_PARSE_DROP_REASONS = (
    "eth_short", "non_ipv4", "bad_hdr", "fragment", "non_tcp", "truncated",
)

#: 캡처 대상 서버 포트 — 8000(디코드) + 4011(원시 세그먼트만, 패킷 PR-D; 외부 도구가
#: 클라별 "chatting" 포트로 표시하는 연결 — 평문/프레이밍 판정은 PR-G2, FINDINGS §9).
#: BPF 의 `port` 절은 양방향이라 c2s 도 여기까지 온다 — 방향은 `_on_packet` 이 가른다.
#: import 시점에 굳힌다 — 테스트가 tcp_flow_map 네임스페이스를 스텁해도 불변.
_CAPTURE_PORTS: tuple[int, ...] = (
    (tcp_flow_map.MAIN_PORT,) + tuple(tcp_flow_map.AUX_PORTS))
#: flow_map 키 — (서버 포트, 로컬 포트). 로컬 포트는 연결마다 유일하지만 8000/4011
#: 흐름을 한 dict 에 섞으므로 서버 포트를 키에 넣어 의미를 명시한다.
_FlowKey = tuple[int, int]


# ─────────────────────────── 값 객체 ───────────────────────────


@dataclass(frozen=True)
class _SlotState:
    """발행 레코드 — 슬롯당 1개를 단일 dict 키로 통째 교체한다(찢긴 읽기 방지)."""

    state: str  # "in" | "out"
    pid: int
    mono_ts: float
    generation: int = 0
    battle_id: int = 0
    flow: object | None = None
    transition_pending: bool = False


@dataclass(frozen=True)
class _Segment:
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    seq: int
    payload: bytes


class _Flow:
    """추적 중인 게임 연결 1개 — 스니퍼 스레드 전용 (락 없음).

    ``server_port == MAIN_PORT`` 면 디코더를 갖는 **전투 스트림**, 부차 포트(4011)면
    ``decoder is None`` 인 **원시 흐름**이다 — 세그먼트는 발굴 탭(segment_cb)으로만
    흘리고 상태·드레인·워치독 어디에도 관여하지 않는다(패킷 PR-D). 원시 흐름은
    opt-in(`aux_c2s`)이면 c2s 세그먼트도 같은 탭으로 흘린다(패킷 PR-G1).
    """

    __slots__ = ("decoder", "slot_idx", "pid", "local_port", "server_port",
                 "last_event_ts", "lock_notified", "reset_sig",
                 "fullness_drops_seen", "jochul_rejects_seen",
                 "popup_unknown_seen",
                 "party_malformed_seen", "market_rejects_seen", "observation_token")

    def __init__(self, slot_idx: int, pid: int, local_port: int,
                 server_port: int = tcp_flow_map.MAIN_PORT, *,
                 enter_anchor: bool = True) -> None:
        self.server_port = server_port
        self.decoder: Optional[FlowDecoder] = (
            FlowDecoder(enter_anchor=enter_anchor)
            if server_port == tcp_flow_map.MAIN_PORT else None)
        self.slot_idx = slot_idx
        self.pid = pid
        self.local_port = local_port
        self.last_event_ts = 0.0
        self.lock_notified = False
        self.reset_sig = (0, 0, 0)
        # 디코더의 누적 드롭 카운터를 헬스 델타로 바꾸기 위한 기준선. 흐름이 새로
        # 만들어지면 디코더도 새것이라 0 에서 다시 센다.
        self.fullness_drops_seen = 0
        self.jochul_rejects_seen = 0
        self.popup_unknown_seen = 0
        self.party_malformed_seen = 0
        self.market_rejects_seen = 0
        self.observation_token = object()

    @property
    def is_main(self) -> bool:
        return self.decoder is not None

    def current_reset_sig(self) -> tuple[int, int, int]:
        if self.decoder is None:
            return (0, 0, 0)
        st = self.decoder.stats
        return (st.hole_resets, st.framer.resets, st.framer.resyncs)


# ─────────────────────────── 순수 파서 ───────────────────────────


def _parse_eth_ipv4_tcp(data: bytes) -> tuple[Optional[_Segment], str]:
    """Ethernet(+VLAN)/IPv4/TCP 손파싱 → (_Segment, "ok") 또는 (None, 사유 토큰).

    ~40줄이면 충분해 scapy 급 의존성을 들이지 않는다(새 pip 의존성 0 원칙).
    페이로드가 빈 순수 ACK 도 (seg, "ok") 로 돌려주고 스킵은 호출자 몫.
    """
    if len(data) < 14:
        return None, "eth_short"
    ethertype = (data[12] << 8) | data[13]
    off = 14
    while ethertype in (0x8100, 0x88A8):  # VLAN / QinQ — 태그당 4B
        if len(data) < off + 4:
            return None, "eth_short"
        ethertype = (data[off + 2] << 8) | data[off + 3]
        off += 4
    if ethertype != 0x0800:
        return None, "non_ipv4"
    if len(data) < off + 20:
        return None, "bad_hdr"
    vihl = data[off]
    if vihl >> 4 != 4:
        return None, "non_ipv4"
    ihl = (vihl & 0x0F) * 4
    total_len = (data[off + 2] << 8) | data[off + 3]
    if ihl < 20 or total_len < ihl or len(data) < off + ihl:
        return None, "bad_hdr"
    if ((data[off + 6] << 8) | data[off + 7]) & 0x3FFF:  # MF 비트 또는 오프셋≠0
        return None, "fragment"
    if data[off + 9] != 6:  # TCP
        return None, "non_tcp"
    tcp_off = off + ihl
    if len(data) < tcp_off + 20:
        return None, "bad_hdr"
    dataofs = (data[tcp_off + 12] >> 4) * 4
    if dataofs < 20 or total_len < ihl + dataofs:
        return None, "bad_hdr"
    pay_off = tcp_off + dataofs
    pay_len = total_len - ihl - dataofs
    if pay_off + pay_len > len(data):
        return None, "truncated"
    return _Segment(
        src_ip=socket.inet_ntoa(data[off + 12: off + 16]),
        dst_ip=socket.inet_ntoa(data[off + 16: off + 20]),
        src_port=(data[tcp_off] << 8) | data[tcp_off + 1],
        dst_port=(data[tcp_off + 2] << 8) | data[tcp_off + 3],
        seq=int.from_bytes(data[tcp_off + 4: tcp_off + 8], "big"),
        payload=data[pay_off: pay_off + pay_len],
    ), "ok"


# ─────────────────────────── 엔진 ───────────────────────────


class PacketStateSource:
    """스니퍼 엔진 — lifecycle 은 소유자(App) 책임, 러너는 어댑터로 poll 만.

    ``pids_provider`` 는 {slot_idx: pid} 를 돌려주는 클로저다. 매 조회(DISCOVER
    진입·5s 하우스키핑)마다 호출되므로 **라이브 Slot 객체를 읽어야 한다** —
    시작 시점 스냅샷을 넘기면 클라 재시작(PID 변경)을 영영 못 본다.
    ``event_cb(slot_idx, kind)``/``status_cb(msg)`` 는 **스니퍼 스레드에서**
    호출된다 — GUI 소유자는 스레드-세이프 싱크(_post_status 계열)를 넘길 것.

    ``segment_cb(slot_idx, local_port, seq, payload, mono_ts, server_port=)`` 는
    발굴 수집용 탭이다(Phase 0). 디코더에 먹이기 **전**의 도착 순서 그대로 받는다 —
    오프라인 재생이 프로덕션과 같은 경로를 밟아야 발굴 결과가 그대로 이식되기
    때문이다. 부차 포트(4011) 세그먼트도 같은 탭으로 오되 ``server_port`` 로
    갈린다 — 디코더에는 절대 먹이지 않는다(패킷 PR-D). ``aux_c2s=True``(개발
    opt-in, 패킷 PR-G1)면 4011 **c2s** 세그먼트도 ``direction="c2s"`` 를 붙여 같은
    탭으로 흘린다 — s2c 호출에는 이 kwarg 가 없으므로 기존 구현체는 무변경이다.
    8000 c2s 는 본문 암호화 확정(FINDINGS §1)이라 opt-in 과 무관하게 버린다.
    ⚠️ event_cb 와 동일하게 **가드하지 않는다**: 여기서 예외가 새면 스니퍼
    스레드가 죽어 세션 패킷 커버리지를 통째로 잃는다. 구현체가 전부 삼킬 것.

    ``fullness_cb(slot_idx, value, mono_ts, pid=, flow=)`` 는 포만감 수치 갱신이다(0~4000).
    전투 에지와 달리 **상태가 아니라 관측값**이라 `_states` 에 넣지 않는다 — 이
    엔진은 발행만 하고 해석은 상위(섀도우 원장)가 한다. 같은 스레드 계약.
    ``party_cb(slot_idx, table, mono_ts, pid=)`` 는 용병 파티 표(`PartyTable`)
    관측이다 — 같은 성격(관측값)·같은 스레드 계약(패킷 PR-F).
    ``pid`` 는 **그 흐름의** 게임 PID 다(전투 상태의 `_SlotState.pid` 와 같은
    출처). 슬롯 재등록 직후 구 프로세스의 흐름이 FLOW_MISS_LIMIT 동안 남는데,
    상위가 슬롯의 *현재* pid 로 스탬프하면 구 프로세스 값이 새 pid 로 기록되어
    런타임 저장소의 PID 신선도 가드가 무력화된다.

    ``market_cb(slot_idx, page, observation, pid=)`` 는 육의전 목록 페이지 관측이다
    (`packet_market.MarketPage` — 파싱 성공분만, `observation` 은 프레이머의 raw
    `MarketObservation`: ts·opcode·body). 같은 성격(관측값)·같은 스레드 계약(패킷 PR-Y2,
    H-2609-08 B — 관측 전용 배선). 콜백이 있어야 프레이머의 ``observe_market`` 이 켜진다
    (세그먼트마다 콜백 유무로 설정 — wordinput 과 같다). 파싱 실패는
    ``market_parse_failures`` 로만 세고 전달하지 않으며, 콜백 예외는 wordinput 처럼 삼키고
    ``market_callback_errors`` 로 센다.
    """

    def __init__(
        self,
        pids_provider: Callable[[], dict[int, int]],
        *,
        event_cb: Optional[Callable[[int, str], None]] = None,
        status_cb: Optional[Callable[[str], None]] = None,
        segment_cb: Optional[Callable[..., None]] = None,
        fullness_cb: Optional[Callable[..., None]] = None,
        party_cb: Optional[Callable[..., None]] = None,
        battle_cb: Optional[Callable[..., None]] = None,
        wordinput_cb: Optional[Callable[..., None]] = None,
        market_cb: Optional[Callable[..., None]] = None,
        invalidate_cb: Optional[Callable[[int], None]] = None,
        aux_c2s: bool = False,
        enter_anchor: bool = True,
        now_fn: Callable[[], float] = time.monotonic,
        retry_wait_sec: float = DISCOVER_RETRY_SEC,
    ) -> None:
        self._pids_provider = pids_provider
        self._event_cb = event_cb
        self._segment_cb = segment_cb
        self._fullness_cb = fullness_cb
        self._party_cb = party_cb
        self._battle_cb = battle_cb
        self._wordinput_cb = wordinput_cb
        self._market_cb = market_cb
        self._invalidate_cb = invalidate_cb
        self._aux_c2s = bool(aux_c2s)
        #: 전투 ENTER 앵커 락(기본 on). 실기기에서 유령 IN 이 의심되면 재배포 없이 끌 수 있게
        #: 소유자(App)가 환경변수로 넘긴다 — 끄면 종전처럼 k 프레임 락만 쓴다.
        self._enter_anchor = bool(enter_anchor)
        self._status = status_cb or (lambda _msg: None)
        self._now = now_fn
        self._retry_wait_sec = retry_wait_sec
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._handle = None  # 스니퍼 스레드가 열고 닫는다 — stop() 은 breakloop 만
        self._states: dict[int, _SlotState] = {}
        from .wordinput_packet_state import WordinputPackets
        self.wordinput = WordinputPackets()
        self._generations: dict[int, int] = {}
        self._battle_ids: dict[int, int] = {}
        self._warned: set[str] = set()
        # 슬롯별 마지막 조철 발화 시각(단조) — JOCHUL_DEDUP_SEC 재발화 억제. 스니퍼 스레드 전용.
        self._jochul_last: dict[int, float] = {}
        # 슬롯별 마지막 미상 팝업 알림 시각 — POPUP_UNKNOWN_DEDUP_SEC. 스니퍼 스레드 전용.
        self._popup_unknown_last: dict[int, float] = {}
        self._health: dict[str, int] = {k: 0 for k in _PARSE_DROP_REASONS}
        self._health.update(
            packets=0, fed_segments=0, battle_events=0, unmapped_segments=0,
            state_invalidations=0, state_clears=0, provider_errors=0,
            tracked_flows=0, opens=0, filter_refreshes=0,
            fullness_updates=0, fullness_out_of_range=0,
            jochul_events=0, jochul_rejected=0, jochul_duplicates=0,
            popup_unknown=0, popup_unknown_suppressed=0,
            aux_segments=0, aux_bytes=0, tracked_aux_flows=0,
            aux_unmapped_segments=0,
            aux_c2s_segments=0, aux_c2s_bytes=0,
            party_tables=0, party_malformed=0,
            wordinput_open=0, wordinput_refresh=0,
            wordinput_dropped=0, wordinput_callback_errors=0,
            market_frames=0, market_rows=0, market_rejected=0, market_dropped=0,
            market_parse_failures=0, market_anomalies=0, market_callback_errors=0,
        )

    # ---------- lifecycle (소유자 스레드) ----------

    def start(self) -> tuple[bool, str]:
        """npcap 프로브 + 스레드 스폰. 디바이스/흐름 탐색·핸들 오픈은 스레드 안."""
        if self._thread is not None and self._thread.is_alive():
            return True, "패킷 스니퍼 이미 실행 중"
        ok, reason = pcap_ffi.npcap_available()
        if not ok:
            return False, pcap_ffi.reason_message(reason)
        self._stop_event.clear()
        self._warned.clear()
        self._jochul_last.clear()
        self._popup_unknown_last.clear()
        self._clear_all_states()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="PacketStateSource"
        )
        self._thread.start()
        return True, "패킷 스니퍼 시작"

    def stop(self) -> None:
        """멱등. breakloop(best-effort) → join → (비정상 시에만) close 강제.

        정지 응답성의 진실 원천은 breakloop 이 아니라 핸들 timeout_ms 주기 폴 +
        모든 대기가 ``_stop_event.wait`` 라는 사실이다(sleep 금지 — 소스 핀).
        """
        self._stop_event.set()
        self.wordinput.reset()
        h = self._handle
        if h is not None:
            h.breakloop()
        t = self._thread
        if t is not None:
            t.join(_JOIN_TIMEOUT_SEC)
            if t.is_alive():
                # 계약 위반 상황(스레드가 join 에 못 나옴) — 최후 수단.
                h2 = self._handle
                if h2 is not None:
                    h2.close()
            else:
                self._thread = None

    # ---------- 읽기 (러너/GUI 스레드) ----------

    def state_for(self, slot_idx: int) -> Optional[_SlotState]:
        """발행 레코드 단일 읽기 — dict.get 은 GIL 원자."""
        return self._states.get(slot_idx)

    def is_alive(self) -> bool:
        """엔진 스레드가 실행 중이고 정지가 요청되지 않았는지 반환한다.

        캡처 핸들이 없는 DISCOVER 대기도 살아 있는 엔진이다. 이 값은 캡처 준비나
        패킷 도착을 보장하지 않으며, 실제 핸들 상태는 ``capture_open`` 으로 본다.
        """
        thread = self._thread
        return bool(
            thread is not None
            and thread.is_alive()
            and not self._stop_event.is_set()
        )

    def is_capturing(self) -> bool:
        """수집 창을 열 수 있는 상태: 실행 중인 스레드와 필터 적용된 캡처 핸들."""
        return self.is_alive() and self._handle is not None

    def health_snapshot(self) -> dict[str, int]:
        """카운터와 현재 lifecycle 상태 사본. 표시/원장 진단용 무락 읽기."""
        health = dict(self._health)
        health["thread_alive"] = int(self.is_alive())
        health["capture_open"] = int(self._handle is not None)
        return health

    # ---------- 스니퍼 스레드 본체 ----------

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                ctx = self._discover()
                if ctx is None:
                    self._stop_event.wait(self._retry_wait_sec)
                    continue
                handle, hosts = ctx
                self._handle = handle
                try:
                    self._capture(handle, hosts)
                finally:
                    self._handle = None
                    handle.close()
                    self._clear_all_states()
                # 캡처 복귀 후에도 재시도 간격을 둔다 — 복귀 사유가 에러(rc=-1/
                # 필터 실패)인데 오픈은 계속 성공하는 반죽음 드라이버면 무대기
                # 재-DISCOVER 가 open/close churn 루프가 된다. 정상 정지는
                # _stop_event 가 이미 set 이라 즉시 깨어나 지연 0.
                self._stop_event.wait(self._retry_wait_sec)
        except Exception as e:  # 스니퍼는 필수 아님 — 죽어도 화면 폴백이 담당.
            get_logger().warning(
                f"패킷 스니퍼 스레드 예외 종료 — 화면 감지로 계속: {format_exc_brief(e)}"
            )
        finally:
            # 발행 잔재 제거 — 엔진이 죽은 뒤 어댑터가 마지막 상태를 영구
            # 공급하면 러너가 화면으로 못 돌아간다(상태 박제).
            self._clear_all_states()

    def _discover(self):
        """흐름 → 서버 IP → 디바이스 → 핸들 오픈 → BPF. 실패는 None (재시도)."""
        pids = self._safe_pids()
        if not pids:
            return None
        flows = tcp_flow_map.find_game_flows(set(pids.values()))
        main = [f for f in flows if f.server_port == tcp_flow_map.MAIN_PORT]
        server_ip = tcp_flow_map.derive_server_ip(flows)
        if not main or server_ip is None:
            return None
        dev = pcap_ffi.find_device_for_ip(main[0].local_ip)
        if dev is None:
            self._warn_once(
                "no_device",
                f"패킷 캡처 디바이스 미발견(로컬 IP {main[0].local_ip}) — 전투 상태 미확정",
            )
            return None
        try:
            handle = pcap_ffi.PcapHandle.open_live(
                dev, snaplen=65535, promisc=False,
                timeout_ms=pcap_ffi.DEFAULT_TIMEOUT_MS,
            )
        except OSError as e:
            self._warn_once("open_fail", f"패킷 캡처 열기 실패 — 화면 감지 유지: {e}")
            return None
        self._health["opens"] += 1
        if self._stop_event.is_set():
            handle.close()
            return None
        # 호스트 절은 8000 호스트 + 부차(4011) 호스트 전부 — 부차가 다른 호스트에
        # 붙어 있어도 캡처된다. 8000 흐름 존재는 위 server_ip 게이트가 보장.
        hosts = tuple(tcp_flow_map.derive_server_ips(flows)) or (server_ip,)
        try:
            handle.set_filter(pcap_ffi.build_bpf(hosts, _CAPTURE_PORTS))
        except (OSError, ValueError) as e:
            self._warn_once("filter_fail", f"BPF 필터 적용 실패 — 화면 감지 유지: {e}")
            handle.close()
            return None
        # 캡처 개시 증거 — "스니퍼가 실제로 떴는가"를 status 로그로 확인할 수 있게
        # (조철 알람 미발화 진단의 첫 관문). 사이클마다 1줄(재-DISCOVER 포함).
        aux_n = sum(1 for f in flows if f.server_port in tcp_flow_map.AUX_PORTS)
        ports_txt = "+".join(str(p) for p in _CAPTURE_PORTS)
        # 슬롯별 로컬 포트 — 외부 도구가 클라별로 보여주는 game/chatting 포트와 눈으로
        # 대조해 흐름↔슬롯 귀속을 확정하는 게이트(패킷 PR-G1). 라벨은 포트 번호
        # 그대로 — 4011 의 정체는 판정(PR-G2) 전까지 이름 붙이지 않는다.
        pid_to_slot = {pid: idx for idx, pid in pids.items()}
        per_slot: dict[int, dict[int, int]] = {}
        for f in flows:
            s = pid_to_slot.get(f.pid)
            if s is not None:
                per_slot.setdefault(s, {})[f.server_port] = f.local_port
        flow_txt = " ".join(
            f"s{s + 1} " + " ".join(f"{p}:{per_slot[s].get(p, '-')}"
                                    for p in _CAPTURE_PORTS)
            for s in sorted(per_slot))
        self._status(
            f"[패킷] 캡처 시작 — 서버 {', '.join(hosts)} 포트 {ports_txt}, "
            f"흐름 {tcp_flow_map.MAIN_PORT} {len(main)}개 / 부차 {aux_n}개, "
            f"오픈 {self._health['opens']}회 | {flow_txt or '-'}")
        return handle, hosts

    def _capture(self, handle, hosts) -> None:
        """CAPTURE 루프 — 복귀는 stop 또는 에러(→ 호출자가 close 후 재-DISCOVER).

        ``hosts`` 는 현재 BPF 의 호스트 튜플(문자열 1개도 허용 — 튜플로 정규화).
        """
        if isinstance(hosts, str):
            hosts = (hosts,)
        hosts = tuple(hosts)
        # 새 캡처 사이클 = 직전 사이클과의 사이에 관측 공백이 있었다는 뜻 —
        # 그 사이 전이를 놓쳤을 수 있으므로 발행 상태를 전부 무효화하고
        # (화면이 메운다) 첫 이벤트부터 다시 세운다.
        for slot_idx in list(self._states):
            self._clear_state(slot_idx)
        pids = self._safe_pids()
        flows = tcp_flow_map.find_game_flows(set(pids.values()))
        flow_map: dict[_FlowKey, _Flow] = {}   # (서버 포트, 로컬 포트) → _Flow
        flow_miss: dict[_FlowKey, int] = {}
        self._reconcile(flows, pids, flow_map, flow_miss)
        last_hk = self._now()
        last_health_log = last_hk
        while not self._stop_event.is_set():
            rc, _wall_ts_unused, data = handle.next_ex()
            now = self._now()
            if rc == 1 and data:
                self._on_packet(data, now, flow_map)
            elif rc == -1:
                self._warn_once(
                    "read_fail",
                    f"패킷 읽기 오류 — 캡처 재시작: {handle.last_error or '원인 미상'}",
                )
                return
            elif rc == -2:
                continue
            if now - last_hk >= REFRESH_SEC:
                last_hk = now
                pids = self._safe_pids()
                flows = tcp_flow_map.find_game_flows(set(pids.values()))
                hosts = self._maybe_refresh_filter(handle, hosts, flows)
                self._reconcile(flows, pids, flow_map, flow_miss)
                self._watchdogs(now, flow_map)
                if now - last_health_log >= HEALTH_LOG_SEC:
                    last_health_log = now
                    self._log_health(flow_map)

    def _maybe_refresh_filter(self, handle, hosts: tuple[str, ...], flows
                              ) -> tuple[str, ...]:
        """하우스키핑 BPF 갱신 — **8000 게이트 + 합집합(확장 전용)**.

        `set_filter` 는 그 자체로 캡처 공백이므로(pcap_ffi.build_bpf 문서) 꼭
        필요할 때만 부른다:
        - 8000 흐름이 없는 스냅샷(클라 재접속 순간 등)은 무시 — 부차(4011)만
          남은 호스트 집합으로 갈아타면 전투 서버가 필터에서 빠져 **조용히 0개**.
        - 호스트가 *사라져도* 필터는 줄이지 않는다 — 일시 4011 타 호스트 흐름이
          (A)→(A,B)→(A) 로 깜빡일 때마다 두 번 재컴파일되는 것을 막는다.
          남은 호스트 절은 트래픽이 없을 뿐 무해하고, 세션 중 서버 IP 수는 유한하다.
        - 실패하면 **기존 필터를 유지**하고 다음 틱에 재시도(warn 1회). 캡처
          사이클을 끝내면 슬롯 발행 상태가 전부 지워지고, 실패가 지속될 때
          open/close 재-DISCOVER 루프가 된다.
        반환값은 갱신 후(또는 유지된) 호스트 튜플.
        """
        flows = list(flows)
        if not any(f.server_port == tcp_flow_map.MAIN_PORT for f in flows):
            return hosts
        seen = tuple(tcp_flow_map.derive_server_ips(flows))
        if not seen or set(seen) <= set(hosts):
            return hosts
        new_hosts = tuple(sorted(set(hosts) | set(seen)))
        try:
            handle.set_filter(pcap_ffi.build_bpf(new_hosts, _CAPTURE_PORTS))
        except (OSError, ValueError) as e:
            self._warn_once(
                "filter_fail", f"BPF 필터 갱신 실패 — 기존 필터 유지, 재시도: {e}")
            return hosts
        self._health["filter_refreshes"] += 1
        self._status(
            f"[패킷] 게임 서버 변경 감지 — 필터 갱신 ({', '.join(new_hosts)})")
        return new_hosts

    def _log_health(self, flow_map: dict[_FlowKey, _Flow]) -> None:
        """헬스 요약 1줄 INFO — 스니퍼 생존·수신·판별 카운터 (패킷 PR-C).

        조철이 안 울렸을 때 사후 분류의 근거: `jochul=0 rejected=0` 이고 조철 번호
        (`JOCHUL_OPCODES`) 카운트도 0 이면 **미도착**(수집/락 문제) **또는 번호 이동**이다.
        둘은 `popup_unknown` 이 가른다 — 0 보다 크면 팝업 마커는 왔는데 옆에 아는 번호가
        없었다는 뜻 = **번호 이동**(2026-09 패치가 그랬다: 0x30dd → 0x30df). `rejected>0` 이면
        **판별 본문 드리프트**, `jochul>0` 인데 알람이 없었으면 **앱 분기** 문제다.
        절대 던지지 않는다(스니퍼 스레드).
        """
        try:
            h = self._health
            per_flow: list[str] = []
            for fl in flow_map.values():
                if fl.decoder is None:
                    continue
                st = fl.decoder.stats.framer
                counts = dict(st.opcode_counts)
                jochul_counts = " ".join(
                    f"{op:04x}={counts.get(op, 0)}" for op in _JOCHUL_OPS)
                market_counts = " ".join(
                    f"{op:04x}={counts.get(op, 0)}" for op in _MARKET_OPS)
                per_flow.append(
                    f"s{fl.slot_idx + 1}:frames={st.frames} lock={int(st.locked)} "
                    f"anchor={st.enter_anchor_locks} "
                    f"03f0={counts.get(0x03F0, 0)} 0fa4={counts.get(0x0FA4, 0)} "
                    f"1772={counts.get(0x1772, 0)} {jochul_counts} {market_counts}")
            get_logger().info(
                "[패킷] 헬스 — packets=%d fed=%d flows=%d aux_flows=%d aux_segs=%d "
                "battle=%d jochul=%d rejected=%d popup_unknown=%d popup_suppressed=%d "
                "market=%d mrej=%d "
                "fullness=%d unmapped=%d aux_unmapped=%d aux_c2s=%d invalid=%d | %s",
                h["packets"], h["fed_segments"], h["tracked_flows"],
                h["tracked_aux_flows"], h["aux_segments"],
                h["battle_events"], h["jochul_events"], h["jochul_rejected"],
                h["popup_unknown"], h["popup_unknown_suppressed"],
                h["market_frames"], h["market_rejected"],
                h["fullness_updates"], h["unmapped_segments"],
                h["aux_unmapped_segments"], h["aux_c2s_segments"],
                h["state_invalidations"], " ".join(per_flow) or "-")
        except Exception:
            pass

    def _on_packet(self, data: bytes, now: float,
                   flow_map: dict[_FlowKey, _Flow]) -> None:
        self._health["packets"] += 1
        seg, why = _parse_eth_ipv4_tcp(data)
        if seg is None:
            self._health[why] += 1
            return
        if seg.src_port not in _CAPTURE_PORTS:
            # c2s(업링크) — 8000 은 본문 암호화 확정(FINDINGS §1)이라 계속 버린다.
            # 4011 업링크는 opt-in(aux_c2s) 일 때만 원시로 발굴 탭에 흘린다 —
            # 디코더·상태·8000 헬스 카운터 무접촉, 미매핑은 세지도 않는다(8000
            # unmapped 3분법 오염 금지). 패킷 PR-G1.
            if (self._aux_c2s and seg.payload
                    and seg.dst_port in tcp_flow_map.AUX_PORTS):
                fl = flow_map.get((seg.dst_port, seg.src_port))  # (서버 포트, 로컬 포트)
                if fl is not None and self._segment_cb is not None:
                    self._segment_cb(fl.slot_idx, seg.src_port, seg.seq, seg.payload,
                                     now, server_port=seg.dst_port, direction="c2s")
                    self._health["aux_c2s_segments"] += 1
                    self._health["aux_c2s_bytes"] += len(seg.payload)
            return
        if not seg.payload:
            return  # 순수 ACK
        fl = flow_map.get((seg.src_port, seg.dst_port))
        if fl is None:
            # 미매핑은 포트별로 센다 — `unmapped_segments` 는 8000(전투 스트림)
            # 매핑 건전성 지표라 헬스 라인의 3분법 근거다. 4011 미매핑(모호 aux,
            # 리컨사일 사이 신규 연결)을 섞으면 전투 매핑 실패로 오독된다.
            key = ("unmapped_segments" if seg.src_port == tcp_flow_map.MAIN_PORT
                   else "aux_unmapped_segments")
            self._health[key] += 1
            return
        # 발굴 수집 탭 — 디코더에 먹이기 **전**(도착 순서 그대로). dedup/재조립
        # 후를 남기면 오프라인 재생이 프로덕션과 다른 경로를 밟는다. 부차 포트도
        # 여기까지는 같다(server_port 로 갈림).
        seg_cb = self._segment_cb
        if seg_cb is not None:
            seg_cb(fl.slot_idx, seg.dst_port, seg.seq, seg.payload, now,
                   server_port=seg.src_port)
        if fl.decoder is None:
            # 부차 흐름(4011) — 원시 관측만. 상태·드레인·fed 카운터 무접촉이라
            # 전투 판정 경로는 이 분기 이전과 완전히 동일하다(패킷 PR-D).
            self._health["aux_segments"] += 1
            self._health["aux_bytes"] += len(seg.payload)
            return
        before = fl.reset_sig
        fl.decoder.framer.retain_exit_body = self._battle_cb is not None
        fl.decoder.framer.observe_wordinput = self._wordinput_cb is not None
        fl.decoder.framer.observe_market = self._market_cb is not None
        wordinput_drops = fl.decoder.wordinput_dropped
        market_drops = fl.decoder.market_dropped
        events = fl.decoder.feed_segment(seg.seq, seg.payload, now)
        self._health["wordinput_dropped"] += fl.decoder.wordinput_dropped - wordinput_drops
        self._health["market_dropped"] += fl.decoder.market_dropped - market_drops
        self._health["fed_segments"] += 1
        fl.reset_sig = fl.current_reset_sig()
        if fl.reset_sig != before:
            # 스트림 연속성 상실(홀 포기/오버플로/재동기) — 그 창에 EXIT 가
            # 삼켜졌을 수 있다. 발행 상태를 유지하면 러너가 화면조차 안 보므로
            # ("in" 박제) 즉시 무효화해 화면 위임한다.
            if self._states.pop(fl.slot_idx, None) is not None:
                self._health["state_invalidations"] += 1
            self._generations[fl.slot_idx] = self._generations.get(fl.slot_idx, 0) + 1
            fl.observation_token = object()
            self.wordinput.invalidate(fl.slot_idx)
            if self._invalidate_cb is not None:
                self._invalidate_cb(fl.slot_idx)
        if not fl.lock_notified and fl.decoder.locked:
            fl.lock_notified = True
            # 앵커 락은 "k 프레임을 채우기 전에 전투가 시작됐다" 는 뜻이라 구분해 남긴다 —
            # 성긴 흐름의 세션 시작에서 첫 ENTER 를 제때 잡았는지 로그만으로 확인된다.
            how = " (ENTER 앵커)" if fl.decoder.stats.framer.anchor_locked else ""
            self._status(
                f"[패킷] 슬롯{fl.slot_idx + 1} 스트림 락{how} — 전투 신호 감시 시작")
        for ev in events:
            fl.last_event_ts = now
            state = "in" if ev.kind == ENTER else "out"
            previous = self._states.get(fl.slot_idx)
            if previous is not None and previous.state != state:
                # Block old-state inputs without pretending callback work is flow loss.
                self._states[fl.slot_idx] = replace(previous, transition_pending=True)
            # Complete EXIT observation is visible BEFORE publishing OUT to runners.
            if self._battle_cb is not None:
                self._battle_cb(fl.slot_idx, ev, pid=fl.pid, flow=fl.observation_token)
            if state == "in" and (previous is None or previous.state != "in"):
                self._battle_ids[fl.slot_idx] = self._battle_ids.get(fl.slot_idx, 0) + 1
            self._states[fl.slot_idx] = _SlotState(
                state, fl.pid, now, self._generations.get(fl.slot_idx, 0),
                self._battle_ids.get(fl.slot_idx, 0), flow=fl.observation_token)
            self._health["battle_events"] += 1
            cb = self._event_cb
            if cb is not None:
                cb(fl.slot_idx, ev.kind)

        # Missing field bytes may contain the next ENTER. Cancel a death-item
        # reservation immediately, without waiting for the reorder timeout.
        rec = self._states.get(fl.slot_idx)
        if (self._invalidate_cb is not None and rec is not None and rec.state == "out"
                and fl.decoder.stats.pending_bytes):
            self._invalidate_cb(fl.slot_idx)

        # 포만감 — 전투 에지와 같은 자리에서, 같은 스레드 계약으로.
        drops = fl.decoder.fullness_dropped
        if drops != fl.fullness_drops_seen:
            self._health["fullness_out_of_range"] += drops - fl.fullness_drops_seen
            fl.fullness_drops_seen = drops
        fcb = self._fullness_cb
        for ts, value in fl.decoder.take_fullness():
            self._health["fullness_updates"] += 1
            if fcb is not None:
                fcb(fl.slot_idx, value, ts, pid=fl.pid, flow=fl.observation_token)

        # 용병 파티 표(0x1772 sub=2001) — 포만감과 같은 자리·같은 계약(패킷 PR-F).
        pm = fl.decoder.party_malformed
        if pm != fl.party_malformed_seen:
            self._health["party_malformed"] += pm - fl.party_malformed_seen
            fl.party_malformed_seen = pm
        pcb = self._party_cb
        for ts, tbl in fl.decoder.take_party_tables():
            self._health["party_tables"] += 1
            if pcb is not None:
                pcb(fl.slot_idx, tbl, ts, pid=fl.pid)

        # 조철 — 전투 에지가 아니므로 _states 를 건드리지 않는다(팝업 신호 전용).
        # event_cb 는 스니퍼 스레드에서 직접 불리니 던지면 스니퍼가 죽는다 —
        # 구현체가 삼켜야 한다는 계약(battle event 와 동일)에 의존한다.
        rejects = fl.decoder.jochul_rejected
        if rejects != fl.jochul_rejects_seen:
            self._health["jochul_rejected"] += rejects - fl.jochul_rejects_seen
            fl.jochul_rejects_seen = rejects
            self._warn_once(
                "jochul_rejected",
                f"조철 번호({_JOCHUL_OPS_TEXT}) 프레임이 판별식(길이 13·헤더 예약 바이트)에 "
                "불일치 — 클라 패치 드리프트 의심, PACKET-FINDINGS §4 확인")
        ecb = self._event_cb
        # 번호 이동 진단 — 팝업 마커 옆에 아는 동반 번호가 없었다. `조철 0 (거부 0)` 만으로는
        # "안 왔다"와 "번호가 옮겨 갔다"를 가를 수 없다(2026-09-14~17 사흘간 그랬다).
        unknown = fl.decoder.popup_unknown
        if unknown != fl.popup_unknown_seen:
            self._health["popup_unknown"] += unknown - fl.popup_unknown_seen
            fl.popup_unknown_seen = unknown
            self._warn_once(
                "popup_unknown",
                f"슬롯{fl.slot_idx + 1} 팝업 마커 옆에 아는 번호(조철·캡차)가 없음 — 클라 패치로 "
                "번호가 옮겨 갔을 수 있다. 화면의 팝업을 확인하고 [패킷 수집] 원시 창을 남길 것 "
                "(PACKET-PATCH-RECHECK)")
            # F-203-7 a (2026-09-18 결정): 발생마다 앱에 알린다 — 앱 분기는 빨간 상태바뿐이고
            # 일시정지·포커스·원장·카운터 리셋은 없다(조철인지 모른다). 카운터 델타 1회 =
            # 이벤트 1회, 같은 슬롯의 POPUP_UNKNOWN_DEDUP_SEC 안 재발은 억제 카운터로만 남긴다.
            if self._dedup_pass(self._popup_unknown_last, fl.slot_idx, now, POPUP_UNKNOWN_DEDUP_SEC):
                self._status(f"[패킷] 슬롯{fl.slot_idx + 1} 미상 팝업 관측 — 번호 이동 의심")
                if ecb is not None:
                    ecb(fl.slot_idx, "popup_unknown")
            else:
                self._health["popup_unknown_suppressed"] += 1
        for _ts in fl.decoder.take_jochul():
            if not self._dedup_pass(self._jochul_last, fl.slot_idx, now, JOCHUL_DEDUP_SEC):
                self._health["jochul_duplicates"] += 1
                continue
            self._health["jochul_events"] += 1
            # 디코드 증거 — 앱 분기(알람)와 독립인 status 라인. 알람이 안 떠도 이
            # 줄이 있으면 "패킷은 왔고 앱 쪽 문제"로 좁혀진다(패킷 PR-C).
            self._status(f"[패킷] 슬롯{fl.slot_idx + 1} 조철 프레임 관측")
            if ecb is not None:
                ecb(fl.slot_idx, "jochul")

        # Publish only a complete, contiguous stream context. OCR/input stays on
        # its runner thread; the capture callback never waits for screen work.
        self.wordinput.flow(
            fl.slot_idx, fl.pid, fl.observation_token,
            fl.decoder.stats.framer.last_frame_ts or now,
            ready=fl.decoder.locked and not fl.decoder.stats.pending_bytes)
        wcb = self._wordinput_cb
        for observation in fl.decoder.take_wordinput():
            self._health["wordinput_" + observation.kind] += 1
            self.wordinput.note(fl.slot_idx, fl.pid, fl.observation_token, observation)
            if wcb is not None:
                try:
                    wcb(fl.slot_idx, observation, pid=fl.pid)
                except Exception:
                    self._health["wordinput_callback_errors"] += 1

        # 육의전 목록(0x321f, H-2609-08 B) — 관측 전용. 전투 상태·러너 무접촉, 같은 스레드 계약.
        # 거부 델타는 조철과 같은 드리프트 진단축(번호는 왔는데 모양이 다르다).
        mrej = fl.decoder.market_rejected
        if mrej != fl.market_rejects_seen:
            self._health["market_rejected"] += mrej - fl.market_rejects_seen
            fl.market_rejects_seen = mrej
            self._warn_once(
                "market_rejected",
                f"육의전 번호({_MARKET_OPS_TEXT}) 프레임이 판별식(9+48×행 수·헤더)에 불일치 — "
                "클라 패치 드리프트 의심, PACKET-FINDINGS §5.4 확인")
        mcb = self._market_cb
        for observation in fl.decoder.take_market():
            page = parse_market_page(observation.body)
            if page is None:
                # is_market 은 통과했는데 파서가 거부 — 두 구현의 드리프트(같은 상수를 쓴다).
                self._health["market_parse_failures"] += 1
                continue
            self._health["market_frames"] += 1
            self._health["market_rows"] += page.count
            if page.anomalies:
                self._health["market_anomalies"] += 1
            if mcb is not None:
                try:
                    mcb(fl.slot_idx, page, observation, pid=fl.pid)
                except Exception:
                    self._health["market_callback_errors"] += 1

    def _reconcile(self, flows, pids: dict[int, int],
                   flow_map: dict[_FlowKey, _Flow],
                   flow_miss: dict[_FlowKey, int]) -> None:
        """흐름↔슬롯 매핑 조정 — 스니퍼 스레드 전용 (디코더 생성/파기 포함).

        모호성(한 PID 에 같은 포트 연결 여럿)은 **포트별로** 판정한다 — 4011 이
        모호해도 8000 디코드는 계속되고, 그 반대도 같다. 부차 흐름의 생성/소실은
        전투 발행 상태(`_states`)를 건드리지 않는다(패킷 PR-D).
        """
        pid_to_slot = {pid: idx for idx, pid in pids.items()}
        ambiguous = tcp_flow_map.ambiguous_pids(flows)
        if ambiguous:
            self._warn_once(
                "ambiguous",
                "한 게임 프로세스에 전투 스트림이 여러 개 — 해당 슬롯은 전투 상태 미확정",
            )
        ambiguous_aux = {
            p: tcp_flow_map.ambiguous_pids(flows, port=p)
            for p in tcp_flow_map.AUX_PORTS
        }
        live: dict[_FlowKey, object] = {}
        for f in flows:
            if f.server_port == tcp_flow_map.MAIN_PORT:
                if f.pid in ambiguous:
                    continue
            elif f.server_port in ambiguous_aux:
                if f.pid in ambiguous_aux[f.server_port]:
                    continue
            else:
                continue
            live[(f.server_port, f.local_port)] = f
        # 신규/유지 흐름
        for key, f in live.items():
            slot = pid_to_slot.get(f.pid)
            if slot is None:
                continue  # 추적 대상 아님(강등 슬롯 포함)
            flow_miss.pop(key, None)
            cur = flow_map.get(key)
            if cur is None or cur.pid != f.pid:
                # 신규 — 또는 같은 로컬 포트가 다른 프로세스에 재배정된 경우
                # (구 연결 종료 후 포트 재사용 = 다른 스트림). 디코더 승계 금지.
                if cur is not None and cur.is_main:
                    self._clear_state(cur.slot_idx)
                if f.server_port == tcp_flow_map.MAIN_PORT:
                    self._clear_state(slot)
                flow_map[key] = _Flow(slot, f.pid, f.local_port, f.server_port,
                                      enter_anchor=self._enter_anchor)
                if not self._enter_anchor and f.server_port == tcp_flow_map.MAIN_PORT:
                    self._warn_once(
                        "enter_anchor_off",
                        "ENTER 앵커 OFF — 시작 직후 첫 전투는 k 프레임 락 뒤에야 잡힌다"
                        " (SEASSIST_PACKET_ENTER_ANCHOR=0)")
            else:
                cur.slot_idx = slot  # 재등록으로 슬롯 재배치 가능 — pid 가 진실
        # 소실 흐름 — FLOW_MISS_LIMIT 연속 미스 후에만 파기 (빈 스냅샷 내성)
        for key in list(flow_map):
            fl = flow_map[key]
            if key in live and pid_to_slot.get(fl.pid) is not None:
                continue
            misses = flow_miss.get(key, 0) + 1
            if misses >= FLOW_MISS_LIMIT:
                flow_miss.pop(key, None)
                del flow_map[key]
                if fl.is_main:
                    self._clear_state(fl.slot_idx)
            else:
                flow_miss[key] = misses
        # 발행 상태의 pid 가 현재 슬롯 pid 와 다르면(재시작/재등록) 클리어 —
        # 어댑터의 매 폴 검증과 동일 판정을 발행측에서도 정리해 둔다.
        for slot_idx, rec in list(self._states.items()):
            if pids.get(slot_idx) != rec.pid:
                self._clear_state(slot_idx)
        n_main = sum(1 for fl in flow_map.values() if fl.is_main)
        self._health["tracked_flows"] = n_main
        self._health["tracked_aux_flows"] = len(flow_map) - n_main

    def _watchdogs(self, now: float, flow_map: dict[_FlowKey, _Flow]) -> None:
        for fl in flow_map.values():
            if fl.decoder is None:
                continue  # 부차 흐름은 워치독 대상이 아니다(상태를 발행하지 않는다)
            st = fl.decoder.stats.framer
            if not st.locked:
                continue
            last_frame = st.last_frame_ts or st.locked_at
            if now - last_frame >= FRAME_SILENCE_SEC:
                self._clear_state(fl.slot_idx)
            anchor = fl.last_event_ts or st.locked_at
            if now - anchor >= EVENT_EXTINCT_SEC:
                self._clear_state(fl.slot_idx)
                self._warn_once(
                    "extinct",
                    "패킷 신호 없음 — 클라이언트 버전 변경 가능성, 전투 상태 미확정",
                )

    # ---------- 내부 유틸 (스니퍼 스레드) ----------

    def _safe_pids(self) -> dict[int, int]:
        """provider 호출 + 방어 — pid≤0 제외, 슬롯 간 pid 충돌 시 전원 제외."""
        try:
            raw = self._pids_provider() or {}
        except Exception:
            self._health["provider_errors"] += 1
            return {}
        out: dict[int, int] = {}
        for idx, pid in raw.items():
            try:
                idx_i, pid_i = int(idx), int(pid)
            except (TypeError, ValueError):
                continue
            if pid_i > 0:
                out[idx_i] = pid_i
        counts: dict[int, int] = {}
        for pid in out.values():
            counts[pid] = counts.get(pid, 0) + 1
        dup = {pid for pid, n in counts.items() if n > 1}
        if dup:
            # 흐름 1개↔슬롯 2개는 last-writer 가 조용히 이겨 오귀속 트리거가
            # 나간다 — ambiguous 와 동급으로 전원 화면 강등이 안전하다.
            self._warn_once(
                "pid_conflict",
                "같은 게임 창(PID)이 여러 슬롯에 등록됨 — 해당 슬롯은 전투 상태 미확정",
            )
            out = {i: p for i, p in out.items() if p not in dup}
        return out

    def _clear_state(self, slot_idx: int) -> None:
        # Revoke the runner/input observation before calling potentially slow sinks.
        if self._states.pop(slot_idx, None) is not None:
            self._health["state_clears"] += 1
            self._generations[slot_idx] = self._generations.get(slot_idx, 0) + 1
        self.wordinput.invalidate(slot_idx)
        if self._invalidate_cb is not None:
            self._invalidate_cb(slot_idx)

    def _clear_all_states(self) -> None:
        slot_indices = list(self._states)
        self._states.clear()
        for slot_idx in slot_indices:
            self._health["state_clears"] += 1
            self._generations[slot_idx] = self._generations.get(slot_idx, 0) + 1
        self.wordinput.reset()
        for slot_idx in slot_indices:
            self._clear_state(slot_idx)

    @staticmethod
    def _dedup_pass(last_by_slot: dict[int, float], slot_idx: int, now: float,
                    window: float) -> bool:
        """슬롯별 재발화 억제 — 직전 통과 시각이 ``window`` 안이면 False(억제), 아니면 시각을
        갱신하고 True. 조철(`JOCHUL_DEDUP_SEC`)·미상 팝업(`POPUP_UNKNOWN_DEDUP_SEC`)이 같은 게이트를
        쓴다 — 표는 발화 종류별로 따로 두어 서로의 창을 막지 않는다. 스니퍼 스레드 전용."""
        last = last_by_slot.get(slot_idx)
        if last is not None and now - last < window:
            return False
        last_by_slot[slot_idx] = now
        return True

    def _warn_once(self, token: str, msg: str) -> None:
        """세션(start~stop)당 토큰별 1회 — log.warning(WARN 브리지) + status."""
        if token in self._warned:
            return
        self._warned.add(token)
        get_logger().warning(f"[패킷] {msg}")
        self._status(f"[패킷] {msg}")


# ─────────────────────────── 어댑터 ───────────────────────────


class PacketBackedSource:
    """``StateSource`` 심의 첫 구현체 — 엔진 발행 상태를 러너 폴에 공급.

    warfield/monster 러너가 각자 스레드에서 동시에 poll 한다. 본 메서드는
    발행 dict 단일 읽기 + 속성 비교뿐이라 비블로킹이고 예외를 던질 수 없다
    (state_source.py 계약). unknown 은 절대 만들지 않는다 — 관측 불가는 None.
    """

    def __init__(self, engine: PacketStateSource) -> None:
        self._engine = engine

    def poll(self, slot_idx: int, slot) -> Optional["ClassifyResult"]:
        # 지연 import — `detector` 는 numpy·PIL 을 끌고 온다. 엔진 자체는 그 둘이 필요
        # 없으므로(ctypes + stdlib), GUI 없는 육의전 관측기가 이 모듈을 가볍게 쓰도록
        # 어댑터(러너 전용)에서만 읽는다(2026-09-21). 러너 폴 스레드의 첫 호출 이후엔
        # sys.modules 캐시라 비용이 없다.
        from .detector import ClassifyResult
        rec = self._engine.state_for(slot_idx)
        if rec is None:
            return None
        info = slot.info  # 지역 스냅샷 — 재등록 경합(TOCTOU) 차단
        if info is None or getattr(info, "pid", None) != rec.pid:
            # 클라 재시작/재등록 — 발행 상태의 유래 pid 와 불일치. 재락까지
            # 화면 폴스루가 공백을 메운다(#195 실기기 검증 경로).
            return None
        if rec.transition_pending:
            return ClassifyResult("unknown", 0.0, 0.0,
                                  observed_mono=rec.mono_ts,
                                  source_epoch=(rec.pid, rec.generation),
                                  battle_id=rec.battle_id, transition_pending=True)
        if rec.state == "in":
            return ClassifyResult("in", 1.0, 0.0, confirmed=True,
                                  observed_mono=rec.mono_ts,
                                  source_epoch=(rec.pid, rec.generation),
                                  battle_id=rec.battle_id)
        if rec.state == "out":
            return ClassifyResult("out", 0.0, 1.0, confirmed=True,
                                  observed_mono=rec.mono_ts,
                                  source_epoch=(rec.pid, rec.generation),
                                  battle_id=rec.battle_id)
        return None


__all__ = [
    "PacketStateSource",
    "PacketBackedSource",
    "DISCOVER_RETRY_SEC",
    "REFRESH_SEC",
    "FLOW_MISS_LIMIT",
    "EVENT_EXTINCT_SEC",
    "FRAME_SILENCE_SEC",
]
