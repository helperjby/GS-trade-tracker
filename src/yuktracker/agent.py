"""관측기 본체 — 스니퍼 엔진 배선 · 육의전 관측·업로드 · 발굴 수집 창 · 콘솔 라벨.

왜 별도 프로그램인가
--------------------
SEAssist 의 패킷 엔진은 War/Mon 매크로 세션이 도는 동안에만 뜬다. 육의전 목록은 유저가
육의전을 **직접 열 때만** 내려오는데 그 순간 매크로가 돌고 있으리란 보장이 없다. 그래서
엔진(`packet_state_source`)·프레이머(`gersang_protocol`)·수집기(`packet_discovery_ledger`)를
벤더 사본으로 **그대로 재사용**하되, 슬롯·러너·Tk 없이 관리자 콘솔 하나로 도는 관측기를 둔다.

범위
----
- 게임 프로세스(`gersang.exe`)를 전부 자동으로 잡아 pseudo-슬롯(클라1·2·…)으로 엔진에 넘긴다.
- ``capture_min`` 이 있으면 그 길이의 발굴 창(`window_*.jsonl`)을 SEAssist GUI `[패킷 수집]` 과
  **같은 형식·같은 폴더**에 남긴다 — SEAssist 의 `packet_explore.py`/`mine_packet_discovery.py`
  가 그대로 읽는다. 콘솔에 한 줄 치면 `market_manual` 라벨이 찍힌다(육의전 열기·검색·페이지
  넘김 시각을 창 안에 남기는 유일한 수단).
- 육의전 목록(`0x321f`)이 오면 엔진이 파싱한 페이지를 `market_cb` 로 받아 아이템 이름을 붙이고
  스풀에 넣는다(`market_observer` → `spool`). 업로더 스레드가 허브로 올린다 — 첫 실행에는
  초대 코드로 기기를 등록한다(`hub_setup`, `docs/HUB-PROTOCOL.md` §3-0). 허브 주소·토큰이 없으면
  업로드 없이 관측만 한다.

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
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, TextIO

from . import __version__, app_config, hub_setup, market_observer, spool
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
#: 관측 모드 상태 줄 주기 — 흐름을 못 잡은 동안의 재안내와, 잡은 뒤 육의전을 아직 못 본 동안의 재안내.
#: 지인 PC 의 콘솔이 조용하면 "되고 있는지" 를 아무도 모른다(엔진의 5분 헬스 INFO 줄은 개발자용).
FLOW_REMIND_SEC = 30.0
IDLE_REMIND_SEC = 600.0

#: 종료 코드.
RC_OK = 0
RC_ENGINE = 2      # Npcap/권한/디바이스 — 엔진 시작 실패
RC_NO_FLOW = 3     # 게임 흐름 대기 초과
RC_WINDOW = 4      # 수집 창 열기 실패(폴더/writer)

_console_log_attached = False


class ConsoleOut:
    """콘솔 출력 한 곳 — `say` 와 로거 핸들러가 여기로 온다. `start()` 뒤에는 **쓰기 스레드**가 대신 쓴다.

    conhost 는 마우스 선택(QuickEdit) 중 콘솔 쓰기를 통째로 멈춘다. 스니퍼 스레드가 그 `print` 에 걸리면 패킷 처리·
    관측 enqueue·업로드가 함께 멈추고 풀릴 때 몰려온다(F1_JBY G7 2차 2026-09-23: 페이지 인식 14:59 → 허브 15:01, 3분).
    엔진이 뜨기 전(등록 프롬프트 등)은 순서가 중요하니 동기로 쓰고, 뜬 뒤에는 큐에 넣고 바로 돌아온다 — 표시만 밀린다.
    큐가 차면(콘솔이 오래 막혀 있으면) 새 줄을 버리고 세어 두었다가 `stop()` 때 한 줄로 알린다.
    """
    MAX_LINES = 2000
    _STOP = object()

    def __init__(self, out: TextIO) -> None:
        self._out = out
        self._q: "queue.Queue" = queue.Queue(maxsize=self.MAX_LINES)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.dropped = 0

    def _emit(self, line: str) -> None:
        with self._lock:
            try:
                print(line, file=self._out, flush=True)
            except Exception:
                pass                       # 콘솔이 닫힌 뒤의 출력 — 관측을 죽일 이유가 없다

    def write_line(self, line: str) -> None:
        if self._thread is None:
            self._emit(line)
            return
        try:
            self._q.put_nowait(line)
        except queue.Full:
            self.dropped += 1

    def say(self, msg: str) -> None:
        self.write_line(f"{time.strftime('%H:%M:%S')} {msg}")

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="YukTrackerConsoleOut", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is self._STOP:
                return
            self._emit(item)

    def stop(self, timeout: float = 3.0) -> None:
        """큐를 비우고 스레드를 끝낸다. 콘솔이 아직 막혀 있으면 기다리다 포기하고 동기 모드로 돌아간다 — 남은 줄은
        스레드가 콘솔이 풀릴 때 마저 쓴다(데몬)."""
        t = self._thread
        if t is None:
            return
        try:
            self._q.put(self._STOP, timeout=timeout)
        except queue.Full:
            pass
        t.join(timeout)
        self._thread = None
        if self.dropped:
            self._emit(f"[콘솔] 콘솔이 막혀 있는 동안 표시 줄 {self.dropped}개를 생략했습니다(관측·업로드는 계속됐습니다)")
            self.dropped = 0


class _ConsoleLogHandler(logging.Handler):
    """로거 → ConsoleOut. StreamHandler 로 직접 쓰면 5분 헬스 INFO 줄이 스니퍼 스레드를 콘솔에 묶는다."""

    def __init__(self, console: ConsoleOut) -> None:
        super().__init__(logging.INFO)
        self._console = console

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._console.write_line(self.format(record))
        except Exception:
            pass


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
    #: 허브 주소 오버라이드(빈 값 = 설정·환경변수·빌드 주입 순).
    hub_url: str = ""
    #: 첫 실행 등록용 초대 코드(빈 값 = 콘솔 프롬프트). 저장하지 않는다.
    invite_code: str = ""
    #: 허브 목록에 보일 이름(빈 값 = 호스트명).
    device_label: str = ""
    #: 아이템 표를 찾을 클라 폴더(빈 값 = 실행 중 gersang.exe → 기본 설치 경로).
    client_dir: str = ""
    #: False 면 스풀·업로더를 아예 띄우지 않는다(`--no-upload`).
    upload: bool = True


def _attach_console_log(console: ConsoleOut) -> None:
    """SEAssist 로거의 INFO(5분 헬스 줄)·WARNING 을 콘솔에. GUI 와 달리 INFO 소비자가 있으니
    레벨을 INFO 로 내린다(logger.py 의 불변식 — INFO 는 소비 핸들러와 함께). 쓰기는 ConsoleOut 을 거친다."""
    global _console_log_attached
    if _console_log_attached:
        return
    log = get_logger()
    log.setLevel(logging.INFO)
    handler = _ConsoleLogHandler(console)
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


@dataclass
class Market:
    """육의전 배선 한 묶음 — 콜백(엔진에 넘길 것)과 업로더. 수명은 **이 객체가** 쥔다: `start()`/`close()` 한 쌍을
    `run()` 이 부른다(G7 1차: 만들기만 하고 start 를 빠뜨려 관측이 큐에서 썩었다 — 그 배치를 한 곳에 모은 것)."""
    cb: Optional[Callable[..., None]] = None
    uploader: Optional[spool.Uploader] = None
    observer: Optional[market_observer.MarketObserver] = None
    #: `close()` 뒤 스풀에도 못 내린 관측 수(스레드가 POST 에 붙들려 있고 마지막 내림도 실패한 경우) — 종료 요약에 찍는다.
    unsaved: int = 0

    def start(self, say: Callable[[str], None]) -> None:
        """업로더 스레드를 띄운다. 못 띄우면(스레드 한도) 업로드 없이 관측만 — 관측 콜백은 그대로 산다."""
        if self.uploader is None:
            return
        try:
            self.uploader.start()
        except RuntimeError as e:
            say(f"[허브] 업로더를 띄우지 못했습니다 — 업로드 없이 관측만 합니다: {e}")
            self.uploader = None

    def close(self) -> None:
        """업로더를 멈추고 잠깐 기다린 뒤, 스레드가 아직 POST 에 붙들려 있으면 큐를 **메인 스레드에서** 스풀에 내린다
        (다음 실행이 올린다). 여러 번 불러도 된다(콘솔 종료 훅과 finally 가 둘 다 부른다)."""
        up = self.uploader
        if up is None:
            return
        try:
            up.stop()
            if up.is_alive():
                up.join(timeout=3.0)
            if up.is_alive() or up.queue_size():
                while up.drain_queue(wait=False) is not None:
                    pass
            self.unsaved = up.queue_size()
        except Exception:
            self.unsaved = up.queue_size() if up is not None else 0


def setup_market(opts: RunOptions, say: Callable[[str], None], stdin: Optional[TextIO], *,
                 config_path=None, spool_dir=None, names=None,
                 register=None, post=None, uploader_kw: Optional[dict] = None) -> Market:
    """아이템 표 → 설정 → (필요하면) 첫 실행 등록 → 스풀·업로더.

    업로드가 안 되는 상황(주소 없음·등록 실패·`--no-upload`)에서도 **관측 콜백은 만든다** —
    콘솔에 페이지가 보여야 사용자가 "되고 있다"를 안다. 주입 인자는 테스트용이다.
    """
    kw = {"register": register} if register is not None else {}
    table = names if names is not None else market_observer.ItemNames.load(client_dir=opts.client_dir)
    if table.rows:
        say(f"[아이템표] {table.rows}건 (gcs {table.gcs_path})")
    else:
        say("[아이템표] 없음 — 아이템 이름 없이 관측합니다(허브가 다른 PC 의 이름으로 채웁니다).")

    cfg = app_config.load(config_path)
    local_id = app_config.ensure_local_id(cfg, config_path)
    uploader = None
    hub_url = app_config.resolve_hub_url(opts.hub_url, cfg)
    if not opts.upload:
        say("[허브] --no-upload — 업로드 없이 관측만 합니다.")
    elif not hub_url:
        say("[허브] 주소가 없습니다 — --hub-url 로 주거나 빌드에 주입하세요(업로드 없이 관측만).")
    elif hub_setup.ensure_registered(cfg, hub_url, invite_code=opts.invite_code,
                                     label=opts.device_label, say=say, stdin=stdin,
                                     config_path=config_path, **kw):
        store = spool.Spool(spool_dir)
        uploader = spool.Uploader(store, hub_url=hub_url, token=cfg.hub_token,
                                  device_id=lambda: cfg.hub_device_id, say=say, names=table,
                                  **({"post": post} if post is not None else {}), **(uploader_kw or {}))
        say(f"[허브] {hub_url} 기기 {cfg.hub_device_id}")
        waiting = len(store.pending())
        if waiting:
            say(f"[허브] 지난 실행의 스풀 {waiting}건부터 올립니다.")
    observer = market_observer.MarketObserver(
        local_id, table, say=say, enqueue=uploader.enqueue if uploader is not None else None)
    return Market(cb=observer, uploader=uploader, observer=observer)


def run(opts: RunOptions, *, engine_factory=None, recorder=None,
        indexer: Optional[PidIndexer] = None, stdin: Optional[TextIO] = None,
        out: Optional[TextIO] = None, clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        market_setup: Callable[..., Market] = setup_market) -> int:
    """관측기 1회 실행. 주입 인자는 전부 테스트용(기본 = 실 엔진·전역 recorder·stdin/stdout)."""
    out = out if out is not None else sys.stdout
    stdin = stdin if stdin is not None else sys.stdin
    recorder = recorder if recorder is not None else DISCOVERY_RECORDER
    indexer = indexer if indexer is not None else PidIndexer(game_pids)
    factory = engine_factory if engine_factory is not None else PacketStateSource
    console = ConsoleOut(out)
    say = console.say
    if opts.attach_log:
        _attach_console_log(console)

    mode = (f"관측 + 수집 {opts.capture_min:g}분" if opts.capture_min else "관측")
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

    market = market_setup(opts, say, stdin)

    engine = factory(
        indexer.provide, status_cb=say, event_cb=event_cb,
        segment_cb=recorder.note_segment, market_cb=market.cb,
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
        market.close()   # 큐에 남은 관측을 스풀에 — CTRL_CLOSE 의 유예 몇 초 안에 끝난다(join 3s + 파일 쓰기)

    remove_close_hook = _install_console_close_hook(on_console_close)
    rc = RC_OK
    try:
        # 엔진이 뜬 **뒤**, try 안에서 — 엔진 실패로 일찍 나가는 길에는 띄우지 않고, finally 가 반드시 close 한다.
        # 만들기만 하고 start 를 빠뜨리면 관측이 큐에 쌓인 채 전송도 스풀도 안 된다(실기기 G7 1차, 2026-09-23).
        market.start(say)
        console.start()   # 여기부터 콘솔 쓰기는 별도 스레드 — 스니퍼·업로더가 막힌 콘솔에 붙들리지 않는다(G7 2차)
        if opts.capture_min:
            rc = _open_window(opts, engine, recorder, indexer, state, stop, say, clock, sleep)
        # 콘솔은 수집 창이 열린 **뒤에** 읽기 시작한다 — 그 전에 친 줄은 라벨이 될 창이 없다
        # (흐름 대기 중 중단은 Ctrl+C). 관측 대기 모드는 바로 읽는다.
        if rc == RC_OK:
            _Console(stdin, on_line).start()
            # 이미 흐름을 잡은 채로 들어오면(수집 모드는 _open_window 가 기다렸다) 첫 틱이 시작을
            # 다시 알리지 않는다 — 엔진의 `[패킷] 캡처 시작 — 서버 …` 줄이 그 증거다.
            status: dict = {"capturing": True} if _capturing(engine) else {}
            while not stop.is_set():
                sleep(_TICK_SEC)
                line = _status_tick(status, capturing=_capturing(engine),
                                    pages=market.observer.pages if market.observer else 0,
                                    now=clock())
                if line:
                    say(line)
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
        market.close()
        try:
            h = engine.health_snapshot()
            say(f"[패킷] 종료 헬스 — 패킷 {h.get('packets', 0)} / 세그 {h.get('fed_segments', 0)} / "
                f"흐름 {h.get('tracked_flows', 0)} / 전투 {h.get('battle_events', 0)} / "
                f"오픈 {h.get('opens', 0)} / 라벨 {state['labels']} / "
                f"육의전 {h.get('market_frames', 0)}쪽 {h.get('market_rows', 0)}행"
                f"(거부 {h.get('market_rejected', 0)} · 유실 {h.get('market_dropped', 0)})")
        except Exception:
            pass
        _say_upload_health(market, say)
        console.stop()    # 밀린 표시를 비우고 동기 모드로 — 종료 안내는 바로 보이게
    return _finish(rc, opts, say, final, state)


def _status_tick(state: dict, *, capturing: bool, pages: int, now: float) -> Optional[str]:
    """관측 상태 줄 1건(없으면 None) — 틱마다 부르는 **순수 판정부**.

    관측 모드는 수집 창이 없어 원래 아무 줄도 찍지 않는다. 지인 PC 에서는 그 침묵이 "거상이 꺼져 있다"·
    "Npcap 이 다른 어댑터를 잡았다"·"잘 돌고 있다"를 구분해 주지 못한다. 그래서 ① 흐름 전이(잡음·끊김)는
    **그때 한 번**, ② 흐름을 못 잡은 동안은 ``FLOW_REMIND_SEC`` 마다, ③ 흐름은 잡았는데 육의전을 아직 못 본
    동안은 ``IDLE_REMIND_SEC`` 마다 한 줄씩 낸다. 첫 관측 뒤에는 ③ 이 멎는다(목록 줄이 대신 찍힌다).
    ``capturing`` 은 캡처 핸들이 아니라 **흐름**(`_capturing`)이다 — "캡처 시작" 문구는 엔진의 상태 줄이
    이미 쓰므로 여기서는 되풀이하지 않고 다음 행동(육의전 열기)을 말한다.

    ``state`` 는 호출자가 들고 있는 dict 하나다(키: 최근 캡처 상태·마지막 대기 안내·마지막 무관측 안내).
    """
    was = state.get("capturing")
    if capturing != was:
        state["capturing"] = capturing
        state["flow_said"] = now
        state["idle_said"] = now
        if capturing:
            if was is None:
                return "[패킷] 게임 서버 흐름을 잡았습니다 — 육의전을 한 번 열면 여기에 목록이 찍힙니다"
            return "[패킷] 게임 서버 흐름을 다시 잡았습니다 — 관측을 계속합니다"
        if was is None:
            return None        # 시작 직후의 미개통은 정상 — 첫 안내는 FLOW_REMIND_SEC 뒤에
        return "[패킷] 게임 서버 흐름이 끊겼습니다 — 거상이 켜져 있는지 확인하세요(다시 잡히면 알려 줍니다)"
    if not capturing:
        if now - state.get("flow_said", now) >= FLOW_REMIND_SEC:
            state["flow_said"] = now
            return "[패킷] 게임 서버 흐름(8000) 대기 중 — 거상이 켜져 있고 서버에 접속돼 있어야 합니다"
        return None
    if pages <= 0 and now - state.get("idle_said", now) >= IDLE_REMIND_SEC:
        state["idle_said"] = now
        return "[육의전] 아직 목록을 못 봤습니다 — 육의전 창을 열어 주세요(열 때만 목록이 내려옵니다)"
    return None


def _capturing(engine) -> bool:
    """게임 서버 흐름이 살아 있는가 — 캡처 핸들이 열려 있고(`is_capturing`) 8000 흐름을 추적 중인가.

    핸들만 보면 거상을 끄거나 접속이 끊겨도 True 로 남는다 — 핸들은 `stop()` 과 읽기 오류에서만 닫히고
    하우스키핑은 필터를 넓힐 뿐이다. `tracked_flows` 는 pid·흐름이 사라지면 FLOW_MISS_LIMIT 번의
    하우스키핑(REFRESH_SEC) 뒤 0 이 된다(종료 헬스 줄이 이미 읽는 값). 그래서 "끊겼습니다" 가 실제로 뜬다.
    """
    try:
        if not engine.is_capturing():
            return False
        return int(engine.health_snapshot().get("tracked_flows", 0)) > 0
    except Exception:
        return False


def _say_upload_health(market: Market, say: Callable[[str], None]) -> None:
    obs, up = market.observer, market.uploader
    if obs is None:
        return
    line = f"[육의전] 관측 {obs.pages}쪽 {obs.rows}행 / 이름 미해석 {obs.unknown_item}행"
    if up is not None:
        try:
            waiting = len(up.spool.pending())
        except Exception:
            waiting = -1
        line += (f" / 업로드 {up.uploaded}건 · 대기 {waiting}배치 · 격리 {up.quarantined}"
                 f" · 큐 유실 {up.queue_dropped}")
        if up.halted:
            line += f" · 정지({up.stopped_reason})"
        if market.unsaved:
            line += f" · 큐 미저장 {market.unsaved}(유실)"
    say(line)


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
