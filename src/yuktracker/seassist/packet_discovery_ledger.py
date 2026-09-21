"""패킷 발굴 수집 — 바운드 창 동안 원시 s2c 세그먼트 + 라벨을 JSONL 로 남긴다 (Phase 0).

배경: 전투 IN/OUT 외의 기능(포만감·캡차·조철)을 패킷으로 옮기려면 먼저 **어떤
opcode 가 그 이벤트를 나르는지** 알아야 하는데, 현재 발굴된 opcode 는 전투 2종뿐이고
다른 이벤트 전용 캡처 데이터도 없다. 이 모듈이 그 데이터를 만든다.

왜 "상시 수집"이 아니라 **바운드 창**인가
------------------------------------------
조철은 등장 시점을 예측할 수 있다("자연 조철은 전투 240~300 사이" 가설 —
``jochul_counter_panel`` 참조). 그래서 상시로 흘리는 대신 사용자가 예상 구간에
수집을 켜고 2~10분 받은 뒤 검수하는 편이 표본 밀도·디스크 양쪽에서 낫다. 창이
짧으므로 **본문을 포함한 원시 바이트를 통째로** 남길 수 있고, 그 덕에 포만감
오프셋 탐색까지 오프라인에서 끝난다(프로덕션 프레이머 무수정).

왜 프레임이 아니라 **원시 세그먼트**인가
----------------------------------------
``FlowDecoder`` 에 들어가는 입력을 그대로 남긴다. 오프라인에서 같은 디코더로
재생하면 프로덕션과 비트 단위로 동일한 프레이밍이 나오므로, 발굴 결과가 그대로
이식된다. 프레임으로 남기면 본문이 이미 버려진 뒤라(``gersang_protocol`` 의
조기 발화) 수치 해독이 불가능하다.

스레드 계약 (중요)
------------------
``note_segment`` 는 **스니퍼 스레드 핫패스**에서 불린다. 이 스레드는 pcap 폴
루프를 돌고 있어 디스크 I/O 로 막으면 패킷을 놓친다 — 게다가 기록 위치가
OneDrive 라 동기 중 수백 ms 씩 멎을 수 있다. 그래서 **바운드 큐 + 전용 writer
스레드**로 가른다: 스니퍼는 enqueue 만 하고, 큐가 차면 **버리고 카운트**한다
(절대 블록하지 않는다). 유실은 수집 품질 저하일 뿐이지만 블록은 전투 감지
자체를 망가뜨린다 — 비대칭이라 선택은 자명하다.

⚠️ 엔진은 콜백을 가드하지 않는다(``packet_shadow_ledger`` 와 동일). 여기서
예외가 새면 스니퍼 스레드가 죽어 세션 패킷 커버리지를 통째로 잃는다. 전 공개
메서드가 어떤 예외도 삼키는 이유다.

기록 위치·관례는 ``packet_shadow_ledger`` / ``jochul_counter`` 미러:
<검토 베이스>/<디바이스>/packet_discovery/window_YYYYMMDD_HHMMSS_ffffff_NNNNNN.jsonl,
베이스 해석 실패 시 %APPDATA%/SEAssist/packet_discovery 폴백.
``OCR_STATS_DISABLE_JSONL`` 킬스위치 공유, ``enable_persistence()`` opt-in
(테스트가 실 원장을 오염시키지 않게 — 프로덕션 엔트리 app.run() 에서만 호출).

행 스키마 (창당 파일 1개, 시간순)
---------------------------------
정상 마감 파일은 ``window_start`` 가 첫 행, ``window_end`` 가 마지막 행이다.
producer 승인과 두 경계 행을 같은 창 상태로 묶어 다른 창의 행이 섞이지 않는다.

- kind="window_start": slots={idx: label}, mono_base, wall_base, duration_sec,
  max_bytes — **mono_base/wall_base 가 축 브리지다.** 세그먼트·자극은 monotonic
  으로 남지만 조철 수동 버튼 등 기존 원장은 벽시계뿐이라, 오프라인 도구가 이
  두 값의 차로 축을 맞춘다(monotonic 은 부팅 간 비교 불가).
- kind="seg":   slot, port, seq, mono, b64(payload) [, sport] [, dir]
  ⚠️ 이 행만 ts/iso/device 공통 헤더를 **의도적으로 생략**한다. 창당 수만 행이라
  행마다 ``datetime.fromtimestamp`` + 60여 바이트를 붙이면 용량·CPU 가 다 낭비다.
  벽시계가 필요하면 mono_base/wall_base 로 환산하면 된다.
  ``sport``(서버 포트)는 **8000 이 아닐 때만** 붙는다(패킷 PR-D, 부차 4011 원시
  세그먼트) — 같은 행 크기 절제. 로더는 부재를 8000 으로 읽는다.
  ``dir`` 은 **"s2c" 가 아닐 때만** 붙는다(패킷 PR-G1, 4011 c2s opt-in 원시 수집) —
  로더는 부재를 s2c 로 읽고, c2s 행은 seq 공간이 달라 재생에서 항상 제외한다.
- kind="label": event("feed"|"captcha"|"battle_in"|"battle_out"|...), slot,
  label, mono, detail(dict)
- kind="window_end": reason, segs, bytes, dropped, aux_segs, aux_bytes,
  aux_dropped, health — segs/bytes/dropped 는 8000(전투 스트림) 계열, aux_* 는
  부차 4011 계열. 부차는 **별도 예산**(`aux_max_bytes`)과 큐 후순위로 다룬다:
  창이 실제로 필요로 하는 8000 표본이 4011 폭주(내용·양 미지)에 밀려 유실되거나
  창이 조기 종료("용량 상한")되면 안 된다. 부차 예산 초과는 부차 행만 버린다.
"""
from __future__ import annotations

import base64
import json
import os
import platform
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config
from . import ledger_paths

__all__ = [
    "DEFAULT_DURATION_SEC",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_AUX_MAX_BYTES",
    "PacketDiscoveryRecorder",
    "DISCOVERY_RECORDER",
    "onedrive_available",
]

_LEDGER_SUBDIR = "packet_discovery"

#: 기본 수집 시간 — 사용자 운용(2~10분)의 상한. 켜놓고 잊어도 자동 종료된다.
DEFAULT_DURATION_SEC = 600.0
#: 기본 용량 상한. 실측 트래픽은 대부분 9~16B idle 틱이라 10분에 수 MB 수준이므로
#: 넉넉하다 — 이 값은 폭주(손상 스트림/예상 못 한 대량 전송) 방어용이다.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
#: 부차(4011) 원시 세그먼트의 창당 별도 예산. 초과분은 부차 행만 버리고
#: `aux_dropped` 로 센다 — 8000 창의 자동 종료 판정(`max_bytes`)에는 섞지 않는다.
DEFAULT_AUX_MAX_BYTES = 16 * 1024 * 1024
#: 큐 점유가 이 비율을 넘으면 부차 행은 넣지 않는다(8000 행 우선). qsize 는
#: 근사치지만 우선순위 힌트로는 충분하다.
_AUX_QUEUE_YIELD_FRAC = 0.5

#: 큐 상한. 초과분은 버리고 ``dropped`` 로 계량한다 — 스니퍼를 막느니 표본을 잃는다.
_QUEUE_MAX = 20000
#: writer 종료 대기 — 소유자(Tk) 스레드가 end_window 에서 블록하는 상한이다.
#: 정상 창은 ms 안에 끝나고, 넘기면 기다림을 포기한다(daemon writer 가 프로세스
#: 생존 동안 남은 큐를 마저 쓴다 — 창별 stop 이벤트가 종료를 보장하므로 창을
#: 즉시 닫아도 고아가 되지 않는다). GUI 1s 틱/정지 경로에서 불리므로 짧게.
_WRITER_JOIN_SEC = 1.0
#: 파일 open 완료 확인 대기. 시작 버튼이 성공을 보고하기 전에 writer 가 실제로
#: 목적 파일을 열 수 있는지 확인하되, OneDrive 정지로 GUI 를 오래 붙잡지 않는다.
_WRITER_READY_SEC = 1.0
#: writer 루프의 큐 대기 — 종료 지연 상한이자 유휴 CPU 비용의 균형점.
_WRITER_POLL_SEC = 0.5

_SENTINEL = object()


class _WriterState:
    """창별 writer 진단값. 늦게 끝난 구 writer 가 새 창 카운터를 오염시키지 않는다."""

    def __init__(self) -> None:
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.written_rows = 0
        self.flushed_rows = 0
        self.errors = 0
        self.error = ""
        self.terminal_row: dict | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "written_rows": self.written_rows,
                "flushed_rows": self.flushed_rows,
                "writer_errors": self.errors,
                "writer_error": self.error,
            }


def _ledger_base() -> Path | None:
    """원장 저장 베이스 — ``jochul_counter._ledger_base`` 미러 (단일 결정 지점).

    패킷 전용 뿌리(``settings.packet_data_dir``)의 디바이스별 폴더다 — 비어 있으면
    종전처럼 wordinput_review 검토 폴더로 폴백한다(``packet_dir`` 이 결정). ⚠️ 이것이
    **자동 합류를 보장하지는 않는다** — 운영 4대 중 1대는 OneDrive 미연동이라
    그 PC 의 창은 로컬에만 남는다(``onedrive_available`` 로 구분해 사용자에게
    알린다). 경로는 ``ledger_paths``(stdlib) 가 결정한다 — 종전엔 ``wordinput_runner`` 를
    지연 import 했는데, 그러면 cv2·genai 가 딸려 와 GUI 없는 육의전 관측기가 같은 폴더를
    쓸 수 없었다(2026-09-21).
    """
    try:
        return ledger_paths.packet_dir()
    except Exception:
        return None  # 호출자가 app_dir()/packet_discovery 로 폴백


def onedrive_available() -> bool:
    """이 PC 에 OneDrive 가 붙어 있는가 — 창 파일이 자동 합류하는지의 판단 근거.

    env var 부재는 "확실히 미연동"이다. 반대로 있다고 해서 창이 반드시 그 아래
    있는 것은 아니므로(사용자가 ``wordinput_review_dir`` 로 딴 곳을 지정 가능)
    **경고는 부재일 때만** 낸다 — 의도적 커스텀 경로에 잔소리하지 않도록.
    """
    try:
        return ledger_paths._onedrive_root() is not None
    except Exception:
        return False


class PacketDiscoveryRecorder:
    """수집 창 1개를 소유하는 싱글턴 (호출 스레드: 스니퍼 1 + 러너 N + Tk).

    상태 전이는 ``begin_window`` / ``end_window`` 두 지점뿐이고 둘 다 소유자
    (GUI) 스레드에서 불린다. 핫패스(``note_segment``)는 ``_active`` 단일 속성
    읽기로 게이트되므로 수집 OFF 비용은 사실상 0이다.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._persist_enabled = False
        # 두 플래그를 가르는 이유: 한도 초과(_auto_stop)는 **스니퍼 스레드**에서
        # 즉시 수집을 멈춰야 하지만, 마감(writer join)은 거기서 하면 안 된다 —
        # 스니퍼가 최대 _WRITER_JOIN_SEC 멎어 패킷을 놓친다. 그래서 핫패스 게이트
        # (_active)만 내리고, 창 수명(_window_open)은 소유자가 end_window 로 닫는다.
        # 하나로 합치면 자동 종료 뒤 end_window 가 no-op 이 되어 writer 가 마감되지
        # 않고 window_end 행이 유실된다.
        self._active = False
        self._window_open = False
        # 아래는 창 수명 동안만 유효 — begin_window 가 세팅, end_window 가 청소.
        self._q: queue.Queue | None = None
        self._writer: threading.Thread | None = None
        self._writer_state: _WriterState | None = None
        # 창별 stop 이벤트 — writer 의 종료 판정은 **이 이벤트**뿐이다. 공유 플래그
        # (_window_open)를 읽게 하면 sentinel 이 유실된 채 다음 창이 열렸을 때 구
        # writer 가 새 창의 True 를 보고 고아로 살아남는다.
        self._stop_evt: threading.Event | None = None
        self._path: Path | None = None
        self._deadline = 0.0
        self._max_bytes = 0
        self._aux_max_bytes = 0
        self._started_mono = 0.0
        self._labels: dict[int, str] = {}
        self._path_serial = 0
        # 계량 — segs/bytes 는 스니퍼(note_segment 단일 스레드)가, dropped 는
        # 스니퍼 + 라벨 스레드(_put)가 만진다. dropped 만 다중 쓰기라 근사치.
        self._segs = 0
        self._bytes = 0
        self._dropped = 0
        # 부차(4011) 계열 — 스니퍼 단일 스레드만 만진다.
        self._aux_segs = 0
        self._aux_bytes = 0
        self._aux_dropped = 0
        self._auto_stop_reason = ""

    # ---------- lifecycle (소유자 = GUI 스레드) ----------

    @property
    def active(self) -> bool:
        """핫패스 게이트 — 속성 읽기 1회 (수집 OFF 비용의 전부)."""
        return self._active

    @property
    def window_open(self) -> bool:
        """창 수명 — 자동 종료 후 아직 마감되지 않은 구간도 True."""
        return self._window_open

    def enable_persistence(self) -> None:
        """파일 기록 활성 — 프로덕션 엔트리(app.run())에서만 호출.

        기본 OFF: 러너/배선 경로를 태우는 테스트가 실 검토 폴더(OneDrive)에
        덤프를 쏟지 않도록 opt-in (JOCHUL_COUNTER/SHADOW_LEDGER 선례).
        """
        with self._lock:
            self._persist_enabled = True

    def begin_window(
        self,
        labels: dict[int, str],
        *,
        duration_sec: float = DEFAULT_DURATION_SEC,
        max_bytes: int = DEFAULT_MAX_BYTES,
        aux_max_bytes: int = DEFAULT_AUX_MAX_BYTES,
        now_mono: float | None = None,
    ) -> tuple[bool, str]:
        """수집 창 시작 → (성공, 사용자 메시지). 이미 켜져 있으면 무해한 실패.

        ``max_bytes`` 는 8000 세그먼트 예산(초과 → 창 자동 종료), ``aux_max_bytes``
        는 부차 4011 예산(초과 → 부차 행만 유실, 창은 계속)."""
        try:
            with self._lock:
                if self._window_open:
                    return False, "이미 수집 중입니다"
                if not self._persist_enabled:
                    return False, "수집 비활성(영속 미허용) — 프로덕션에서만 동작합니다"
                mono = time.monotonic() if now_mono is None else now_mono
                path = self._resolve_path()
                if path is None:
                    return False, "수집 폴더를 열 수 없습니다"
                self._path = path
                self._q = queue.Queue(maxsize=_QUEUE_MAX)
                self._deadline = mono + max(1.0, float(duration_sec))
                self._max_bytes = max(1, int(max_bytes))
                self._aux_max_bytes = max(0, int(aux_max_bytes))
                self._started_mono = mono
                self._labels = dict(labels)
                self._segs = self._bytes = self._dropped = 0
                self._aux_segs = self._aux_bytes = self._aux_dropped = 0
                self._auto_stop_reason = ""
                writer_state = _WriterState()
                self._writer_state = writer_state
                self._stop_evt = writer_state.stop
                self._writer = threading.Thread(
                    target=self._writer_loop,
                    args=(self._q, path, writer_state),
                    daemon=True, name="PacketDiscoveryWriter",
                )
                self._writer.start()
                self._window_open = True
                self._active = False
            if not writer_state.ready.wait(_WRITER_READY_SEC):
                self._record_writer_error(
                    writer_state, "open timeout", TimeoutError("writer 준비 시간 초과"))
            writer_health = writer_state.snapshot()
            if writer_health["writer_errors"]:
                self._teardown(writer_state)
                return False, f"패킷 수집 파일 열기 실패 — {writer_health['writer_error']}"
            with self._lock:
                if self._writer_state is not writer_state or not self._window_open:
                    return False, "패킷 수집 시작 취소"
                start_queued = self._put({
                    "kind": "window_start",
                    "slots": {str(k): v for k, v in labels.items()},
                    "mono_base": mono,
                    "wall_base": time.time(),
                    "duration_sec": float(duration_sec),
                    "max_bytes": int(max_bytes),
                    "aux_max_bytes": int(aux_max_bytes),
                }, header=True)
                if start_queued:
                    self._active = True
            if not start_queued:
                self._teardown(writer_state)
                return False, "패킷 수집 시작 행을 기록할 수 없습니다"
            # 저장 위치를 **시작 시점에** 알린다 — 미연동 PC 는 창이 로컬에만
            # 남아, 끝난 뒤에 찾으려면 어디를 볼지 모른다.
            note = "" if onedrive_available() else \
                "  ⚠ OneDrive 미연동 — 이 PC 에만 남습니다(직접 복사 필요)"
            return True, (f"패킷 수집 시작 — 최대 {int(duration_sec // 60)}분 "
                          f"→ {path.parent}{note}")
        except Exception:
            # 창을 열다 실패했으면 반쯤 열린 상태를 남기지 않는다.
            try:
                self._teardown()
            except Exception:
                pass
            return False, "패킷 수집 시작 실패"

    def end_window(self, reason: str = "manual",
                   health: dict | None = None) -> str:
        """수집 창 종료 → 사용자 메시지. 창이 없으면 no-op (공용 마감 경로 안전).

        자동 종료(_auto_stop)로 이미 ``_active`` 가 내려간 창도 여기서 마감된다 —
        그쪽은 수집만 멈추고 writer 는 살려두기 때문이다.
        """
        try:
            with self._lock:
                if not self._window_open:
                    return ""
                self._active = False
                q, writer, path = self._q, self._writer, self._path
                stop_evt = self._stop_evt
                writer_state = self._writer_state
                segs, nbytes, dropped = self._segs, self._bytes, self._dropped
                aux = (self._aux_segs, self._aux_bytes, self._aux_dropped)
                # 종료 행은 큐 용량을 쓰지 않는다. 이 락이 producer admission 과
                # 같은 경계라 여기까지 승인된 행/유실만 terminal 집계에 들어간다.
                if writer_state is not None:
                    now = time.time()
                    writer_state.terminal_row = {
                        "ts": now,
                        "iso": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
                        "device": platform.node() or "?",
                        "kind": "window_end", "reason": reason, "segs": segs,
                        "bytes": nbytes, "dropped": dropped,
                        "aux_segs": aux[0], "aux_bytes": aux[1],
                        "aux_dropped": aux[2], "health": dict(health or {}),
                    }
            # 종료 신호는 이벤트 — sentinel 은 "큐가 비면 즉시" 깨우는 보조일 뿐
            # 이라 기다리지 않는다(Full 이면 writer 가 큐를 비운 뒤 이벤트로 나간다).
            # ⚠️ 이벤트는 terminal 행을 **확정한 뒤** — writer 는 큐를 다 비운 뒤
            # terminal 을 직접 써서 큐 포화와 무관하게 행 순서·완결을 유지한다.
            if stop_evt is not None:
                stop_evt.set()
            if q is not None:
                try:
                    q.put_nowait(_SENTINEL)
                except queue.Full:
                    pass
            if writer is not None:
                writer.join(_WRITER_JOIN_SEC)
                if writer.is_alive() and writer_state is not None:
                    self._record_writer_error(
                        writer_state, "shutdown timeout",
                        TimeoutError("writer 종료 시간 초과"))
            with self._lock:
                self._window_open = False
                self._q = None
                self._writer = None
                self._stop_evt = None
                self._path = None
            # **전체 경로**를 낸다 — 파일명만으로는 미연동 PC 에서 찾을 수 없다.
            where = str(path) if path is not None else "?"
            mb = nbytes / (1024 * 1024)  # 락 안 스냅샷 — 메시지 값도 같은 스냅샷
            extra = f", 유실 {dropped}" if dropped else ""
            if aux[0] or aux[2]:
                extra += (f", 부차 {aux[0]}세그 / {aux[1] / (1024 * 1024):.1f}MB"
                          + (f"(유실 {aux[2]})" if aux[2] else ""))
            wh = writer_state.snapshot() if writer_state is not None else {}
            if wh.get("writer_errors"):
                extra += f", writer 오류 {wh['writer_errors']} ({wh['writer_error']})"
            extra += f", 기록 {wh.get('written_rows', 0)}행"
            return (f"패킷 수집 종료({reason}) — {segs}세그 / {mb:.1f}MB{extra}"
                    f" → {where}")
        except Exception:
            return "패킷 수집 종료(오류)"

    def status(self) -> dict:
        """GUI 1초 틱용 스냅샷. 락 없이 읽는다 — 표시용이라 찢겨도 무해."""
        writer = self._writer_state
        writer_health = writer.snapshot() if writer is not None else {
            "written_rows": 0, "flushed_rows": 0,
            "writer_errors": 0, "writer_error": "",
        }
        if not self._window_open:
            return {
                "active": False, "recording": False,
                "auto_stop": self._auto_stop_reason,
                "segs": self._segs, "bytes": self._bytes,
                "dropped": self._dropped,
                "aux_segs": self._aux_segs, "aux_bytes": self._aux_bytes,
                "aux_dropped": self._aux_dropped,
                **writer_health,
            }
        return {
            "active": True,
            "recording": self._active,
            "elapsed": max(0.0, time.monotonic() - self._started_mono),
            "remaining": max(0.0, self._deadline - time.monotonic()),
            "segs": self._segs,
            "bytes": self._bytes,
            "dropped": self._dropped,
            "aux_segs": self._aux_segs,
            "aux_bytes": self._aux_bytes,
            "aux_dropped": self._aux_dropped,
            **writer_health,
        }

    def take_auto_stop_reason(self) -> str:
        """자동 종료가 걸렸으면 사유를 1회 꺼낸다 (GUI 가 사용자에게 알리도록).

        시간 만료는 **여기서도** 검사한다 — ``note_segment`` 의 만료 검사는
        세그먼트 도착에 의존하므로, 패킷 소스 off·트래픽 중단 상태에서는 영영
        안 걸린다("켜놓고 잊어도 자동 종료" 보장이 깨진다). GUI 1s 틱이 이
        메서드를 매초 부르므로 시계 기반 검사를 얹으면 무트래픽 창도 닫힌다.
        ⚠️ 검사 축은 ``time.monotonic()`` — 프로덕션 begin_window 가 쓰는 축과
        같다(테스트가 합성 now_mono 를 주는 경우는 이 메서드를 부르기 전에
        직접 만료를 유발하므로 무관).
        """
        if self._active and time.monotonic() >= self._deadline:
            self._auto_stop("시간 만료")
        r = self._auto_stop_reason
        if r:
            self._auto_stop_reason = ""
        return r

    # ---------- 기록 (스니퍼 / 러너 / Tk 스레드) ----------

    def note_segment(self, slot_idx: int, local_port: int, seq: int,
                     payload: bytes, mono_ts: float, *,
                     server_port: int = 8000, direction: str = "s2c") -> None:
        """원시 세그먼트 — **스니퍼 스레드 핫패스**. 절대 던지지 않는다.

        ``feed_segment`` 직전에 불린다: 디코더가 dedup/재조립하기 **전**의 도착
        순서 그대로여야 오프라인 재생이 프로덕션과 같은 경로를 밟는다.
        ``server_port`` 가 8000 이 아니면(부차 4011) 행에 ``sport`` 를 붙이고,
        **별도 예산·큐 후순위**로 다룬다 — 4011 폭주가 8000 표본을 밀어내거나
        창을 조기 종료시키면 안 된다(창의 목적은 8000 라벨 표본이다).
        ``direction`` 이 "s2c" 가 아니면(4011 c2s opt-in, 패킷 PR-G1) 행에 ``dir``
        을 붙인다 — 예산·큐 취급은 부차와 같다(c2s 는 부차 포트에서만 온다).
        """
        writer_state = self._writer_state
        if not self._active or writer_state is None:
            return
        try:
            aux = server_port != 8000
            n = len(payload)
            row = {
                "kind": "seg", "slot": slot_idx, "port": local_port,
                "seq": seq, "mono": mono_ts,
                "b64": base64.b64encode(payload).decode("ascii"),
            }
            if aux:
                row["sport"] = int(server_port)
            if direction != "s2c":
                row["dir"] = str(direction)
            with self._lock:
                if not self._active or self._writer_state is not writer_state:
                    return
                if mono_ts >= self._deadline:
                    self._auto_stop("시간 만료")
                    return
                if not aux and self._bytes >= self._max_bytes:
                    self._auto_stop("용량 상한")
                    return
                q = self._q
                if q is None:
                    return
                if aux and (self._aux_bytes >= self._aux_max_bytes
                            or q.qsize() >= _QUEUE_MAX * _AUX_QUEUE_YIELD_FRAC):
                    self._aux_dropped += 1
                    return
                try:
                    q.put_nowait(row)
                except queue.Full:
                    if aux:
                        self._aux_dropped += 1
                    else:
                        self._dropped += 1
                    return
                if aux:
                    self._aux_segs += 1
                    self._aux_bytes += n
                else:
                    self._segs += 1
                    self._bytes += n
        except Exception:
            pass

    def note_label(self, event: str, *, slot_idx: int = -1, label: str = "",
                   mono_ts: float | None = None, **detail) -> None:
        """자극/사용자 라벨 — 어느 스레드에서든. 절대 던지지 않는다.

        ``event`` 예: "feed"(급식 발사 — 우리가 시각을 아는 최강 자극),
        "captcha"(팝업 확정), "jochul_manual"/"word_manual"(사용자 버튼),
        "battle_in"/"battle_out"(패킷 전투 에지 — 발굴 표에서 기준선).
        """
        writer_state = self._writer_state
        if not self._active or writer_state is None:
            return
        try:
            with self._lock:
                if not self._active or self._writer_state is not writer_state:
                    return
                self._put({
                    "kind": "label", "event": event, "slot": slot_idx,
                    "label": label or self._labels.get(slot_idx, ""),
                    "mono": time.monotonic() if mono_ts is None else mono_ts,
                    "detail": detail,
                }, header=True)
        except Exception:
            pass

    # ---------- 내부 ----------

    def _auto_stop(self, reason: str) -> None:
        """한도 초과 — 스니퍼 스레드에서 수집만 즉시 멈춘다(GUI 틱을 안 기다린다).

        ``end_window`` 는 writer join 을 하므로 스니퍼가 최대 _WRITER_JOIN_SEC
        멈출 수 있다. 그래서 여기서는 플래그만 내리고 실제 마감은 GUI 틱이
        ``take_auto_stop_reason`` 으로 인지해 수행한다.
        """
        if not self._active:
            return
        self._active = False
        self._auto_stop_reason = reason

    def _resolve_path(self) -> Path | None:
        """창당 파일 1개. 베이스 해석은 **여기 1회뿐** — ``_debug_dir()`` 이 매
        호출 ``load_settings()``+mkdir 을 하므로 행마다 부르면 안 된다."""
        try:
            base = _ledger_base()
            root = (Path(base) if base is not None else config.app_dir())
            d = root / _LEDGER_SUBDIR
            d.mkdir(parents=True, exist_ok=True)
            stamp = datetime.fromtimestamp(time.time()).strftime("%Y%m%d_%H%M%S_%f")
            serial = self._path_serial
            while True:
                path = d / f"window_{stamp}_{serial:06d}.jsonl"
                self._path_serial = serial + 1
                if not path.exists():
                    return path
                serial += 1
        except Exception:
            return None

    def _put(self, row: dict, *, header: bool = False) -> bool:
        """저빈도 행 enqueue. ``header=True`` 면 ts/iso/device 공통 헤더를 붙인다
        (seg 행은 창당 수만 건이라 의도적으로 생략 — 모듈 docstring 참조)."""
        qq = self._q
        if qq is None:
            return False
        try:
            if header:
                now = time.time()
                base = {
                    "ts": now,
                    "iso": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
                    "device": platform.node() or "?",
                }
                base.update(row)
                row = base
            qq.put_nowait(row)
            return True
        except queue.Full:
            self._dropped += 1
        except Exception:
            pass
        return False

    def _writer_loop(self, q: queue.Queue, path: Path,
                     writer_state: _WriterState) -> None:
        """큐 → 파일. 이 스레드만 파일을 만진다(Windows append 비원자 회피).

        종료 판정은 인자로 받은 **창별** ``stop_evt`` 뿐 — 인스턴스 공유 상태를
        읽지 않는다(다음 창이 열려도 이 writer 는 자기 창의 이벤트로 나간다).
        파일 오류는 창별 상태에 기록하고 수집을 멈춘다. 예외는 writer 밖으로
        올리지 않아 스니퍼를 막지 않되 GUI/status 가 실패를 숨기지 않게 한다.
        """
        opened = False
        try:
            # 이름 선택 이후 다른 recorder가 같은 파일을 만들었어도 합치지 않는다.
            with path.open("x", encoding="utf-8") as f:
                opened = True
                writer_state.ready.set()
                # begin readiness timeout/start-marker 실패 뒤 늦게 열린 경우. 이
                # writer 가 x 모드로 만든 빈 파일이며 쓸 행도 없으므로 닫은 뒤 제거.
                if (writer_state.stop.is_set()
                        and writer_state.terminal_row is None and q.empty()):
                    return
                pending = 0
                while True:
                    try:
                        item = q.get(timeout=_WRITER_POLL_SEC)
                    except queue.Empty:
                        if writer_state.stop.is_set():
                            break  # 창이 마감됐고 큐도 비었다
                        if pending:
                            try:
                                f.flush()
                            except Exception as e:
                                self._record_writer_error(writer_state, "flush", e)
                                pending = 0
                                break
                            self._mark_flushed(writer_state, pending)
                            pending = 0
                        continue
                    if item is _SENTINEL:
                        break
                    try:
                        f.write(json.dumps(item, ensure_ascii=False,
                                           default=str) + "\n")
                        with writer_state.lock:
                            writer_state.written_rows += 1
                        pending += 1
                        # 창이 길어 크래시 시 전량 유실되는 것을 막되, 행마다
                        # flush 하면 OneDrive 폴더에서 비싸다 — 절충.
                        if pending >= 256:
                            try:
                                f.flush()
                            except Exception as e:
                                self._record_writer_error(writer_state, "flush", e)
                                pending = 0
                                break
                            self._mark_flushed(writer_state, pending)
                            pending = 0
                    except Exception as e:
                        self._record_writer_error(writer_state, "write", e)
                        break
                terminal = writer_state.terminal_row
                terminal_written = False
                if terminal is not None:
                    terminal = dict(terminal)
                    terminal.update(writer_state.snapshot())
                    try:
                        f.write(json.dumps(terminal, ensure_ascii=False,
                                           default=str) + "\n")
                        with writer_state.lock:
                            writer_state.written_rows += 1
                        pending += 1
                        terminal_written = True
                    except Exception as e:
                        self._record_writer_error(writer_state, "terminal write", e)
                try:
                    if pending:
                        f.flush()
                        self._mark_flushed(writer_state, pending)
                except Exception as e:
                    phase = "terminal flush" if terminal_written else "flush"
                    self._record_writer_error(writer_state, phase, e)
        except Exception as e:
            self._record_writer_error(writer_state, "close" if opened else "open", e)
        finally:
            writer_state.ready.set()
            cancelled = (
                opened
                and writer_state.stop.is_set()
                and writer_state.terminal_row is None
            )
            if cancelled:
                try:
                    if writer_state.snapshot()["written_rows"]:
                        self._record_writer_error(
                            writer_state, "cancel cleanup",
                            RuntimeError("취소된 수집 파일에 이미 기록된 행이 있음"),
                        )
                    elif path.stat().st_size == 0:
                        path.unlink()
                    else:
                        self._record_writer_error(
                            writer_state, "cancel cleanup",
                            RuntimeError("취소된 수집 파일이 비어 있지 않음"),
                        )
                except FileNotFoundError:
                    pass
                except Exception as e:
                    self._record_writer_error(writer_state, "cancel cleanup", e)

    @staticmethod
    def _mark_flushed(writer_state: _WriterState, count: int) -> None:
        with writer_state.lock:
            writer_state.flushed_rows += count

    def _record_writer_error(self, writer_state: _WriterState,
                             phase: str, error: Exception) -> None:
        message = f"{phase}: {error}"
        with writer_state.lock:
            writer_state.errors += 1
            writer_state.error = message
        with self._lock:
            if self._writer_state is writer_state and self._active:
                self._active = False
                self._auto_stop_reason = "writer 오류"

    def _teardown(self, writer_state: _WriterState | None = None) -> None:
        """begin_window 실패 경로 전용 — 반쯤 열린 창을 흔적 없이 되돌린다.

        writer 가 이미 떴을 수 있으므로 stop 이벤트를 올려 다음 폴에서 탈출시킨다.
        OS 파일 open 자체가 멎으면 즉시 취소할 수 없다. 그 open 이 나중에 성공하면
        writer 가 자신이 x 모드로 만든 빈 파일만 제거한다. 제거 실패는 status 에
        남고 해당 빈 불완전 파일도 보존된다.
        """
        with self._lock:
            if writer_state is not None and self._writer_state is not writer_state:
                return
            self._active = False
            self._window_open = False
            if self._stop_evt is not None:
                self._stop_evt.set()
            writer = self._writer
            self._q = None
            self._writer = None
            self._stop_evt = None
            self._path = None
        if writer is not None:
            writer.join(_WRITER_JOIN_SEC)


#: 모듈 전역 싱글턴 — 스니퍼/러너/App 이 공유 (SHADOW_LEDGER 선례).
DISCOVERY_RECORDER = PacketDiscoveryRecorder()
