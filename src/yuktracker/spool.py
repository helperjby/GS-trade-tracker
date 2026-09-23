"""로컬 스풀과 업로더 스레드 — at-least-once 로 허브에 올린다(허브가 `obs_id` 로 dedup).

배치 1개 = 파일 1개(`%APPDATA%\\YukTracker\\spool\\obs_<epoch_ms>_<n>.jsonl`, 관측 1건 = 1줄).
**보내기 전에 파일부터 쓴다** — 그래야 프로세스가 죽어도 관측이 남고, 다음 실행이 오래된 것부터
이어서 올린다. 성공(200) 응답 뒤에만 지운다.

스니퍼 스레드는 `enqueue()` 로 큐에 넣기만 하고(파일·네트워크 금지), 업로더 스레드가 꺼내 쓴다.
행동 결정은 `hub_client.classify_upload` 한 곳에 있다:

| 행동 | 여기서 하는 일 |
|---|---|
| `ok` | 스풀 파일 삭제 |
| `retry` | 지수 백오프(1s → 300s) 뒤 같은 파일 재시도 |
| `wait` | `Retry-After` 만큼 자고 재시도(격리 아님) |
| `split` | 배치를 반으로 쪼개 다시 |
| `quarantine` | `spool/quarantine/` 으로 옮김(재시도해도 같은 400) |
| `stop` | 업로더 정지 — 스풀은 **유지**하고 사유를 상태 줄에. 자동 재등록은 하지 않는다 |
"""
from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import hub_client, paths
from .seassist.logger import get_logger

#: 큐 상한 — 업로더가 막혀도 메모리가 불지 않게. 넘치면 새 관측을 버리고 센다(부분 스냅샷이 전제다).
QUEUE_MAX = 512
#: 백오프 — 네트워크·5xx.
BACKOFF_MIN_SEC = 1.0
BACKOFF_MAX_SEC = 300.0
#: 큐를 모으는 시간(이 시간 안에 온 관측은 한 배치로).
BATCH_WAIT_SEC = 2.0


class Spool:
    """스풀 폴더 하나. 파일 이름이 곧 순서다(`obs_<13자리 ms>_<4자리 일련>`)."""

    def __init__(self, directory: Optional[Path] = None, quarantine: Optional[Path] = None,
                 *, wall: Callable[[], float] = time.time) -> None:
        self.dir = Path(directory) if directory is not None else paths.spool_dir()
        self.quarantine_dir = (Path(quarantine) if quarantine is not None
                               else (self.dir / paths.QUARANTINE_DIR_NAME
                                     if directory is not None else paths.quarantine_dir()))
        self._wall = wall
        self._seq = 0

    def write(self, observations: list) -> Path:
        """관측들을 파일 1개로. 고유 tmp → `os.replace` 라 반쪽 파일이 목록에 잡히지 않는다."""
        self.dir.mkdir(parents=True, exist_ok=True)
        self._seq = (self._seq + 1) % 10000
        path = self.dir / f"obs_{int(self._wall() * 1000):013d}_{self._seq:04d}.jsonl"
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for obs in observations:
                    f.write(json.dumps(obs, ensure_ascii=False) + "\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path

    def pending(self) -> list[Path]:
        """오래된 것부터. 격리 폴더는 하위에 있으므로 `glob` 이 섞이지 않는다."""
        try:
            return sorted(p for p in self.dir.glob("obs_*.jsonl") if p.is_file())
        except OSError:
            return []

    def read(self, path: Path) -> list[dict]:
        """깨진 줄은 건너뛴다 — 한 줄이 잘렸다고 배치 전체를 잃지 않는다."""
        out: list[dict] = []
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            get_logger().warning("스풀 파일을 읽지 못했습니다(%s): %s", path, e)
            return out
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obs = json.loads(line)
            except ValueError:
                continue
            if isinstance(obs, dict):
                out.append(obs)
        return out

    def done(self, path: Path) -> None:
        try:
            Path(path).unlink()
        except OSError as e:
            get_logger().warning("스풀 파일을 지우지 못했습니다(%s): %s", path, e)

    def quarantine(self, path: Path, reason: str) -> Optional[Path]:
        """400 배치를 옆으로. 사유는 같은 이름의 `.why` 파일에 남긴다."""
        path = Path(path)
        try:
            self.quarantine_dir.mkdir(parents=True, exist_ok=True)
            target = self.quarantine_dir / path.name
            os.replace(path, target)
            (self.quarantine_dir / (path.name + ".why")).write_text(reason + "\n", encoding="utf-8")
            return target
        except OSError as e:
            get_logger().warning("스풀 파일을 격리하지 못했습니다(%s): %s", path, e)
            return None

    def split(self, path: Path) -> list[Path]:
        """배치를 반으로. 1건짜리는 쪼갤 수 없으니 빈 목록(호출자가 격리한다)."""
        obs = self.read(path)
        if len(obs) < 2:
            return []
        half = len(obs) // 2
        parts = [self.write(obs[:half]), self.write(obs[half:])]
        self.done(path)
        return parts


class Uploader(threading.Thread):
    """스풀을 비우는 백그라운드 스레드 1개."""

    def __init__(self, spool: Spool, *, hub_url: str, token: str,
                 device_id: Callable[[], str], say: Optional[Callable[[str], None]] = None,
                 names=None, post=hub_client.upload,
                 clock: Callable[[], float] = time.monotonic,
                 batch_wait: float = BATCH_WAIT_SEC) -> None:
        """백오프·429 대기는 **스레드를 재우지 않는다** — ``retry_at``(단조시계) 이전에는 전송만 건너뛰고 루프는 계속 큐를
        스풀로 내린다(허브가 죽은 동안에도 관측이 큐에서 넘쳐 버려지지 않게, ``stop()`` 이 즉시 먹게)."""
        super().__init__(name="YukTrackerUploader", daemon=True)
        self.spool = spool
        self.hub_url = hub_url
        self.token = token
        self.device_id = device_id
        self.names = names
        self._post = post
        self._clock = clock
        self._batch_wait = batch_wait
        #: 이 시각(단조) 전에는 전송을 시도하지 않는다. ``retry_delay`` 는 마지막으로 정한 대기(표시·테스트용).
        self.retry_at = 0.0
        self.retry_delay = 0.0
        self._say = say or (lambda msg: None)
        self._q: "queue.Queue[dict]" = queue.Queue(maxsize=QUEUE_MAX)
        self._stop = threading.Event()
        self.stopped_reason = ""
        self.uploaded = 0
        self.queue_dropped = 0
        self.quarantined = 0
        self._backoff = BACKOFF_MIN_SEC

    # ── 스니퍼 스레드가 부르는 부분 ────────────────────────────────────
    def enqueue(self, envelope: dict) -> None:
        """넘치면 **새 관측을 버린다** — 육의전 DB 는 애초에 부분·지연 스냅샷이고, 여기서 막히면
        스니퍼 스레드가 멎어 창 전체를 잃는다."""
        try:
            self._q.put_nowait(envelope)
        except queue.Full:
            self.queue_dropped += 1

    # ── 업로더 스레드 ──────────────────────────────────────────────────
    def stop(self) -> None:
        self._stop.set()

    @property
    def halted(self) -> bool:
        """자격 문제로 전송을 멈춘 상태(스풀은 쌓인다)."""
        return bool(self.stopped_reason)

    def queue_size(self) -> int:
        """아직 스풀에 안 내린 관측 수(메모리 큐) — 종료 요약의 '미저장'."""
        return self._q.qsize()

    def _defer(self, delay: float) -> None:
        self.retry_delay = float(delay)
        self.retry_at = self._clock() + self.retry_delay

    def drain_queue(self, *, wait: bool = True) -> Optional[Path]:
        """큐에 있는 만큼(상한 100) 모아 스풀 파일 1개로. 없으면 None. ``wait`` 면 첫 항목을 ``batch_wait`` 까지 기다린다
        (루프의 박자) — 종료 직전·메인 스레드의 마지막 내림은 기다리지 않는다."""
        batch: list[dict] = []
        try:
            batch.append(self._q.get(timeout=self._batch_wait) if wait else self._q.get_nowait())
        except queue.Empty:
            return None
        while len(batch) < hub_client.MAX_OBSERVATIONS:
            try:
                batch.append(self._q.get_nowait())
            except queue.Empty:
                break
        try:
            return self.spool.write(batch)
        except OSError as e:
            get_logger().warning("스풀에 쓰지 못했습니다 — 이 배치는 버린다: %s", e)
            self.queue_dropped += len(batch)
            return None

    def send_one(self, path: Path) -> str:
        """파일 1개를 보내고 행동을 돌려준다. 빈 파일은 그냥 지운다."""
        observations = self.spool.read(path)
        if not observations:
            self.spool.done(path)
            return "ok"
        resp = self._post(self.hub_url, self.token, self.device_id(), observations)
        action, why = hub_client.classify_upload(resp)
        if action == "ok":
            self.spool.done(path)
            self.uploaded += len(observations)
            self._backoff = BACKOFF_MIN_SEC
            return action
        if action == "quarantine":
            self.spool.quarantine(path, why)
            self.quarantined += 1
            self._say(f"[허브] {why} — 격리했습니다.")
            return action
        if action == "split":
            if not self.spool.split(path):
                self.spool.quarantine(path, why)
                self.quarantined += 1
                self._say(f"[허브] {why} 더 못 쪼갭니다 — 격리했습니다.")
                return "quarantine"
            self._say(f"[허브] {why}")
            return action
        if action == "stop":
            self.stopped_reason = why
            self._say(f"[허브] {why}")
            return action
        if action == "wait":
            self._say(f"[허브] {why}")
            self._defer(max(1.0, resp.retry_after))
            return action
        # retry — 재우지 않고 다음 시도 시각만 정한다(루프는 그동안 큐를 계속 스풀로 내린다)
        self._say(f"[허브] {why} — {self._backoff:.0f}초 뒤 다시 시도합니다.")
        self._defer(self._backoff)
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_SEC)
        return action

    def flush_pending(self) -> None:
        """오래된 것부터. `stop` 이 나오면 즉시 그만두고(스풀은 남는다), 백오프·대기 중(``retry_at`` 전)이면 아무것도 안 보낸다."""
        if self._clock() < self.retry_at:
            return
        for path in self.spool.pending():
            if self._stop.is_set() or self.halted:
                return
            if self.send_one(path) in ("retry", "wait"):
                return  # 다음 시도 시각이 정해졌다 — 그때 같은 파일부터

    def run(self) -> None:  # pragma: no cover - 스레드 진입점(본체는 아래 두 메서드)
        while not self._stop.is_set():
            self.drain_queue()                 # ≤ batch_wait 블록 — 루프의 박자이자 stop 반응 시간
            if not self.halted:
                self.flush_pending()           # retry_at 전이면 곧바로 돌아온다
            if self.names is not None and self.names.recheck():
                self._say(f"[아이템표] 클라 패치 감지 — {self.names.rows}건으로 갱신")
        while self.drain_queue(wait=False) is not None:   # 종료 직전 큐에 남은 것은 전부 스풀에(다음 실행이 올린다)
            pass
