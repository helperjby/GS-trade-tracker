"""관측기 본체 — SEAssist 스니퍼 엔진 배선 · 발굴 수집 창 · 콘솔 라벨 (PR-Y1 = 수집 모드).

왜 별도 프로그램인가
--------------------
SEAssist 의 패킷 엔진은 War/Mon 매크로 세션이 도는 동안에만 뜬다. 육의전 목록은 유저가
육의전을 **직접 열 때만** 내려오는데 그 순간 매크로가 돌고 있으리란 보장이 없다. 그래서
엔진(`packet_state_source`)·프레이머(`gersang_protocol`)·수집기(`packet_discovery_ledger`)를
벤더 사본으로 **그대로 재사용**하되, 슬롯·러너·Tk 없이 관리자 콘솔 하나로 도는 관측기를 둔다.

이 단계(PR-Y1)의 범위
---------------------
- 게임 프로세스(`gersang.exe`)를 전부 자동으로 잡아 pseudo-슬롯(클라1·2·…)으로 엔진에 넘긴다.
- ``capture_min`` 이 있으면 그 길이의 발굴 창(`window_*.jsonl`)을 SEAssist GUI `[패킷 수집]` 과
  **같은 형식·같은 폴더**에 남긴다 — SEAssist 의 `packet_explore.py`/`mine_packet_discovery.py`
  가 그대로 읽는다. 콘솔에 한 줄 치면 `market_manual` 라벨이 찍힌다(육의전 열기·검색·페이지
  넘김 시각을 창 안에 남기는 유일한 수단).
- 파서·업로드는 없다. 육의전 opcode 가 발굴(SEAssist PACKET-PROCESS H-2609-07)로 확정된 뒤
  PR-Y2(프로토콜)·PR-Y1b(관측 모드) 가 붙는다.

스레드
------
엔진 콜백(status/event/segment)은 스니퍼 스레드에서 온다 — 여기서는 print 와 recorder enqueue
뿐이다(둘 다 비블로킹). 콘솔 입력은 별도 데몬 스레드가 읽는다. 메인 스레드는 1초 틱으로
자동 종료(시간 만료·용량 상한)만 살핀다. 콘솔 닫기(X)·로그오프·셧다운은 Windows 가 만든 핸들러
스레드로 오고, 거기서 창 마감(`window_end`)까지 **동기로** 끝낸다 — 핸들러가 돌아오면 프로세스가
죽어 `finally` 는 돌지 않는다(실기기 2026-09-21: X 로 닫은 창이 `window_end` 없이 끊겼다).
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, TextIO

from . import __version__
from .game_processes import game_pids
from .seassist import ledger_paths
from .seassist.logger import get_logger
from .seassist.packet_discovery_ledger import DISCOVERY_RECORDER, onedrive_available
from .seassist.packet_state_source import PacketStateSource

#: 수집 모드 기본 길이(분) — SEAssist GUI 의 2/5/10 과 같은 범위.
DEFAULT_CAPTURE_MIN = 5.0
#: 게임 흐름(8000) 대기 상한 — 이 안에 캡처 핸들이 안 열리면 수집 창을 열지 않고 안내만 한다.
FLOW_WAIT_SEC = 120.0
#: 콘솔 라벨의 event 이름 — 발굴 도구(`mine_packet_discovery.py`)의 자극 표에 이 이름으로 나온다.
LABEL_EVENT = "market_manual"
#: 종료로 읽는 줄(소문자 비교). ``ㅂ`` = 한글 2벌식에서 q 키 — 실기기(2026-09-21)에서 한글 IME 상태로 q 를
#: 쳐 `ㅂ` 라벨이 두 번 남았다. 한/영 상태와 무관하게 종료되게 한다.
QUIT_WORDS = ("q", "quit", "exit", "ㅂ")
#: Windows 콘솔 제어 이벤트(`SetConsoleCtrlHandler` HandlerRoutine 의 dwCtrlType).
CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6
_CLOSE_EVENTS = frozenset({CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT})
#: 콘솔 닫기·로그오프·셧다운으로 마감된 창의 `window_end.reason` — `packet_explore.py list` 종료 열에 보인다.
CONSOLE_CLOSE_REASON = "console_close"
#: 종료 뒤 콘솔을 붙잡아 두는 상한 — 승격 재기동으로 뜬 새 콘솔은 프로그램이 끝나면 닫혀
#: 저장 위치 안내를 못 본다.
FINAL_WAIT_SEC = 600.0
_TICK_SEC = 1.0
_FLOW_POLL_SEC = 0.5

#: 종료 코드.
RC_OK = 0
RC_ENGINE = 2      # Npcap/권한/디바이스 — 엔진 시작 실패
RC_NO_FLOW = 3     # 게임 흐름 대기 초과
RC_WINDOW = 4      # 수집 창 열기 실패(폴더/writer)

_console_log_attached = False


class PidIndexer:
    """게임 PID → pseudo-슬롯 인덱스. 처음 본 순서로 번호를 주고 **재사용하지 않는다**.

    엔진의 ``pids_provider`` 계약은 ``{slot_idx: pid}`` 다. 클라가 하나 닫혔다고 나머지 번호를
    당기면 창 안의 ``slot`` 의미가 중간에 바뀐다(라벨 `클라2` 가 다른 프로세스를 가리킴).
    같은 pid 가 다시 보이면 같은 번호다. 열거 실패는 빈 dict(엔진이 provider_errors 로 센다).
    """

    def __init__(self, enumerate_pids: Callable[[], Iterable[int]]) -> None:
        self._enumerate = enumerate_pids
        self._index_of: dict[int, int] = {}
        self._lock = threading.Lock()

    def provide(self) -> dict[int, int]:
        try:
            pids = sorted({int(p) for p in self._enumerate() if int(p) > 0})
        except Exception:
            return {}
        with self._lock:
            for pid in pids:
                if pid not in self._index_of:
                    self._index_of[pid] = len(self._index_of)
            return {self._index_of[pid]: pid for pid in pids}

    @property
    def known(self) -> dict[int, int]:
        """``{인덱스: pid}`` — 지금까지 본 전부(수집 창 라벨용)."""
        with self._lock:
            return {i: p for p, i in self._index_of.items()}


@dataclass(frozen=True)
class RunOptions:
    #: None 이면 관측 대기 모드(수집 창 없음 — 스니퍼 생존·흐름 락 확인용).
    capture_min: Optional[float] = None
    flow_wait_sec: float = FLOW_WAIT_SEC
    #: 끝난 뒤 Enter 를 기다린다(승격 새 콘솔이 닫히지 않게). 테스트·파이프에서는 False.
    pause_on_exit: bool = True
    #: SEAssist 로거(헬스 INFO 줄)를 콘솔로 흘린다. 테스트에서는 False.
    attach_log: bool = True


def _attach_console_log(out: TextIO) -> None:
    """SEAssist 로거의 INFO(5분 헬스 줄)·WARNING 을 콘솔에. GUI 와 달리 INFO 소비자가 있으니
    레벨을 INFO 로 내린다(logger.py 의 불변식 — INFO 는 소비 핸들러와 함께)."""
    global _console_log_attached
    if _console_log_attached:
        return
    log = get_logger()
    log.setLevel(logging.INFO)
    handler = logging.StreamHandler(out)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    log.addHandler(handler)
    _console_log_attached = True


def _console_ctrl_event(event: int, on_close: Callable[[str], None]) -> bool:
    """콘솔 제어 이벤트 1건 → 처리했으면 True (HandlerRoutine 의 순수 판정부, 테스트 대상).

    닫기·로그오프·셧다운만 받는다 — 창 마감을 **이 스레드에서 동기로** 끝낸 뒤 True. Windows 는
    CTRL_CLOSE 핸들러에 약 5초를 주고 `end_window` 의 writer join 상한은 1초라 충분하다.
    Ctrl+C·Ctrl+Break 는 False 로 넘겨 CRT/Python 의 기존 처리(KeyboardInterrupt → `finally` 마감)를
    그대로 둔다. ``on_close`` 의 예외는 삼킨다 — 핸들러에서 새면 프로세스가 그 자리에서 죽는다.
    """
    if int(event) not in _CLOSE_EVENTS:
        return False
    try:
        on_close(CONSOLE_CLOSE_REASON)
    except Exception:
        pass
    return True


def _install_console_close_hook(on_close: Callable[[str], None]) -> Callable[[], None]:
    """`SetConsoleCtrlHandler` 로 `_console_ctrl_event` 를 건다 → 해제 함수. 비Windows·실패는 no-op.

    관측기가 이 훅 때문에 죽어서는 안 되므로 어떤 예외도 밖으로 내지 않는다. ctypes 콜백 객체는 해제
    함수에 붙여 GC 를 막는다(참조가 사라진 콜백이 호출되면 크래시).
    """
    if sys.platform != "win32":
        return lambda: None
    try:
        import ctypes
        from ctypes import wintypes

        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        callback = handler_type(lambda event: _console_ctrl_event(event, on_close))
        kernel32 = ctypes.windll.kernel32
        if not kernel32.SetConsoleCtrlHandler(callback, True):
            return lambda: None

        def remove() -> None:
            try:
                kernel32.SetConsoleCtrlHandler(callback, False)
            except Exception:
                pass

        remove._callback = callback  # type: ignore[attr-defined]  # GC 방지
        return remove
    except Exception:
        return lambda: None


class _Console(threading.Thread):
    """stdin 한 줄 = 콜백 한 번. 블로킹 read 라 데몬 스레드 — 프로세스 종료를 막지 않는다."""

    def __init__(self, stdin: TextIO, on_line: Callable[[str], None]) -> None:
        super().__init__(daemon=True, name="YukTrackerConsole")
        self._stdin = stdin
        self._on_line = on_line

    def run(self) -> None:
        try:
            for line in self._stdin:
                self._on_line(line.rstrip("\r\n"))
        except Exception:
            pass


def run(opts: RunOptions, *, engine_factory=None, recorder=None,
        indexer: Optional[PidIndexer] = None, stdin: Optional[TextIO] = None,
        out: Optional[TextIO] = None, clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep) -> int:
    """관측기 1회 실행. 주입 인자는 전부 테스트용(기본 = 실 엔진·전역 recorder·stdin/stdout)."""
    out = out if out is not None else sys.stdout
    stdin = stdin if stdin is not None else sys.stdin
    recorder = recorder if recorder is not None else DISCOVERY_RECORDER
    indexer = indexer if indexer is not None else PidIndexer(game_pids)
    factory = engine_factory if engine_factory is not None else PacketStateSource
    say_lock = threading.Lock()

    def say(msg: str) -> None:
        with say_lock:
            print(f"{time.strftime('%H:%M:%S')} {msg}", file=out, flush=True)

    if opts.attach_log:
        _attach_console_log(out)

    mode = (f"수집 {opts.capture_min:g}분" if opts.capture_min
            else "관측 대기(파서 미탑재 — 스니퍼 생존·흐름 락 확인용)")
    try:
        ledger = ledger_paths.packet_dir()
    except Exception:
        ledger = None
    say(f"육의전 관측기 v{__version__} — 모드: {mode}")
    say(f"원장 폴더: {ledger or '?'}"
        + ("" if onedrive_available() else "  ⚠ OneDrive 미연동 — 이 PC 에만 남습니다(직접 복사 필요)"))

    stop = threading.Event()
    final = threading.Event()
    state = {"window": False, "labels": 0, "finished": False}

    def event_cb(slot_idx: int, kind: str) -> None:
        # 전투 IN/OUT·조철·미상 팝업 — 스트림이 실제로 디코드되고 있다는 생존 증거.
        say(f"[패킷] 클라{slot_idx + 1} 이벤트 — {kind}")

    engine = factory(
        indexer.provide, status_cb=say, event_cb=event_cb,
        segment_cb=recorder.note_segment,
        enter_anchor=os.environ.get("SEASSIST_PACKET_ENTER_ANCHOR", "1").strip() != "0")

    def close_window(reason: str) -> None:
        if not state["window"]:
            return
        state["window"] = False
        try:
            health = engine.health_snapshot()
        except Exception:
            health = {}
        say(f"[발굴] {recorder.end_window(reason, health)}")

    def on_line(text: str) -> None:
        t = text.strip()
        if state["finished"]:
            final.set()
            return
        if t.lower() in QUIT_WORDS:
            stop.set()
            return
        if not t:
            return
        if state["window"]:
            state["labels"] += 1
            recorder.note_label(LABEL_EVENT, note=t)
            say(f"[발굴] 라벨 #{state['labels']} 기록 — {t}")
        else:
            say(f"[발굴] 수집 창이 없어 라벨을 기록하지 않았습니다 — {t}")

    ok, msg = engine.start()
    say(f"[패킷] {msg}")
    if not ok:
        say("[패킷] Npcap 설치(https://npcap.com)와 관리자 권한을 확인하세요")
        return _finish(RC_ENGINE, opts, say, final, state)

    def on_console_close(reason: str) -> None:
        # 콘솔 X·로그오프·셧다운 — 핸들러 스레드에서 창을 마감하고 엔진을 세운다(프로세스는 곧 죽는다).
        say("[발굴] 콘솔 종료 신호 — 수집 창을 마감합니다")
        close_window(reason)
        try:
            engine.stop()
        except Exception:
            pass

    remove_close_hook = _install_console_close_hook(on_console_close)
    rc = RC_OK
    try:
        if opts.capture_min:
            rc = _open_window(opts, engine, recorder, indexer, state, stop, say, clock, sleep)
        # 콘솔은 수집 창이 열린 **뒤에** 읽기 시작한다 — 그 전에 친 줄은 라벨이 될 창이 없다
        # (흐름 대기 중 중단은 Ctrl+C). 관측 대기 모드는 바로 읽는다.
        if rc == RC_OK:
            _Console(stdin, on_line).start()
            while not stop.is_set():
                sleep(_TICK_SEC)
                if state["window"]:
                    reason = recorder.take_auto_stop_reason()
                    if reason:
                        say(f"[발굴] 자동 종료 — {reason}")
                        close_window(reason)
                        stop.set()  # 수집 모드는 창이 닫히면 할 일이 끝난다
    except KeyboardInterrupt:
        say("중단 요청(Ctrl+C)")
    finally:
        remove_close_hook()
        close_window("manual")
        try:
            engine.stop()
        except Exception:
            pass
        try:
            h = engine.health_snapshot()
            say(f"[패킷] 종료 헬스 — 패킷 {h.get('packets', 0)} / 세그 {h.get('fed_segments', 0)} / "
                f"흐름 {h.get('tracked_flows', 0)} / 전투 {h.get('battle_events', 0)} / "
                f"오픈 {h.get('opens', 0)} / 라벨 {state['labels']}")
        except Exception:
            pass
    return _finish(rc, opts, say, final, state)


def _open_window(opts, engine, recorder, indexer, state, stop, say, clock, sleep) -> int:
    """캡처 핸들이 열릴 때까지 기다린 뒤 발굴 창을 연다 — SEAssist GUI 의 시작 거부 규칙과 같다
    (스니퍼 준비 전엔 열지 않는다 — 0세그 빈 창 방지, PACKET-TOOLS §0)."""
    deadline = clock() + max(1.0, float(opts.flow_wait_sec))
    announced = False
    while not engine.is_capturing():
        if stop.is_set():
            return RC_OK
        if not announced:
            say("[발굴] 게임 클라이언트의 서버 흐름(8000) 대기 중 — 거상이 켜져 있고 서버에 "
                "접속돼 있어야 합니다")
            announced = True
        if clock() >= deadline:
            say("[발굴] 대기 초과 — 수집 창을 열지 않았습니다. 거상 클라이언트·Npcap·관리자 권한을 "
                "확인하고 다시 실행하세요")
            return RC_NO_FLOW
        sleep(_FLOW_POLL_SEC)
    recorder.enable_persistence()
    labels = {i: f"클라{i + 1}" for i in sorted(indexer.known)}
    ok, msg = recorder.begin_window(labels, duration_sec=float(opts.capture_min) * 60.0)
    say(f"[발굴] {msg}")
    if not ok:
        return RC_WINDOW
    state["window"] = True
    say("[발굴] 콘솔에 한 줄 치고 Enter = 라벨(예: '육의전 열기' · '검색 소나무' · '2페이지' · '닫기') — "
        "동작 직후 짧게, 목록 상세 메모는 다음 줄로. q + Enter = 종료(한글 상태의 ㅂ 도 종료)")
    return RC_OK


def _finish(rc: int, opts: RunOptions, say, final: threading.Event, state: dict) -> int:
    state["finished"] = True
    if opts.pause_on_exit:
        say("Enter 를 누르면 창을 닫습니다")
        final.wait(FINAL_WAIT_SEC)
    return rc
