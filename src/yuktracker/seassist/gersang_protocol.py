"""거상 s2c 프로토콜 스트리밍 프레이머 — 전투 진입/종료 추출 (2026-07-30, 패킷 감지 P3).

오프라인 검증(`17. Packet study`)에서 확정한 프레이밍을 **증분(스트리밍)** 형태로 옮긴 것.
이 파일이 **정본**이고, 저쪽은 캡처 파일 분석·검증용이다.

프레이밍::

    frame = [length:2 LE][body]          length = len(body) + 2   (자기 자신 포함)
    body  = [magic:1 = 0x00][opcode:2 LE][reserved:1 = 0x00][payload...]

전투 판별 (캡처 5종 + 클라이언트 교체 전후 교차 검증)::

    ENTER = opcode 0x03f0  AND  body[5] == 0x02
    EXIT  = opcode 0x0fa4  AND  body[5] == 0x01

⚠️ **길이로 판별하면 안 된다.** 두 신호 모두 본문 길이가 클라/내용에 따라 크게 바뀐다
(ENTER 5,111~5,704 → 6,700 / EXIT 914~1,358 → 560). `body[5]` 만 안정적이다.

⚠️ **`body[0] == 0x00` 단정이 핵심이다.** 없으면 프레이머가 증가하는 LE-16 카운팅 배열
(`14 00 16 00 18 00 …`)을 프레임으로 오인해 유령 opcode `0x1600`/`0x2800` 을 만든다.
구 문서가 "원인 미상 misalign" 이라 적었던 것의 정체가 이것이다.

⚠️ **opcode 는 클라이언트 버전 간에 바뀐다(관측된 사실).** 구 EXIT 후보 `0x32aa` 는
클라 교체 후 완전히 소멸했다. 참고용으로 상수만 남기고 판별에는 쓰지 않는다.

두 클래스, 그리고 어느 쪽을 써야 하는가
---------------------------------------
- **`FlowDecoder` 가 프로덕션 진입점이다.** TCP 세그먼트를 도착 순서 그대로 먹인다.
- `StreamFramer` 는 그 아래의 바이트 지향 계층이다. **연속된 스트림 바이트만** 받는다.

pcap 은 커널 재조립 **전**의 raw 패킷이라 s2c 재전송 18~33% 가 그대로 들어온다. 도착
순서로 이어붙이면 재전송된 ENTER 프레임이 **두 번 발화**한다 — 조기 발화까지 얹히면
세그먼트 하나만으로도 헤더 6바이트가 다시 완성되므로 더 나쁘다. seq 처리를 건너뛰면
안 되는 이유다 (`tests/test_gersang_protocol.py` 가 이 실패 모드를 코드로 고정한다).

이 모듈이 하지 않는 것
----------------------
- npcap·소켓·Ethernet/IP/TCP 헤더 파싱 (상위 계층). 이미 벗겨진 payload 만 받는다.
- 흐름 ↔ 슬롯 매핑, 방향/포트 필터 (BPF + 상위 계층).
- **"전투 중" 상태 유지.** 에지만 낸다 — 재락 창이 ENTER 를 삼킬 수 있어 교대를
  가정하지 않는다. 상태 판정과 이중 ENTER 흡수는 상위 계층의 몫이다.
- 화면 폴백 판단. 다만 판단에 필요한 원신호(`locked`, `frames`, `last_frame_ts`,
  `last_event_ts`, `resyncs`, `opcode_counts`)는 `stats` 로 노출한다. 프레이머 단독으로는
  "기대 opcode 소멸" 과 "그냥 유휴" 를 구분할 수 없다 — 화면 소스를 아는 계층에서만 가능.
- **로깅.** 핫패스이기도 하지만, 더 큰 이유는 `get_logger()` 가 `config`/`log_history` 를
  끌어와 아래의 stdlib 전용 성질을 깨기 때문이다. 보고 가치가 있는 것은 전부 카운터다.
- 설정·스레드·타이머·디스크.

계약
----
- `ts` 는 **단조 시계**(`time.monotonic()`)여야 한다. 홀 예산을 차분으로 재기 때문이다.
  벽시계가 필요하면 상위가 수신 시점에 따로 잡아 이벤트와 짝지어라.
- `BattleEvent.body_len` 은 **선언 길이**다 — 조기 발화 시점엔 아직 안 받은 바이트를
  포함한다. "받은 바이트 수" 가 아니다.
- **인스턴스당 단일 스레드 접근**을 전제하며 락이 없다. 상위 설계상 스니퍼 스레드 1개가
  모든 흐름을 소유한다. ⚠️ 흐름 재매핑(재접속으로 로컬 포트 변경) 시의 `reset()` 도
  **스니퍼 스레드에서** 해야 한다. 5초 주기 갱신 타이머에서 직접 부르면 계약 위반이다.
- **stdlib 전용, 상대 import 0.** 오프라인 프로젝트가 `importlib.util.spec_from_file_location`
  으로 (`src` 패키지 충돌 없이) 이 파일을 직접 로드해 상수·판정 함수를 단일 소스화할 수
  있게 하기 위함이다. 실제로 드리프트한 것은 알고리즘이 아니라 **상수**였다(`0x32aa`).

이식 원본 (읽기용):
`17. Packet study/src/frame/resync_framer.py`, `.../src/analysis/battle_events.py`,
`.../src/flow/stream_assembler.py`, `.../docs/phase6-warfield-inout.md`
"""
from __future__ import annotations

from dataclasses import dataclass

# ─────────────────────────── 프레이밍 상수 ───────────────────────────
LEN_PREFIX = 2
MIN_BODY = 4  # magic + opcode(2) + reserved
#: u16 길이는 접두 자신을 포함한다. 실측 ENTER 본문 16,600~17,798B를
#: 옛 16,386B 상한이 거부하고 본문 내부에서 재락했다(2026-09-11 코퍼스 회귀).
#: 관측 최대값을 프로토콜 상한으로 쓰지 않는다.
MAX_BODY = (1 << (8 * LEN_PREFIX)) - 1 - LEN_PREFIX
MAGIC = 0x00

SUBTYPE_OFFSET = 5
#: `body[0:6]` (magic+opcode+reserved+subtype) 만 있으면 판정이 확정된다.
HEADER_DECIDE_LEN = SUBTYPE_OFFSET + 1

# ─────────────────────────── 전투 판별 ───────────────────────────
OP_BATTLE_ENTER = 0x03F0
OP_BATTLE_EXIT = 0x0FA4
ENTER_SUBTYPE_VALUE = 0x02
EXIT_SUBTYPE_VALUE = 0x01

#: 구 클라이언트 전용 — 교체 후 소멸 확인(2026-07-30). **판별에 쓰지 않는다.**
OP_BATTLE_EXIT_LEGACY = 0x32AA

# ─────────────────────────── 포만감 판별 ───────────────────────────
#: `0x1772` 는 단일 메시지가 아니라 **컨테이너**다 — 본문의 u32 sub-id 가 실제 종류를
#: 가른다. 2000=포만감(body 13B), 2001=전투 결산으로 보이는 표(body 101B/109B).
#: 채굴기가 opcode 단위로만 상관을 봐서 건수 많은 2001 이 전체 성격을 덮었고, 그래서
#: 2026-07-31 까지 "전투 결산 계열"로 오분류돼 있었다(PACKET-FINDINGS §5).
OP_STATUS = 0x1772
SUB_FULLNESS = 2000

#: sub-id u32 위치. `SUBTYPE_OFFSET`(단일 바이트) 로 읽으면 2000/2001 이 `0xd0`/`0xd1` 로
#: 우연히 갈리지만, `0x30dd` 에서 body[5] 를 subtype 으로 오해했던 것과 같은 함정이다
#: (FOLLOWUP F-203-1) — u32 전체를 읽는다.
_SUB_ID_SLICE = slice(5, 9)
_FULLNESS_VALUE_SLICE = slice(9, 13)
_FULLNESS_BODY_LEN = 13
#: OP_STATUS 컨테이너는 이 길이 이하면 **전량 대기** 후 파싱한다(패킷 PR-F 일반화).
#: 포만감 13B·파티 표 101/109B 모두 판정 바이트가 조기 발화 경계(need=8) 밖이라
#: 조기 발화하면 본문을 SKIP 예산이 버린다. 상한을 두는 이유는 5~6.7KB ENTER 급의
#: 큰 프레임에서 조기 발화(대기 비용 0 이 아님)를 유지하기 위해서 — 실측 OP_STATUS
#: 최대는 109B 라 여유가 넉넉하다.
_STATUS_FULL_WAIT_MAX_BODY = 256

#: 인게임 클램프. 전투 in 1회당 레벨×10 감소하되 0 밑으로는 안 내려가고, 급식으로도
#: 4000 을 넘지 않는다 (창 210348 slot2 전수 + 사용자 확인).
FULLNESS_MIN = 0
FULLNESS_MAX = 4000

#: 같은 컨테이너의 두 번째 sub-id — **12칸 용병 파티 표** (PACKET-FINDINGS §5.2, 2026-08-03
#: 구조 확정 / 의미 일부 미확정). 전투 EXIT 와 같은 버스트로 도착(n=83, Δ 중앙 0.000s).
#:   body = 헤더 5B | sub-id u32=2001 | count u8 | 3B 미상 | 레코드 8B × count
#:   레코드 = [u8 칸번호 0..11][3B 미상][u32 값]
#: 첫 바이트는 순번이 아니라 **칸 번호**다(결측 칸 = 편성 공백). 값은 정적 속성으로 보이고
#: `0` 이 유일한 비정적 값(용병 사망 후보, n=1 — FOLLOWUP F-203-5 결정 실험 대기).
SUB_PARTY = 2001
PARTY_CELLS = 12
_PARTY_HDR_LEN = 13          # 헤더 5 + sub-id 4 + count 1 + 미상 3
_PARTY_REC_LEN = 8
_PARTY_COUNT_OFFSET = 9
_PARTY_HDR3_SLICE = slice(10, 13)

# ─────────────────────────── 조철 판별 ───────────────────────────
#: 조철(제련) 팝업 신호. 발생 2창 전건 포착 / 음성 35,787프레임 0건, 화면 감지보다
#: 0.44~0.69s 선행 (PACKET-FINDINGS §4, FOLLOWUP F-203-1, 2026-07-31 확정). 마커
#: `0x03f0`(sub=14)와 같은 버스트로 오며 이 프레임 하나로 즉시 발화한다 — 패치 전 3건과
#: 09-15 F1_JBY 는 조철이 마커보다 0.1~0.5ms 먼저, 09-17 두 건은 마커가 먼저라
#: **순서를 조건으로 쓰지 않는다**.
#: ⚠️ "조철이 풀렸다"는 패킷으로 알 수 없다(신호 이후 창 끝까지 비-베이스라인 s2c 0건,
#: 2026-09-17 수동 제출 표본에서도 제출 응답 0건).
OP_JOCHUL = 0x30DD
#: H-2609-06: 2026-09-14~15 클라 패치 뒤의 조철 번호(2026-09-17 두 PC 실조철 2/2,
#: 같은 기간 `0x30dd` 0건 — docs/PACKET-JOCHUL-PATCH-2026-09-17.md). 관측된 번호만 추가한다:
#: 캡차는 −8, 조철은 +2 로 움직여 산술 오프셋 추정이 성립하지 않는다.
OP_JOCHUL_PATCHED = 0x30DF
#: 관측된 조철 번호 전부 — 판별·헬스 줄·경고 문구·채굴기·패치 점검이 **이 집합 하나**에서
#: 파생한다. 번호는 패치마다 옮겨 갈 수 있고, 한 곳이라도 빠지면 "왔는데 못 셌다"가 재발한다.
JOCHUL_OPCODES = frozenset({OP_JOCHUL, OP_JOCHUL_PATCHED})
#: body[5:10] 은 인스턴스 ID(매 창 변동)라 판정에 쓰지 않는다 — `0x30dd` 에서 body[5] 를
#: subtype 으로 오해했던 함정과 같다.
#: body[10:13] 꼬리도 **어느 계열에서든 조건이 아니다**(H-2607-05 기각). 꼬리는 번호가 아니라
#: 전달 경로를 따라 변한다 — 조철이 마커보다 먼저 온 4건은 `0c 14 00`×3(구 번호)·`0b 14 00`
#: (신 번호 F), 마커가 먼저 온 2건은 `00 00 00`. 구 번호에만 꼬리를 남겨 두면 번호가 되돌아오고
#: 마커가 먼저 오는 날 알람을 놓친다. 구 번호 len 13 은 전 코퍼스에서 양성 3건뿐이라(음성 0)
#: 꼬리를 빼도 오탐이 늘지 않는다.
_JOCHUL_BODY_LEN = 13

# H-2609-03/04/05: packet observations, never battle events.
# body[11] is variable (0 and 1 in reviewed positives); do not filter on it.
# Keep the pre-patch family for replay. Only the observed September 14 family
# is added; other opcode numbers do not inherit a guessed arithmetic offset.
_WORDINPUT_SHAPES = {0x3241: (17, "open"), 0x3243: (21, "refresh"),
                     0x3239: (17, "open"), 0x323B: (21, "refresh")}
_WORDINPUT_QUEUE_MAX = 64
_WORDINPUT_POPUP_MARKER = bytes.fromhex("0b0000f00300000e000000")
#: 팝업 마커 옆에 와야 할 "알려진 동반" 번호(조철 + 캡차 open) — 번호 이동 진단축
#: (`StreamFramer.popup_unknown`). 본문 모양은 보지 않는다: 모양 드리프트는 `jochul_rejected`·
#: `wordinput_kind` 쪽 몫이고, 여기서 묻는 것은 "아는 번호가 왔는가" 하나다.
_POPUP_COMPANION_OPCODES = JOCHUL_OPCODES | frozenset(
    op for op, (_len, kind) in _WORDINPUT_SHAPES.items() if kind == "open")
# 라이브 판정은 `need`(헤더 8B)만으로 끝나므로 마커도 (opcode, subtype, 길이)로 알아본다.
_POPUP_MARKER_OPCODE = int.from_bytes(_WORDINPUT_POPUP_MARKER[3:5], "little")
_POPUP_MARKER_SUBTYPE = _WORDINPUT_POPUP_MARKER[LEN_PREFIX + SUBTYPE_OFFSET]
_POPUP_MARKER_BODY_LEN = len(_WORDINPUT_POPUP_MARKER) - LEN_PREFIX


def wordinput_kind(opcode: int, body: bytes) -> str | None:
    """Classify a complete candidate body; unknown layouts return None."""
    shape = _WORDINPUT_SHAPES.get(opcode)
    if (shape is None or len(body) != shape[0] or body[0] != MAGIC
            or int.from_bytes(body[1:5], "little") != opcode):
        return None
    return shape[1]

#: idle 5분 베이스라인에서 관측된 opcode. `0x03ee` 는 1.2Hz 틱(간격 중앙값 ~0.83s).
IDLE_BASELINE_OPCODES = frozenset({0x03EE, 0x03F9, 0x044C, 0x03FA, 0x1F82, 0x2711})

ENTER = "enter"
EXIT = "exit"

# ─────────────────────────── 튜닝 ───────────────────────────
#: 라이브·오프라인 기본 락 증거 수.
DEFAULT_LOCK_K = 8
#: LOCKING 상태의 탐색 윈도우. 상한에 닿으면 백오프 없이 재검사한 뒤 확정 거부만 버린다.
#: ⚠️ 락하려면 k개 프레임이 **동시에** 버퍼에 상주해야 하므로, 이 값은 곧 "락 가능한
#: 프레임 평균 크기"의 상한이다. 최대 길이 프레임 k개를 담을 공간을 확보해
#: 락 전 큰 프레임의 분할 도착도 보존한다. 기본 약 512KiB로 유한하다.
_LOCK_WINDOW = DEFAULT_LOCK_K * (MAX_BODY + LEN_PREFIX)
#: 어떤 상태에서든 버퍼 상한 (방어). 정상 동작에서는 도달하지 않는다.
_MAX_BUFFER = 2 * _LOCK_WINDOW
#: 락 재시도 간격 — 실패 후 "버퍼 크기 / 이 값" 만큼 더 쌓이기 전에는 재스캔하지 않는다.
#: 스캔 비용이 O(버퍼) 이므로 고속 수신 중 간격을 버퍼에 비례시켜 반복 탐색을 줄인다.
#: (여기서는 바이트당 약 4 프로브). 없으면 미락 상태에서 매 feed 마다 윈도우 전체를
#: 재스캔해 처리량이 수백 배로 떨어진다(리뷰 실측: 미락 39 KB/s vs 락 10,973 KB/s).
#: 저속 수신은 아래 시간 상한으로 별도 재검사한다. 바이트만 기다리면 큰 첫 ENTER 뒤
#: 작은 틱만 오는 동안 완성된 경계도 수십 초 이상 처리되지 않을 수 있다.
_SCAN_GAP_DIVISOR = 4
_SCAN_GAP_MIN = 256
#: A large first frame followed by small ticks must not wait for another quarter
#: buffer of traffic. Retry on the next payload after this monotonic interval.
_SCAN_MAX_WAIT_SEC = 0.5
#: 버퍼가 이보다 작으면 백오프를 걸지 않는다 — 정상 락은 실측 중앙값 216B / 최대 2,286B
#: 안에 끝나므로, 그 구간에 지연을 넣으면 이득 없이 이 기능의 존재 이유만 갉아먹는다.
#: 작은 버퍼 전수 스캔은 어차피 값싸고, CPU 문제는 버퍼가 커진 뒤에만 생긴다.
_SCAN_ALWAYS_BELOW = 4096
#: 전투 ENTER 앵커 — 미락 상태에서 "완성된 ENTER + 바로 뒤 유효 프레임" 이면 k 와 무관하게
#: 락한다. 성긴 스트림(F1_JBY 단독 사냥 ≈0.87 세그먼트/s)은 k=8 을 채우는 데 중앙값 13s·
#: 최대 30s 가 걸려, 시작 직후 첫 ENTER 가 백로그에서 9~22s 늦게 재생됐다
#: (2026-09-18, docs/PACKET-COLDSTART-2026-09-18.md). 이 헤더 6바이트(magic + opcode u32 +
#: subtype)는 3기기 72창 5.2MB 에서 진짜 ENTER 246건에만 나왔다(다른 위치 0건).
_ENTER_ANCHOR_HEAD = (bytes((MAGIC,)) + OP_BATTLE_ENTER.to_bytes(4, "little")
                      + bytes((ENTER_SUBTYPE_VALUE,)))
#: 실측 ENTER 전체 길이는 6,459~17,881B 다. 같은 opcode 의 소형 프레임을 앵커로 삼지 않게
#: 하한을 둔다(팝업 마커 `0x03f0 sub=14` 는 subtype 에서 이미 갈린다).
_ENTER_ANCHOR_MIN_TOTAL = 2048
#: opcode 상위 2바이트의 프레임 내 위치 — 관측된 전 프레임(3,283건)에서 0 이다. 앵커 뒤
#: 프레임 검증에만 쓴다(일반 락·잠금 파서의 판별식은 바꾸지 않는다).
_OPCODE_HIGH_OFFSET = LEN_PREFIX + 3
#: opcode 히스토그램 키 상한 — 손상된 스트림이 메모리를 갉아먹지 않게.
_OPCODE_HIST_CAP = 64

_SEQ_MOD = 1 << 32

__all__ = [
    # 진입점을 먼저 — StreamFramer 직접 사용은 재전송 이중 발화를 부른다.
    "FlowDecoder",
    "StreamFramer",
    "BattleEvent",
    "WordinputObservation",
    "wordinput_kind",
    "FramerStats",
    "FlowStats",
    "classify",
    "is_enter",
    "is_exit",
    "is_fullness",
    "fullness_value",
    "is_jochul",
    "is_party_table",
    "parse_party_table",
    "PartyRecord",
    "PartyTable",
    "SUB_PARTY",
    "PARTY_CELLS",
    "frame_total_len",
    "is_valid_frame_at",
    "find_boundary",
    "ENTER",
    "EXIT",
    "OP_BATTLE_ENTER",
    "OP_BATTLE_EXIT",
    "OP_BATTLE_EXIT_LEGACY",
    "OP_STATUS",
    "SUB_FULLNESS",
    "FULLNESS_MIN",
    "FULLNESS_MAX",
    "OP_JOCHUL",
    "OP_JOCHUL_PATCHED",
    "JOCHUL_OPCODES",
    "SUBTYPE_OFFSET",
    "ENTER_SUBTYPE_VALUE",
    "EXIT_SUBTYPE_VALUE",
    "IDLE_BASELINE_OPCODES",
    "DEFAULT_LOCK_K",
    "MAX_BODY",
]


# ─────────────────────────── 값 객체 ───────────────────────────


@dataclass(frozen=True)
class WordinputObservation:
    ts: float  # decoder completion timestamp; not physical popup onset
    kind: str  # open | refresh; neither implies that the popup is still open
    opcode: int
    body: bytes


@dataclass(frozen=True)
class BattleEvent:
    ts: float
    kind: str  # ENTER | EXIT
    opcode: int
    #: **선언** 본문 길이 — 조기 발화 시점엔 아직 안 받은 바이트를 포함한다.
    body_len: int
    #: Opt-in complete EXIT body; ENTER retains its low-latency header event.
    body: bytes | None = None


@dataclass(frozen=True)
class FramerStats:
    frames: int
    events: int
    resyncs: int
    locks: int
    resets: int
    #: **프레이밍 불가**로 버려진 바이트 — 락 오프셋 앞 구간 + 재동기 스킵 + 윈도우 트림.
    #: 조기 발화로 건너뛴 정상 본문은 여기가 아니라 `body_skipped` 다(SKIP 상태명과
    #: 헷갈리기 쉬워 일부러 분리했다).
    bytes_skipped: int
    #: 판정 후 읽지 않고 버린 프레임 본문 — 조기 발화가 실제로 아낀 양. 정상 동작이다.
    body_skipped: int
    #: `_MAX_BUFFER` 초과 = 스트림 손상 신호 (reset 동반).
    overflows: int
    #: 락 탐색 윈도우 트림 — **양성 동작**이다. `overflows` 와 뭉치면 오탐한다.
    trims: int
    locked: bool
    locked_at: float
    last_frame_ts: float
    last_event_ts: float
    opcode_counts: tuple[tuple[int, int], ...]
    #: 히스토그램 상한에 막혀 기록되지 못한 신규 opcode 수 (전투 2종·조철 번호는 항상 통과).
    opcodes_dropped: int
    #: `locks` 중 전투 ENTER 앵커로 잡은 횟수 — 0 이 아니면 k 프레임을 채우기 전에 전투가
    #: 시작됐다는 뜻이다(성긴 흐름의 세션 시작·재락).
    enter_anchor_locks: int = 0
    #: **지금의** 락이 앵커 락인가. 위 카운터는 누적이라 "방금 안내하는 락" 을 말해 주지 못한다 —
    #: 앵커 락이 풀린 뒤의 일반 락까지 앵커라고 부르게 된다.
    anchor_locked: bool = False


@dataclass(frozen=True)
class FlowStats:
    framer: FramerStats
    segments: int
    dup_segments: int
    dup_bytes: int
    reordered: int
    hole_resets: int
    pending_bytes: int


@dataclass(frozen=True)
class PartyRecord:
    """파티 표 레코드 1개 = 칸 1개. ``raw3`` 는 정체 미상 3B(보고마다 바뀌는 칸이
    많아 ID 로 쓸 수 없다 — 미초기화 패딩 의심)."""

    cell: int      # 0..11
    raw3: bytes
    value: int     # u32 — 정적 속성(최대HP/전투력?), 0 = 사망 후보


@dataclass(frozen=True)
class PartyTable:
    """`0x1772` sub=2001 한 건. ``count`` 11 → 한 칸 결측(편성 공백), 12 → 전 칸."""

    count: int
    hdr3: bytes    # body[10:13] 미상
    records: tuple[PartyRecord, ...]

    def value_of(self, cell: int) -> int | None:
        for r in self.records:
            if r.cell == cell:
                return r.value
        return None

    @property
    def cells(self) -> tuple[int, ...]:
        return tuple(r.cell for r in self.records)

    @property
    def missing_cells(self) -> tuple[int, ...]:
        have = {r.cell for r in self.records}
        return tuple(c for c in range(PARTY_CELLS) if c not in have)

    @property
    def zero_cells(self) -> tuple[int, ...]:
        return tuple(r.cell for r in self.records if r.value == 0)


# ─────────────────────────── 순수 판정 ───────────────────────────


def is_enter(opcode: int, subtype: int) -> bool:
    return opcode == OP_BATTLE_ENTER and subtype == ENTER_SUBTYPE_VALUE


def is_exit(opcode: int, subtype: int) -> bool:
    return opcode == OP_BATTLE_EXIT and subtype == EXIT_SUBTYPE_VALUE


def classify(opcode: int, subtype: int) -> str | None:
    """ENTER / EXIT / None. `subtype` 이 없는 프레임은 -1 을 넘긴다."""
    if is_enter(opcode, subtype):
        return ENTER
    if is_exit(opcode, subtype):
        return EXIT
    return None


def is_fullness(opcode: int, body: bytes) -> bool:
    """포만감 프레임인가 — opcode + 본문 길이 + sub-id 3중 판별.

    길이 단독은 불가하다(len=13 인 다른 opcode 가 14종 465건). `0x30dd` 조철 판별이
    세운 선례와 같은 형태다.
    """
    if opcode != OP_STATUS or len(body) != _FULLNESS_BODY_LEN:
        return False
    return int.from_bytes(body[_SUB_ID_SLICE], "little") == SUB_FULLNESS


def fullness_value(body: bytes) -> int | None:
    """포만감 수치(0~4000). 범위 밖이면 None.

    범위 가드는 클라이언트 패치 드리프트 방어다 — opcode 는 버전 간 바뀐다(구 EXIT
    `0x32aa` 소멸 전례). 같은 opcode 가 다른 뜻으로 재사용되면 값이 먼저 튀므로,
    호출부는 None 을 "이번 프레임은 포만감이 아니다"로 취급하고 조용히 넘긴다.

    ⚠️ `is_fullness` 로 거른 본문만 넘길 것 — 길이 검사를 다시 하지 않는다.
    """
    v = int.from_bytes(body[_FULLNESS_VALUE_SLICE], "little")
    return v if FULLNESS_MIN <= v <= FULLNESS_MAX else None


def is_party_table(opcode: int, body: bytes) -> bool:
    """용병 파티 표 프레임인가 — opcode + sub-id + count/길이 정합 3중 판별.

    `len(body) == 13 + 8*count` 이고 `count <= 12` 여야 한다(실측 101B/109B). 길이
    단독은 불가하고, sub-id 만으로도 부족하다 — 컨테이너 opcode 는 sub-id 로 갈라야
    한다는 §5.1 교훈 + 길이 정합으로 드리프트를 막는다.
    """
    if opcode != OP_STATUS or len(body) < _PARTY_HDR_LEN:
        return False
    if int.from_bytes(body[_SUB_ID_SLICE], "little") != SUB_PARTY:
        return False
    count = body[_PARTY_COUNT_OFFSET]
    if count > PARTY_CELLS:
        return False
    return len(body) == _PARTY_HDR_LEN + _PARTY_REC_LEN * count


def parse_party_table(body: bytes) -> PartyTable | None:
    """파티 표 본문 → `PartyTable`. 칸 번호가 범위 밖이거나 중복이면 None.

    ⚠️ `is_party_table` 로 거른 본문만 넘길 것 — 길이/sub-id 를 다시 검사하지 않는다.
    """
    count = body[_PARTY_COUNT_OFFSET]
    recs: list[PartyRecord] = []
    seen: set[int] = set()
    pos = _PARTY_HDR_LEN
    for _ in range(count):
        cell = body[pos]
        if cell >= PARTY_CELLS or cell in seen:
            return None
        seen.add(cell)
        recs.append(PartyRecord(
            cell, bytes(body[pos + 1:pos + 4]),
            int.from_bytes(body[pos + 4:pos + 8], "little")))
        pos += _PARTY_REC_LEN
    return PartyTable(count, bytes(body[_PARTY_HDR3_SLICE]), tuple(recs))


def is_jochul(opcode: int, body: bytes) -> bool:
    """조철 팝업 프레임인가 — 관측된 번호(`JOCHUL_OPCODES`) + 본문 길이 13 + 헤더.

    길이 단독은 불가하다(len=13 인 다른 opcode 가 14종 465건). body[5:10] 인스턴스
    ID 는 매 창 변동이라 건드리지 않는다. 헤더는 magic 과 opcode u32 전체(body[1:5])를
    대조한다 — 호출자가 넘긴 opcode 와 본문이 어긋나거나 예약 바이트(body[3:5])가 0 이
    아니면 거부. 꼬리 body[10:13] 은 어느 계열에서도 보지 않는다(`JOCHUL_OPCODES` 주석).
    """
    if opcode not in JOCHUL_OPCODES or len(body) != _JOCHUL_BODY_LEN:
        return False
    return body[0] == MAGIC and int.from_bytes(body[1:5], "little") == opcode


# `_probe` 반환 규약 — 오프라인 `_frame_len_at` 이 둘을 뭉뚱그려 None 으로 돌려주던 것을
# 라이브에서는 반드시 갈라야 한다 (이식 델타 B).
_PROBE_PENDING = 0  # 더 와봐야 안다
_PROBE_REJECT = -1  # 여기는 프레임 경계가 아님이 확정


def _probe(buf: bytes, pos: int) -> int:
    """`pos` 에서 시작하는 프레임의 전체 길이(>0) / PENDING(0) / REJECT(-1)."""
    n = len(buf)
    if pos + LEN_PREFIX > n:
        return _PROBE_PENDING
    total = int.from_bytes(buf[pos : pos + LEN_PREFIX], "little")
    body_len = total - LEN_PREFIX
    if body_len < MIN_BODY or body_len > MAX_BODY:
        return _PROBE_REJECT
    if pos + LEN_PREFIX >= n:
        return _PROBE_PENDING  # magic 미도착 — 거부가 아니다
    if buf[pos + LEN_PREFIX] != MAGIC:
        return _PROBE_REJECT
    if pos + total > n:
        return _PROBE_PENDING
    return total


def frame_total_len(buf: bytes, pos: int = 0) -> int | None:
    """완전히 도착한 유효 프레임의 전체 길이. 아니면 None."""
    r = _probe(buf, pos)
    return r if r > 0 else None


def is_valid_frame_at(buf: bytes, pos: int = 0) -> bool:
    return _probe(buf, pos) > 0


def _find_lock(buf: bytes, start: int, k: int) -> tuple[int | None, int]:
    """(락 offset 또는 None, 확정거부 상한).

    확정거부 상한 r 은 "offset < r 은 영원히 경계가 아님이 증명됐다" 는 뜻이다. 다음
    feed 는 r 부터 재개한다 (델타 C). 거부는 **단조**다 — 길이 범위 위반과 magic 불일치는
    이미 도착한 바이트로 결정되므로 데이터가 더 와도 뒤집히지 않는다. 그래서 캐시해도 안전하다.

    ⚠️ 다만 이것만으로 재스캔 비용이 사라지지는 **않는다**. 앞쪽에 PENDING 후보가 하나만
    있어도 r 이 거기 못 박혀 그 뒤 전체가 매 feed 재스캔된다(리뷰 실측: 미락 시 처리량이
    락 상태의 1/280). 그래서 호출자(`StreamFramer.feed`)가 실패 후 백오프를 건다.

    ⚠️ **PENDING 은 스캔을 멈추지 않는다.** 후보 하나가 "선언 길이가 버퍼를 넘어서 아직
    모름" 이라고 해서 그 뒤를 안 보면, 쓰레기 구간에 우연히 생긴 큰 길이 후보 하나가
    **뒤따라오는 진짜 경계를 통째로 가려버린다**(재락 실패). PENDING 후보는 재개 지점으로
    기억만 하고 스캔은 계속한다.

    ⚠️ 오프라인 `find_boundary` 에 있는 **"버퍼 끝까지 유효하게 소진 = 사실상 확정"
    지름길을 옮겨오면 안 된다** (델타 A). 오프라인에선 버퍼가 스트림 전체라 옳지만,
    라이브에서 버퍼 끝은 "확정" 이 아니라 "아직 모름" 이다. 그 줄을 남기면 프레임
    **1개**만으로 락이 걸려 k 가 조용히 1 로 퇴화한다.
    """
    n = len(buf)
    pos = start
    first_pending: int | None = None
    while pos < n:
        # Every complete candidate needs magic at pos + LEN_PREFIX. Skip known
        # rejections in C, keeping the final prefix bytes for PENDING handling.
        magic_pos = buf.find(b"\x00", pos + LEN_PREFIX)
        pos = (magic_pos - LEN_PREFIX if magic_pos >= 0
               else max(pos, n - LEN_PREFIX))
        cur = pos
        cnt = 0
        pending = False
        while cnt < k:
            r = _probe(buf, cur)
            if r == _PROBE_PENDING:
                pending = True
                break
            if r == _PROBE_REJECT:
                break
            cur += r
            cnt += 1
        if cnt >= k:
            return pos, pos
        if pending and first_pending is None:
            first_pending = pos  # 아직 확정 거부가 아니다 — 다음 feed 의 재개 지점
        pos += 1
    return None, n if first_pending is None else first_pending


def _enter_anchor(buf: bytes, start: int, k: int = DEFAULT_LOCK_K) -> tuple[int | None, int]:
    """(ENTER 앵커 offset 또는 None, 다음 호출의 재개 지점).

    앵커 = 완성된 ENTER 프레임 + 그 끝에서 정확히 시작하는 완성된 유효 프레임. 6KB 이상을
    건너뛴 자리가 다시 ``[len][00][opcode][00 00]`` 이라는 것은 헤더 6바이트 일치에 더해지는
    독립 증거다. 뒤 프레임은 실측 중앙값 2.7ms·최대 48ms 안에 같은 버스트로 온다(n=246).

    **거부권**: ENTER 부터 k 프레임 안에 확정 무효 프레임이 이미 도착해 있으면 후보를 버린다.
    진짜 흐름에서 유효 프레임 뒤는 언제나 유효 프레임이므로, 그 체인은 일반 스캔이 방금
    기각한 바로 그 체인이다 — 거기서 락하면 IN 을 발행하고 곧바로 재동기한다(팝업 쌍 앵커와
    같은 규칙). 미도착(PENDING)은 거부가 아니다. 이 거부권이 `_chain_start` 의 전제도 지킨다.

    `_find_lock` 과 같은 규칙을 따른다. 거부는 단조(이미 도착한 바이트로 결정)라 재개 지점을
    앞으로 옮겨도 안전하고, PENDING 후보는 재개 지점으로만 기억하고 탐색은 계속한다 —
    끝나지 않는 가짜 후보 하나가 뒤따르는 진짜 ENTER 를 가리면 안 된다.
    """
    first_pending: int | None = None
    horizon = max(1, k - 1)  # ENTER 뒤로 볼 프레임 수 — 일반 락이 보는 k 프레임과 같은 폭
    i = buf.find(_ENTER_ANCHOR_HEAD, max(0, start) + LEN_PREFIX)
    while i >= 0:
        pos = i - LEN_PREFIX
        total = _probe(buf, pos)
        if total == _PROBE_PENDING:
            if first_pending is None:
                first_pending = pos
        elif total >= _ENTER_ANCHOR_MIN_TOTAL:
            cur = pos + total
            followers = 0
            verdict = _PROBE_PENDING
            while followers < horizon:
                verdict = _probe(buf, cur)
                if verdict <= 0:
                    break
                if followers == 0:
                    high = cur + _OPCODE_HIGH_OFFSET
                    if (verdict < _OPCODE_HIGH_OFFSET + 2
                            or buf[high:high + 2] != b"\x00\x00"):
                        verdict = _PROBE_REJECT
                        break
                cur += verdict
                followers += 1
            if verdict != _PROBE_REJECT:
                if followers:
                    return pos, pos
                if first_pending is None:
                    first_pending = pos
        i = buf.find(_ENTER_ANCHOR_HEAD, i + 1)
    if first_pending is not None:
        return None, first_pending
    # 헤더가 버퍼 끝에 걸쳐 있을 수 있다 — 그만큼은 다음 호출이 다시 본다.
    tail = len(buf) - (LEN_PREFIX + len(_ENTER_ANCHOR_HEAD)) + 1
    return None, max(start, tail, 0)


def _chain_start(buf: bytes, start: int, target: int) -> int:
    """`target` 에 **정확히** 닿는 유효 프레임 체인의 가장 앞선 시작점 (없으면 `target`).

    ENTER 앵커가 경계를 증명하면, 그 앞에 쌓인 프레임도 같은 체인 위에 있는 한 경계다.
    앵커 자리에서 락하고 앞을 버리면 같은 순간에 k-락이 잡혔을 때보다 프레임을 덜 보게 되어
    오프라인 재생과 어긋난다(필수 코퍼스 검사 `counts == arrival_order` 가 실제로 잡았다:
    HIC0TCR 07-31 18:49 창 92 → 86).

    `start` 는 호출자가 아는 확정 거부 상한이다 — 그 앞 offset 의 체인은 REJECT 로 끊겼다.
    끊긴 곳이 `target` 앞이면 `target` 에 닿지 못하고, 뒤라면 그 REJECT 는 ENTER 에서 k 프레임
    안이라 `_enter_anchor` 의 거부권이 앵커 자체를 이미 기각했다. 어느 쪽이든 다시 볼 필요가 없다.

    한 번 걸은 offset 은 다시 걷지 않는다(`dead`). 같은 offset 을 지나는 체인은 전부 같은
    곳에서 끊기므로, 후보마다 끝까지 다시 걸으면 "길게 이어지다 빗나가는" 백로그에서 제곱
    비용이 된다 — 이 함수는 IN 을 발행해야 하는 바로 그 feed 에서 스니퍼 스레드로 돈다.
    """
    dead: set[int] = set()
    magic = bytes((MAGIC,))
    pos = start
    while pos < target:
        # 모든 후보는 pos + LEN_PREFIX 에 magic 이 있어야 한다 — `_find_lock` 과 같은 건너뛰기.
        magic_pos = buf.find(magic, pos + LEN_PREFIX, target)
        if magic_pos < 0:
            break
        pos = magic_pos - LEN_PREFIX
        if pos not in dead:
            walked = []
            cur = pos
            while cur < target and cur not in dead:
                walked.append(cur)
                total = _probe(buf, cur)
                if total <= 0:
                    break
                cur += total
            if cur == target:
                return pos
            dead.update(walked)
        pos += 1
    return target


def find_boundary(buf: bytes, start: int = 0, k: int = DEFAULT_LOCK_K, *,
                  popup_anchor: bool = False,
                  enter_anchor: bool = False) -> int | None:
    """`start` 이후에서 연속 k개 프레임이 모두 유효한 첫 offset.

    ``popup_anchor=True`` 는 라이브 기본 디코더(`FlowDecoder()`)와 같은 조철 쌍 앵커를
    오프셋 0 에 허용한다 — 오프라인 재생(`mine.walk_frames`)이 k 프레임에 못 미치는 조철
    창에서 라이브와 어긋나지 않게 한다(F1_JBY 09-15: 라이브 5프레임·조철 1 vs 오프라인 0).
    캡차 앵커는 라이브에서도 opt-in(`observe_wordinput`)이라 여기에 넣지 않는다.

    ``enter_anchor=True`` 는 라이브의 ENTER 앵커(`_enter_anchor`)를 같은 규칙으로 허용한다.
    k-락과 앵커 체인의 시작점 중 **더 앞선 쪽**을 돌려준다. 보통은 k-체인이 앞서거나 같다
    (앵커 체인이 k 프레임을 채우면 그 시작점이 곧 k-락이다). 갈리는 것은 앵커 뒤에 끝나지 않는
    가짜 헤더가 있고 그 너머에서야 k-체인이 시작하는 경우다 — 라이브는 그 바이트가 오기 전에
    이미 앵커로 락했으므로, k-체인만 고르면 라이브가 보고한 ENTER 를 오프라인이 건너뛴다.
    """
    off = _find_lock(buf, start, k)[0]
    if (off is None and popup_anchor and start == 0
            and _popup_prefix_anchor(buf, k, wordinput=False)):
        return 0
    if enter_anchor:
        anchor = _enter_anchor(buf, start, k)[0]
        if anchor is not None:
            first = _chain_start(buf, start, anchor)
            if off is None or first < off:
                off = first
    return off


_WORDINPUT_OPEN_TOTAL = 17 + LEN_PREFIX
_JOCHUL_TOTAL = _JOCHUL_BODY_LEN + LEN_PREFIX


def _popup_prefix_anchor(buf: bytes, k: int, *, wordinput: bool) -> bool:
    """A complete popup companion with the exact popup marker adjacent can establish initial lock.

    The observed pair stands in for frames that have not arrived yet. The walk covers the
    same ``k`` frames the generic lock examines, from offset zero: only complete frame
    boundaries count, payload bytes are never searched, and a proven-invalid frame anywhere
    in that prefix vetoes the anchor — the generic scan has just rejected this very chain,
    and locking on it would fire and desync at once.

    Jochul (always on): F1_JBY 2026-09-15 01:06 — the session's resume action drew a jochul
    0.3s after the flow attached, five frames in total, so the eight-frame lock never formed
    and the popup passed unseen. The marker may sit on either side (before the patch and in
    that capture the jochul came first; in both 2026-09-17 captures the marker came first).

    Wordinput open (``wordinput`` only): the short 09-14 capture has seven frames before
    silence. Either order, like jochul (F-203-7 b, decided 2026-09-18): marker-first opens
    are 5 of 23 in the 2026-09 survey and all five real ones carry the same final field
    ``01 00 00 00`` (their ``body[12]`` is ``0x02``). That final field is an anchor
    constraint, not a general semantic rule. An anchored open only opens the runner's
    scan window — the screen detector still gates any key input.
    """
    pos = 0
    prev = None  # 직전 프레임 — "marker" | "jochul" | "open" | None
    found = False
    for _ in range(k):
        total = _probe(buf, pos)
        if total == _PROBE_REJECT:
            return False
        if total == _PROBE_PENDING:
            break
        cur = None
        if total == len(_WORDINPUT_POPUP_MARKER):
            if buf.startswith(_WORDINPUT_POPUP_MARKER, pos):
                cur = "marker"
                found = found or prev in ("jochul", "open")
        elif total == _JOCHUL_TOTAL:
            body = bytes(buf[pos + LEN_PREFIX:pos + total])
            if is_jochul(int.from_bytes(body[1:3], "little"), body):
                cur = "jochul"
                found = found or prev == "marker"
        elif wordinput and total == _WORDINPUT_OPEN_TOTAL:
            body = bytes(buf[pos + LEN_PREFIX:pos + total])
            if (wordinput_kind(int.from_bytes(body[1:5], "little"), body) == "open"
                    and body[13:17] == b"\x01\0\0\0"):
                cur = "open"
                found = found or prev == "marker"
        prev = cur
        pos += total
    return found


# ─────────────────────────── 바이트 계층 ───────────────────────────


class StreamFramer:
    """흐름 1개·방향 1개의 **연속 바이트**를 프레임으로 자른다.

    ⚠️ 이 클래스에 raw 세그먼트를 직접 먹이지 말 것 — 재전송이 이벤트를 이중 발화한다.
    `FlowDecoder` 를 써라.

    상태 3개
    --------
    - **LOCKING**: 경계 탐색 중. 버퍼는 후보 윈도우.
    - **HEADER**: `_buf[0]` 이 프레임 경계. 판정에 필요한 바이트를 모으는 중.
    - **SKIP**: 이미 판정·발화가 끝난 프레임의 잔여 본문을 버리는 중 (버퍼는 항상 빔).

    **불변식 — 판정 시점 == 소비 시점.** 프레임은 판정되는 순간 버퍼에서 사라지거나
    SKIP 예산으로 전환된다. "부분 프레임을 들고 있는" 상태가 존재하지 않으므로 중복
    발화가 **구조적으로** 불가능하다 (상태로 막는 게 아니라, 두 번 볼 바이트가 없다).

    조기 발화
    ---------
    ENTER 프레임은 5~6.7KB 라 TCP 세그먼트 여러 개에 걸친다. `body[0:6]` 만 확보되면
    본문 전체를 기다리지 않고 즉시 발화하고 나머지는 SKIP 으로 버린다.

    `lock_k` 가 오프라인(4)보다 큰 이유
    -----------------------------------
    오프라인의 오락(false lock)은 `RESULT: FAIL` 로 사람이 즉시 본다. 라이브의 유령
    `0x03f0` + 유령 `body[5]==0x02` 는 **가짜 ENTER = 게임에 실제 키 입력 발사**다.
    게다가 라이브는 세션 시작·재접속·채널이동·오버플로·홀 포기마다 재락해 시도 횟수가
    훨씬 많다(합집합 확률이 지배한다). 비용은 락 거리 중앙값 216B — 사냥 중 1초 미만이고,
    애초에 우리가 인용하는 88/88 실측이 k=8 에서 측정된 값이다.

    `lock_k` 의 유일한 예외 — 팝업 쌍 앵커
    --------------------------------------
    스트림 머리에 "동반 프레임 + 인접한 정확한 팝업 마커" 쌍이 있으면 k 와 무관하게 **2프레임**
    으로 락한다(`_popup_prefix_anchor` — 26~30B 정확 일치라 k개의 일반 프레임보다 강한 증거).
    조철 쌍은 기본 on, 캡차 쌍은 `observe_wordinput` 일 때만. k 만으로 증거를 통제해야 하는
    호출자는 ``popup_anchor=False`` 로 끈다.

    또 하나의 예외 — 전투 ENTER 앵커
    --------------------------------
    미락 버퍼 어디에서든 "완성된 ENTER + 바로 뒤 완성된 유효 프레임" 이 보이면 그 ENTER 에서
    락한다(`_enter_anchor`, 기본 on). k=8 은 프레임이 초당 1개도 안 오는 흐름에서 10~30s 가
    걸리고, 그 사이 도착한 ENTER 는 락 순간에 **현재 시각으로** 재생돼 전투 시퀀스를 한참
    늦게 쏜다. 락 offset 은 그 ENTER 에 정확히 닿는 유효 프레임 체인의 맨 앞이다
    (`_chain_start`) — 같은 순간에 k-락이 잡혔을 때와 같은 프레임을 보므로 오프라인 재생과
    어긋나지 않는다. 그 백로그 프레임의 ts 는 일반 락과 똑같이 락 시각이다.
    끄려면 ``enter_anchor=False``.
    """

    def __init__(self, lock_k: int = DEFAULT_LOCK_K, *,
                 retain_exit_body: bool = False,
                 observe_wordinput: bool = False,
                 popup_anchor: bool = True,
                 enter_anchor: bool = True) -> None:
        self.retain_exit_body = retain_exit_body
        self.observe_wordinput = observe_wordinput
        self.popup_anchor = popup_anchor
        self.enter_anchor = enter_anchor
        # ENTER 앵커 탐색 재개 지점 — 이 앞의 후보는 전부 확정 거부됐다(버퍼 offset).
        self._anchor_from = 0
        self._enter_anchor_locks = 0
        self._anchor_locked = False
        self._wordinput: list[WordinputObservation] = []
        self.wordinput_dropped = 0
        self._lock_k = max(1, int(lock_k))
        self._buf = bytearray()
        self._locked = False
        self._skip = 0
        self._scan_from = 0
        self._scan_gap = 0
        self._bytes_since_scan = 0
        self._last_scan_ts = float("-inf")
        self._opcodes: dict[int, int] = {}
        self._opcodes_dropped = 0
        self._frames = 0
        self._events = 0
        self._resyncs = 0
        self._locks = 0
        self._resets = 0
        self._bytes_skipped = 0
        self._body_skipped = 0
        self._overflows = 0
        self._trims = 0
        self._locked_at = 0.0
        self._last_frame_ts = 0.0
        self._last_event_ts = 0.0
        # 포만감은 전투 이벤트와 성격이 달라(에지가 아니라 수치) `feed` 반환값에 섞지
        # 않는다. 호출자가 feed 직후 `take_fullness()` 로 꺼낸다.
        self._fullness: list[tuple[float, int]] = []
        self._fullness_dropped = 0
        # 파티 표(0x1772 sub=2001) 드레인 채널 + 파싱 실패(칸 범위/중복) 카운터.
        self._party: list[tuple[float, PartyTable]] = []
        self._party_malformed = 0
        # 조철 번호(`JOCHUL_OPCODES`)인데 is_jochul(len 13 + 헤더 u32)에 떨어진 프레임 수.
        # 0 이 아니면 "도착은 했는데 판별식이 안 맞는다" — 클라 패치로 본문이 드리프트한
        # 것을 "조철 번호 미도착"과 구분하는 진단축(패킷 PR-C). ⚠️ 번호 자체가 옮겨 가면
        # 이 카운터도 0 이다(2026-09 패치: 0x30dd → 0x30df, 조철 0·거부 0 으로만 보였다).
        # 그 경우는 아래 `_popup_unknown` 이 잡는다.
        self._jochul_rejected = 0
        # 번호 이동 진단축 — 팝업 마커(0x03f0 sub=14)의 앞·뒤 어느 쪽에도 아는 동반 번호
        # (`_POPUP_COMPANION_OPCODES`)가 없던 횟수. 마커는 두 번의 번호 이동에도 그대로였고
        # 검토 폴더 69창의 마커 27건은 전부 아는 동반과 붙어 있었다(2026-09-17 전수).
        # `_prev_companion` None = 직전 프레임을 모름(락 직후) → 판단을 보류한다.
        self._popup_unknown = 0
        self._prev_companion: bool | None = None
        self._marker_waiting = False
        # 조철도 전투 IN/OUT 에지가 아니라 팝업 신호다 — `feed` 의 BattleEvent 로 섞으면
        # 상위 상태머신이 "out" 으로 오인한다(_on_packet). 별도로 꺼낸다.
        self._jochul: list[float] = []

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def buffered(self) -> int:
        """진단용 — 정상 상태에서는 세그먼트 1개 + 헤더 몇 바이트를 넘지 않는다."""
        return len(self._buf)

    @property
    def stats(self) -> FramerStats:
        top = sorted(self._opcodes.items(), key=lambda kv: (-kv[1], kv[0]))
        return FramerStats(
            frames=self._frames,
            events=self._events,
            resyncs=self._resyncs,
            locks=self._locks,
            resets=self._resets,
            bytes_skipped=self._bytes_skipped,
            body_skipped=self._body_skipped,
            overflows=self._overflows,
            trims=self._trims,
            locked=self._locked,
            locked_at=self._locked_at,
            last_frame_ts=self._last_frame_ts,
            last_event_ts=self._last_event_ts,
            opcode_counts=tuple(top),
            opcodes_dropped=self._opcodes_dropped,
            enter_anchor_locks=self._enter_anchor_locks,
            anchor_locked=self._locked and self._anchor_locked,
        )

    @property
    def fullness_dropped(self) -> int:
        """범위 밖이라 버린 포만감 프레임 누적 수 — 0 이 아니면 클라 패치 의심."""
        return self._fullness_dropped

    @property
    def jochul_rejected(self) -> int:
        """조철 번호지만 `is_jochul` 을 통과 못 한 프레임 누적 수 — 0 이 아니면
        조철 본문(길이 13·헤더 u32 의 예약 바이트) 드리프트 의심(도착 자체는 했다는 뜻)."""
        return self._jochul_rejected

    @property
    def popup_unknown(self) -> int:
        """팝업 마커 옆에 아는 동반 번호(조철·캡차 open)가 없던 누적 수 — 0 이 아니면
        **동반 번호 이동** 의심(2026-09 패치가 그랬다). `jochul_rejected` 가 못 보는 축이다."""
        return self._popup_unknown

    def take_fullness(self) -> list[tuple[float, int]]:
        """읽어둔 `(ts, 포만감)` 을 꺼내 비운다. 드레인하지 않으면 계속 쌓인다."""
        out = self._fullness
        self._fullness = []
        return out

    @property
    def party_malformed(self) -> int:
        """`is_party_table` 은 통과했으나 `parse_party_table` 이 거부한(칸 범위/중복)
        프레임 누적 수 — 0 이 아니면 레이아웃 드리프트 의심."""
        return self._party_malformed

    def take_party_tables(self) -> list[tuple[float, PartyTable]]:
        """읽어둔 `(ts, PartyTable)` 을 꺼내 비운다. 드레인하지 않으면 계속 쌓인다."""
        out = self._party
        self._party = []
        return out

    def take_jochul(self) -> list[float]:
        """읽어둔 조철 신호 `ts` 를 꺼내 비운다. 드레인하지 않으면 계속 쌓인다."""
        out = self._jochul
        self._jochul = []
        return out

    def take_wordinput(self) -> list[WordinputObservation]:
        """Drain opt-in observations, independently of battle events."""
        out, self._wordinput = self._wordinput, []
        return out

    def reset(self) -> None:
        """파스 상태만 비운다 — **누적 카운터는 보존**한다(관측 연속성)."""
        self.wordinput_dropped += len(self._wordinput)
        self._wordinput.clear()
        self._buf.clear()
        self._locked = False
        self._skip = 0
        self._scan_from = 0
        self._anchor_from = 0
        self._scan_gap = 0
        self._bytes_since_scan = 0
        self._last_scan_ts = float("-inf")
        self._locked_at = 0.0
        self._resets += 1
        self._prev_companion = None
        self._marker_waiting = False

    def feed(self, data: bytes, ts: float, *, wordinput_ts: float | None = None) -> list[BattleEvent]:
        events: list[BattleEvent] = []
        if not data:
            return events

        if self._skip:
            n = min(self._skip, len(data))
            self._skip -= n
            data = data[n:]
            if not data:
                return events

        self._buf += data
        self._bytes_since_scan += len(data)

        while True:
            if not self._locked:
                # 백오프 — 실패한 직후 같은 큰 버퍼를 다시 훑어봐야 결과가 거의 같다.
                # 작은 버퍼는 스캔이 값싸므로 지연 없이 항상 시도한다(정상 락 경로 보호).
                byte_scan_due = (len(self._buf) < _SCAN_ALWAYS_BELOW
                                 or len(self._buf) >= _LOCK_WINDOW
                                 or self._bytes_since_scan >= self._scan_gap)
                off: int | None = None
                scan_failed = False
                if byte_scan_due or ts - self._last_scan_ts >= _SCAN_MAX_WAIT_SEC:
                    # A timed retry supplements the byte deadline, never postpones
                    # it: the rest of a burst may complete the chain a moment later.
                    if byte_scan_due:
                        self._bytes_since_scan = 0
                    self._last_scan_ts = ts
                    off, rejected = _find_lock(self._buf, self._scan_from, self._lock_k)
                    if (off is None and self.popup_anchor
                            and _popup_prefix_anchor(self._buf, self._lock_k,
                                                     wordinput=self.observe_wordinput)):
                        off = 0
                    if off is None:
                        self._scan_from = rejected
                        scan_failed = True
                anchored = False
                if self.enter_anchor:
                    # 백오프 밖에서 **매 feed** 본다. 성긴 흐름은 다음 payload 가 수 초 뒤에
                    # 오므로 재검사 시한에 묶으면 앵커가 있어도 그만큼 늦는다. 비용은 새로
                    # 온 바이트(와 보류 후보 뒤)의 `find` 한 번이다. k-락이 같은 feed 에서
                    # 잡혔어도 더 앞선 쪽을 고른다 — `find_boundary` 와 같은 규칙.
                    anchor, self._anchor_from = _enter_anchor(
                        self._buf, self._anchor_from, self._lock_k)
                    if anchor is not None:
                        first = _chain_start(
                            self._buf, min(self._scan_from, anchor), anchor)
                        if off is None or first < off:
                            off = first
                            anchored = True
                if off is None:
                    self._trim_lock_window()
                    if scan_failed and byte_scan_due:
                        self._scan_gap = max(
                            _SCAN_GAP_MIN, len(self._buf) // _SCAN_GAP_DIVISOR
                        )
                    break
                self._consume(off, skipped=True)
                self._locked = True
                self._locks += 1
                self._enter_anchor_locks += anchored
                self._anchor_locked = anchored
                self._locked_at = ts
                self._scan_from = 0
                self._anchor_from = 0
                self._scan_gap = 0
                continue

            buf = self._buf
            n = len(buf)
            if n < LEN_PREFIX:
                break
            total = int.from_bytes(buf[0:LEN_PREFIX], "little")
            body_len = total - LEN_PREFIX
            if body_len < MIN_BODY or body_len > MAX_BODY:
                self._desync()
                continue
            if n <= LEN_PREFIX:
                break  # magic 미도착 — 대기 (거부가 아니다)
            if buf[LEN_PREFIX] != MAGIC:
                self._desync()
                continue
            need = LEN_PREFIX + min(body_len, HEADER_DECIDE_LEN)
            if n < need:
                break

            opcode = int.from_bytes(buf[LEN_PREFIX + 1 : LEN_PREFIX + 3], "little")
            subtype = (
                buf[LEN_PREFIX + SUBTYPE_OFFSET] if body_len > SUBTYPE_OFFSET else -1
            )
            complete_exit = (self.retain_exit_body
                             and classify(opcode, subtype) == EXIT)
            if complete_exit and n < total:
                break
            # ⚠️ OP_STATUS 컨테이너(포만감 body[9:13]·파티 표 13+8×count)는 판정
            # 바이트가 `need`=8 밖이라 조기 발화하면 나머지 본문이 SKIP 예산으로
            # 버려져 값을 영영 못 본다(세그먼트 분할 불변성 테스트가 잡은 실제 손실).
            # ≤256B 는 전량을 기다리는 비용이 사실상 0 이므로 여기서 기다린다 —
            # 조기 발화는 5~6.7KB ENTER 를 위한 장치지 작은 프레임을 위한 게 아니다.
            # 아직 아무 카운터도 올리지 않았으므로 재파스 시 이중 계상되지 않는다.
            if (opcode == OP_STATUS and body_len <= _STATUS_FULL_WAIT_MAX_BODY
                    and n < total):
                break
            # 조철도 동일 — `is_jochul` 은 완성 본문(길이 13)을 받는다. 조기 발화하면 잘린
            # 본문이 길이 검사에 떨어져 거부로 잘못 센다. 13B 프레임이라 전량 대기 비용 0.
            if opcode in JOCHUL_OPCODES and body_len == _JOCHUL_BODY_LEN and n < total:
                break
            word_shape = _WORDINPUT_SHAPES.get(opcode) if self.observe_wordinput else None
            if word_shape is not None and body_len == word_shape[0] and n < total:
                break

            self._frames += 1
            # ⚠️ max() — 재정렬 흡수 후 drain 은 **보류 당시의 ts** 로 재생되므로,
            # 무조건 대입하면 상위가 읽는 건강 신호가 뒤로 간다(리뷰에서 실측).
            self._last_frame_ts = max(self._last_frame_ts, ts)
            self._bump_opcode(opcode)
            self._note_popup_neighbour(opcode, subtype, body_len)
            if word_shape is not None and body_len == word_shape[0]:
                body = bytes(buf[LEN_PREFIX:total])
                if wordinput_kind(opcode, body) is not None:
                    if len(self._wordinput) < _WORDINPUT_QUEUE_MAX:
                        # Reordered bytes retain their arrival ts for existing events;
                        # wordinput records when assembly actually becomes possible.
                        observed_at = ts if wordinput_ts is None else wordinput_ts
                        self._wordinput.append(WordinputObservation(observed_at, word_shape[1], opcode, body))
                    else:
                        self.wordinput_dropped += 1
            if opcode == OP_STATUS:
                body = bytes(buf[LEN_PREFIX:total])
                if is_fullness(opcode, body):
                    value = fullness_value(body)
                    if value is None:
                        self._fullness_dropped += 1
                    else:
                        self._fullness.append((ts, value))
                elif is_party_table(opcode, body):
                    tbl = parse_party_table(body)
                    if tbl is None:
                        self._party_malformed += 1
                    else:
                        self._party.append((ts, tbl))
            elif opcode in JOCHUL_OPCODES:
                if is_jochul(opcode, bytes(buf[LEN_PREFIX:total])):
                    self._jochul.append(ts)
                else:
                    self._jochul_rejected += 1
            kind = classify(opcode, subtype)
            if kind is not None:
                events.append(BattleEvent(ts, kind, opcode, body_len,
                                         bytes(buf[LEN_PREFIX:total]) if complete_exit else None))
                self._events += 1
                self._last_event_ts = max(self._last_event_ts, ts)

            # 판정에 실제로 쓴 것은 `need` 바이트뿐 — 나머지 본문은 버린다.
            self._body_skipped += total - need
            if n >= total:
                self._consume(total)
            else:
                # 조기 발화 — 남은 본문은 SKIP 예산으로 넘긴다.
                self._skip = total - n
                self._consume(n)
                break

        # 방어 backstop. **파싱을 끝낸 뒤에** 본다 — append 직후에 보면 유효 프레임으로
        # 가득 찬 큰 feed 한 번이 멀쩡한 락과 파싱 가능한 바이트를 통째로 날린다
        # (리뷰 지적: 락 상태에서 280KB 단일 feed → frames 증가 0, 전량 skip).
        # 여기까지 왔다면 남은 바이트는 미완성 프레임 몇 개뿐이라 정상 경로엔 도달하지 않는다.
        if len(self._buf) > _MAX_BUFFER:
            self._overflows += 1
            self._bytes_skipped += len(self._buf)
            self.reset()
        return events

    def _desync(self) -> None:
        self._locked = False
        self._resyncs += 1
        # 현재 offset 0 은 경계가 아님이 확정됐다 — 다음 바이트부터 다시 찾는다.
        self._scan_from = 1
        self._anchor_from = 0
        self._prev_companion = None
        self._marker_waiting = False

    def _note_popup_neighbour(self, opcode: int, subtype: int, body_len: int) -> None:
        """팝업 마커의 앞·뒤 프레임이 아는 동반 번호인지 본다(`popup_unknown`)."""
        known = opcode in _POPUP_COMPANION_OPCODES
        if self._marker_waiting:
            self._marker_waiting = False
            if not known:
                self._popup_unknown += 1
        if (opcode == _POPUP_MARKER_OPCODE and subtype == _POPUP_MARKER_SUBTYPE
                and body_len == _POPUP_MARKER_BODY_LEN and self._prev_companion is False):
            self._marker_waiting = True  # 앞은 아니었다 — 뒤 프레임이 판정한다
        self._prev_companion = known

    def _consume(self, n: int, *, skipped: bool = False) -> None:
        if n <= 0:
            return
        del self._buf[:n]
        if skipped:
            self._bytes_skipped += n
        self._scan_from = max(0, self._scan_from - n)
        self._anchor_from = max(0, self._anchor_from - n)

    def _trim_lock_window(self) -> None:
        """락을 못 찾은 큰 버퍼에서 이미 거부된 앞부분만 버린다.

        상한 직전까지 PENDING이던 최대 프레임 체인을 백오프/트림으로 잃지 않는다.
        호출 전 상한 도달 시 재검사하므로 `_scan_from` 앞의 바이트만 안전하게 버린다.
        이건 **양성 동작**이라 `overflows`(스트림 손상 신호)가 아니라 `trims` 로 센다 —
        둘을 한 카운터에 뭉치면 상위가 "손상" 을 오탐한다.
        """
        if len(self._buf) <= _LOCK_WINDOW:
            return
        drop = min(self._scan_from, len(self._buf) - _LOCK_WINDOW // 2)
        if drop <= 0:
            return
        # 보류 중인 ENTER 후보는 일반 스캔에도 PENDING 체인이라 `_scan_from` 이 그 앞에
        # 머문다 — 트림이 후보를 자르는 일은 없고, 재개 지점만 같이 민다.
        del self._buf[:drop]
        self._bytes_skipped += drop
        self._scan_from = max(0, self._scan_from - drop)
        self._anchor_from = max(0, self._anchor_from - drop)
        self._trims += 1

    def _bump_opcode(self, opcode: int) -> None:
        cur = self._opcodes.get(opcode)
        if cur is not None:
            self._opcodes[opcode] = cur + 1
            return
        # 상한은 손상된 스트림이 메모리를 갉아먹는 걸 막으려는 것이지, 전투 신호를
        # 가리려는 게 아니다. 잡음 opcode 로 상한이 차버리면 `opcode_counts` 가
        # "기대 opcode 소멸" 판정 근거로 쓸 수 없게 되므로 두 opcode 는 항상 통과시킨다.
        # 조철 번호도 같다 — 수백 전투에 한 번 오는 번호라 긴 세션에선 상한이 먼저 차고,
        # 그러면 헬스 줄의 `30dd=`/`30df=` 가 "안 왔다"와 "못 셌다"를 가르지 못한다.
        if len(self._opcodes) >= _OPCODE_HIST_CAP and opcode not in (
            OP_BATTLE_ENTER,
            OP_BATTLE_EXIT,
        ) and opcode not in JOCHUL_OPCODES:
            self._opcodes_dropped += 1
            return
        self._opcodes[opcode] = 1


# ─────────────────────────── seq 계층 ───────────────────────────


def _seq_delta(a: int, b: int) -> int:
    """b 기준 a 의 부호 있는 거리 — 32bit wrap 안전."""
    d = (a - b) % _SEQ_MOD
    return d - _SEQ_MOD if d > _SEQ_MOD // 2 else d


class FlowDecoder:
    """⭐ 프로덕션 진입점. TCP 세그먼트를 **도착 순서 그대로** 밀어넣는다.

    중복 제거 · 겹침 절단 · 재정렬 흡수 · 홀 포기를 담당하고, 정리된 연속 바이트만
    `StreamFramer` 에 넘긴다.

    홀은 예산 안에서 **기다린다**(즉시 재동기하지 않는다). 기다리는 비용이 0 이기
    때문이다 — 같은 홀 때문에 게임 클라이언트의 커널 TCP 스택도 멈춰 있으므로 우리가
    기다려도 화면보다 뒤처지지 않는다. 반대로 즉시 reset 하면 중앙값 216B 의 재락 창이
    전투 이벤트를 삼킬 수 있다. (실측 홀 0건이라 사실상 죽은 경로지만, 죽은 채로 옳다.)

    주의: 흐름이 홀 상태로 침묵하면 `feed_segment` 가 안 불려 타임아웃이 돌지 않는다.
    디코딩할 데이터가 없으니 무해하고, 상위는 `stats.framer.last_frame_ts` 정체로 본다.
    """

    def __init__(
        self,
        lock_k: int = DEFAULT_LOCK_K,
        *,
        reorder_budget_bytes: int = 65536,
        reorder_budget_sec: float = 1.0,
        retain_exit_body: bool = False,
        observe_wordinput: bool = False,
        popup_anchor: bool = True,
        enter_anchor: bool = True,
    ) -> None:
        self.framer = StreamFramer(lock_k, retain_exit_body=retain_exit_body,
                                   observe_wordinput=observe_wordinput,
                                   popup_anchor=popup_anchor,
                                   enter_anchor=enter_anchor)
        self._budget_bytes = reorder_budget_bytes
        self._budget_sec = reorder_budget_sec
        self._next: int | None = None
        self._pending: dict[int, tuple[bytes, float]] = {}
        self._pending_bytes = 0
        # None = 타이머 미시작. 0.0 을 센티널로 쓰면 ts=0.0 인 첫 홀에서 타이머가
        # 시작되지 않는다(계약이 monotonic 을 요구하니 실사고는 아니지만 공짜로 막는다).
        self._gap_since: float | None = None
        self._segments = 0
        self._dup_segments = 0
        self._dup_bytes = 0
        self._reordered = 0
        self._hole_resets = 0

    @property
    def locked(self) -> bool:
        return self.framer.locked

    @property
    def fullness_dropped(self) -> int:
        return self.framer.fullness_dropped

    @property
    def jochul_rejected(self) -> int:
        return self.framer.jochul_rejected

    @property
    def popup_unknown(self) -> int:
        return self.framer.popup_unknown

    @property
    def party_malformed(self) -> int:
        return self.framer.party_malformed

    def take_fullness(self) -> list[tuple[float, int]]:
        """`feed_segment` 직후에 꺼낸다 — 재정렬 흡수분까지 함께 나온다."""
        return self.framer.take_fullness()

    def take_party_tables(self) -> list[tuple[float, PartyTable]]:
        """`feed_segment` 직후에 꺼낸다 — 재정렬 흡수분까지 함께 나온다."""
        return self.framer.take_party_tables()

    def take_jochul(self) -> list[float]:
        """`feed_segment` 직후에 꺼낸다 — 재정렬 흡수분까지 함께 나온다."""
        return self.framer.take_jochul()

    def take_wordinput(self) -> list[WordinputObservation]:
        return self.framer.take_wordinput()

    @property
    def wordinput_dropped(self) -> int:
        return self.framer.wordinput_dropped

    @property
    def stats(self) -> FlowStats:
        return FlowStats(
            framer=self.framer.stats,
            segments=self._segments,
            dup_segments=self._dup_segments,
            dup_bytes=self._dup_bytes,
            reordered=self._reordered,
            hole_resets=self._hole_resets,
            pending_bytes=self._pending_bytes,
        )

    def reset(self) -> None:
        self._next = None
        self._pending.clear()
        self._pending_bytes = 0
        self._gap_since = None
        self.framer.reset()

    def feed_segment(self, seq: int, data: bytes, ts: float) -> list[BattleEvent]:
        if not data:
            return []
        self._segments += 1
        seq %= _SEQ_MOD

        if self._next is None:
            self._next = (seq + len(data)) % _SEQ_MOD
            return self.framer.feed(data, ts)

        d = _seq_delta(seq, self._next)
        if d > 0:
            return self._park(seq, data, ts)
        if d < 0:
            overlap = -d
            if overlap >= len(data):
                self._dup_segments += 1
                self._dup_bytes += len(data)
                return []
            self._dup_bytes += overlap
            data = data[overlap:]

        events = self.framer.feed(data, ts)
        self._next = (self._next + len(data)) % _SEQ_MOD
        events.extend(self._drain(ts))
        return events

    def _park(self, seq: int, data: bytes, ts: float) -> list[BattleEvent]:
        """홀 — 예산 안에서 보류. 초과하면 포기하고 재락한다."""
        prev = self._pending.get(seq)
        if prev is not None:
            # ⚠️ seq 만 보고 버리면 안 된다. TCP repacketization 은 **같은 seq · 더 긴**
            # 세그먼트를 흔히 만드는데(인접 세그먼트 병합), 짧은 쪽을 남기면 홀이
            # 메워질 때 뒤쪽 바이트가 조용히 사라지고 `_next` 가 어긋나 프레이머가
            # 프레임 중간에서 재동기한다 — 그 창이 ENTER 를 삼킬 수 있다.
            self._dup_segments += 1
            if len(data) <= len(prev[0]):
                self._dup_bytes += len(data)
                return []
            self._dup_bytes += len(prev[0])
            self._pending_bytes += len(data) - len(prev[0])
            self._pending[seq] = (data, ts)
        else:
            # 재정렬 계상은 dup 판정 **뒤**에 — 같은 세그먼트의 재전송을 재정렬로
            # 이중 계상하면 상위가 흐름 품질을 오판한다.
            self._reordered += 1
            self._pending[seq] = (data, ts)
            self._pending_bytes += len(data)
        if self._gap_since is None:
            self._gap_since = ts

        over_bytes = self._pending_bytes > self._budget_bytes
        over_time = (ts - self._gap_since) > self._budget_sec
        if not (over_bytes or over_time):
            return []

        # 포기: 손실된 스트림에서 프레이밍을 이어가면 조용히 틀린다.
        self._hole_resets += 1
        self._pending.clear()
        self._pending_bytes = 0
        self._gap_since = None
        self.framer.reset()
        self._next = (seq + len(data)) % _SEQ_MOD
        return self.framer.feed(data, ts)

    def _drain(self, ready_ts: float) -> list[BattleEvent]:
        """`_next` 에 이어지는 보류 세그먼트를 순서대로 흘려보낸다."""
        events: list[BattleEvent] = []
        while self._pending:
            hit = None
            for s in self._pending:
                if _seq_delta(s, self._next) <= 0:
                    hit = s
                    break
            if hit is None:
                break
            data, ts = self._pending.pop(hit)
            self._pending_bytes -= len(data)
            d = _seq_delta(hit, self._next)
            if d < 0:
                overlap = -d
                if overlap >= len(data):
                    self._dup_segments += 1
                    self._dup_bytes += len(data)
                    continue
                self._dup_bytes += overlap
                data = data[overlap:]
            events.extend(self.framer.feed(data, ts, wordinput_ts=ready_ts))
            self._next = (self._next + len(data)) % _SEQ_MOD
        if not self._pending:
            self._gap_since = None
        return events
