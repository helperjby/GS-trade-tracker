from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

DEFAULT_CAPACITY = 1000


@dataclass(frozen=True)
class LogEntry:
    ts: datetime
    msg: str
    source: str = ""  # "" = 로컬 인스턴스, 그 외 = 원격 디바이스 hostname/display_name
    # severity 분류 (LogViewerDialog 에서 색상 태그):
    #   ""          — 일반 시스템 메시지 + 일반 감지 모드 (검정 기본).
    #                 Warfield/Image/Monster 의 status_cb 가 사용.
    #   "detection" — 자동대응 로그 (약한 빨강 #a04040). WordInput 의
    #                 status_cb 가 사용.
    #   "error"     — 즉시 인지 alert (강한 빨강 #d00000 + 굵게). 조철레타 등.
    severity: str = ""

    def format(self) -> str:
        if self.source:
            return f"{self.ts:%H:%M:%S} [{self.source}] {self.msg}"
        return f"{self.ts:%H:%M:%S} {self.msg}"


Observer = Callable[[LogEntry], None]


class LogHistory:
    """스레드 안전 링버퍼 + 옵저버.

    `App._post_status`가 호출될 때마다 `append`되어 사용자 가시 메시지를 누적.
    LogViewerDialog 가 `subscribe` 로 실시간 한 줄 추가를 받고, 다이얼로그가
    닫힐 때 unsubscribe 하여 메모리 누수를 방지한다.

    옵저버 콜백은 keyboard 워커 / 러너 스레드에서도 호출될 수 있으므로 GUI
    위젯을 직접 만지면 안 된다. 호출자가 `widget.after(0, ...)` 로 GUI 스레드
    마샬링을 책임진다.
    """

    _instance: "LogHistory | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._buf: deque[LogEntry] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._observers: list[Observer] = []

    @classmethod
    def instance(cls) -> "LogHistory":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = LogHistory()
            return cls._instance

    def _push(self, entry: LogEntry) -> None:
        with self._lock:
            self._buf.append(entry)
            observers = list(self._observers)
        for obs in observers:
            try:
                obs(entry)
            except Exception:
                pass

    def append(self, msg: str, severity: str = "") -> None:
        self._push(LogEntry(datetime.now(), msg, severity=severity))

    def append_remote(self, entry: LogEntry) -> None:
        """원격 인스턴스에서 받은 entry 를 로컬 옵저버에 fan-out.

        LAN client 가 다른 디바이스에서 받은 LogEntry 를 호출자가 그대로 넘기면
        기존 LogViewerDialog 가 별도 변경 없이 source prefix 와 함께 표시한다.
        """
        self._push(entry)

    def snapshot(self) -> list[LogEntry]:
        with self._lock:
            return list(self._buf)

    def iter_format(self) -> Iterable[str]:
        for e in self.snapshot():
            yield e.format()

    def subscribe(self, fn: Observer) -> Callable[[], None]:
        with self._lock:
            self._observers.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._observers:
                    self._observers.remove(fn)

        return unsubscribe

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
