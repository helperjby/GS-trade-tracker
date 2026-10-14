"""엔진 `satong_cb` → 사통팔달 업로드 봉투 + 수집기 상태(하트비트). 계약은 `docs/HUB-PROTOCOL.md` §3-8.

**운영자 PC 전용 기능이다** — 기본 꺼짐이고 `--satong <서버 번호>` 또는 설정 `satong_server_id` 로만 켠다.
허브도 `satong_slots` 에 든 슬롯의 기기만 받는다. 지인 배포판은 이 경로를 타지 않는다.

`SatongObserver` 는 스니퍼 스레드에서 돈다 — `market_observer.MarketObserver` 와 같은 규칙(가볍고 절대 던지지
않는다, 파일·네트워크는 업로더 스레드 몫).

슬롯 간 중복 — 같은 외치기는 이 PC 의 클라마다 한 번씩 온다(도착 차이 수 ms). 같은 (보낸이, 내용)이
``DEDUP_SEC`` 안에 다시 오면 한 건으로 친다. 서버는 사통을 약 15초 간격으로 한 건씩 내보내므로, 같은 사람이
같은 글을 연달아 외쳐도 10초 안에 두 번 오지는 않는다. 같은 클라 안의 TCP 재전송은 디코더가 이미 걸렀다.
허브도 기기 간 같은 규칙으로 한 번 더 합친다(PC 두 대가 수집할 때).

시각·`obs_id` 규칙은 `market_observer` 와 같다: ``agent_ts`` = 단조시계 관측 시각을 epoch 로 되돌린 값,
``obs_id = {local_id}:{agent_ts_ms}:{sha1(body)[:12]}``.

하트비트 — 미루봇은 "사통이 안 온다"와 "수집기가 죽었다"를 구분해야 한다(무응답 = 빈 결과가 아니라 실패).
`SatongUploader` 는 보낼 메시지가 없어도 ``HEARTBEAT_SEC`` 마다 빈 배치를 보내 수집기 상태(클라 연결 여부·마지막
사통 시각)를 허브에 남긴다. 메시지 배치에도 같은 상태가 실려 하트비트를 겸한다.
"""
from __future__ import annotations

import hashlib
import time
from typing import Callable, Optional

from . import hub_client, spool
from .seassist.logger import get_logger

#: 슬롯 간 중복 창(초) — 서버 큐 간격(≈15s)보다 짧게.
DEDUP_SEC = 10.0
#: 중복 표 정리 문턱 — 이보다 크면 창 밖 키를 쓸어낸다(사통은 분당 4건 남짓이라 평소엔 닿지 않는다).
_DEDUP_SWEEP_AT = 256
#: 하트비트 주기(초) — 허브·봇은 3배(180s)를 넘기면 수집기 오프라인으로 본다(HUB-PROTOCOL §3-8).
HEARTBEAT_SEC = 60.0
#: 콘솔 상태 줄 주기(초) — 메시지마다 찍으면 하루 수천 줄이라 건수만 이 주기로(`agent._satong_tick`).
REPORT_SEC = 600.0


def offline_collector() -> dict:
    """수집기 상태를 못 만들 때(관측자 없음·예외) 보내는 값 — 허브는 이것을 '클라 끊김'으로 본다."""
    return {"client_connected": False, "last_message_ts": None, "messages": 0, "agent_ts": time.time()}


class SatongObserver:
    """엔진 콜백 본체 — `satong_cb(slot_idx, message, observation, *, pid)`."""

    def __init__(self, local_id: str, *, enqueue: Optional[Callable[[dict], None]] = None,
                 say: Optional[Callable[[str], None]] = None,
                 wall: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.local_id = local_id
        self._enqueue = enqueue
        self._say = say
        self._wall = wall
        self._mono = mono
        #: 중복 제거 뒤 건수 · 걸러낸 사본 수 · 마지막 사통 시각(epoch) — 상태 줄·하트비트가 읽는다(단일 대입이라 무락).
        self.messages = 0
        self.duplicates = 0
        self.last_message_ts: Optional[float] = None
        self._recent: dict[tuple[str, str], float] = {}

    # ── 봉투 ────────────────────────────────────────────────────────────
    def agent_ts(self, observation) -> float:
        return self._wall() - (self._mono() - float(observation.ts))

    def obs_id(self, body: bytes, agent_ts: float) -> str:
        digest = hashlib.sha1(body).hexdigest()[:12]
        return f"{self.local_id}:{int(agent_ts * 1000)}:{digest}"

    def envelope(self, message, observation) -> dict:
        agent_ts = self.agent_ts(observation)
        return {
            "obs_id": self.obs_id(observation.body, agent_ts),
            "agent_ts": agent_ts,
            "opcode": int(observation.opcode),
            "sender": message.sender,
            "text": message.text,
            "anomalies": list(message.anomalies),
            # 진단용 원바이트(잔재로 보인다, D) — 허브는 payload 에만 보존한다.
            "mid": int(message.mid),
            "tail": message.tail_hex,
        }

    def _is_duplicate(self, key: tuple[str, str], ts: float) -> bool:
        last = self._recent.get(key)
        if last is not None and abs(ts - last) < DEDUP_SEC:
            return True
        self._recent[key] = ts
        if len(self._recent) > _DEDUP_SWEEP_AT:
            self._recent = {k: v for k, v in self._recent.items() if ts - v < DEDUP_SEC}
        return False

    # ── 콜백 ────────────────────────────────────────────────────────────
    def __call__(self, slot_idx: int, message, observation, *, pid: int = 0) -> None:
        try:
            if self._is_duplicate((message.sender, message.text), float(observation.ts)):
                self.duplicates += 1
                return
            env = self.envelope(message, observation)
            self.messages += 1
            self.last_message_ts = env["agent_ts"]
            if self._enqueue is not None:
                self._enqueue(env)
            if self.messages == 1 and self._say is not None:
                self._say(f"[사통] 클라{slot_idx + 1} 에서 첫 사통팔달을 받았습니다 — 이후는 {REPORT_SEC / 60:g}분마다 "
                          "건수만 찍습니다")
        except Exception:
            get_logger().exception("사통 관측 처리 실패 — 이 메시지는 버린다")

    def collector(self, *, connected: bool) -> dict:
        """하트비트·배치에 싣는 수집기 상태 — 허브 `satong_collectors` 한 행이 된다."""
        return {"client_connected": bool(connected), "last_message_ts": self.last_message_ts,
                "messages": int(self.messages), "agent_ts": self._wall()}


class SatongUploader(spool.Uploader):
    """사통 스풀을 비우는 업로더 + 하트비트. 행동 규칙(백오프·429·격리·정지)은 `spool.Uploader` 그대로다.

    배치는 `spool.Spool` 파일 1개 = 메시지 N건(관측기가 죽어도 남는다). 하트비트는 스풀에 쓰지 않는다 — 지난 상태를
    나중에 올려 봐야 쓸모가 없다. 보낼 스풀이 남아 있으면 그 배치가 하트비트를 겸하므로 따로 보내지 않는다.

    밀린 스풀은 **여러 파일을 요청 하나로** 묶어 보낸다(``MAX_OBSERVATIONS`` 까지). 사통은 15초에 한 건이라 파일 1개가
    대개 1건인데, 허브가 몇 시간 죽어 있다 살아나면 수천 개가 쌓인다 — 파일마다 요청하면 육의전 업로드와 같은 기기의
    속도제한을 다 써 버린다(허브도 사통 버킷을 따로 둔다, HUB-PROTOCOL §0).
    """

    def __init__(self, store: spool.Spool, *, hub_url: str, token: str, device_id: Callable[[], str],
                 server_id: int, collector: Callable[[], dict],
                 say: Optional[Callable[[str], None]] = None,
                 post=hub_client.upload_satong, clock: Callable[[], float] = time.monotonic,
                 heartbeat_sec: float = HEARTBEAT_SEC,
                 batch_wait: float = spool.BATCH_WAIT_SEC) -> None:
        self._satong_post = post
        self._collector = collector
        self.server_id = int(server_id)
        self.heartbeat_sec = float(heartbeat_sec)
        #: 마지막 전송 **시도** 시각(단조) — 실패도 센다(실패는 어차피 retry_at 이 막는다).
        self._last_sent: Optional[float] = None
        self._heartbeat_failing = False
        self.heartbeats = 0
        super().__init__(store, hub_url=hub_url, token=token, device_id=device_id, say=say,
                         post=self._send, clock=clock, batch_wait=batch_wait)
        self.name = "YukTrackerSatongUploader"

    def _send(self, hub_url: str, token: str, device_id: str, messages: list) -> hub_client.Response:
        self._last_sent = self._clock()
        try:
            status = self._collector()
        except Exception:
            status = offline_collector()
        return self._satong_post(hub_url, token, device_id, self.server_id, messages, status)

    def _send_group(self, paths: list, messages: list) -> str:
        """스풀 파일 여러 개를 요청 하나로. 200 이면 전부 지운다. 격리·분할(배치 안의 어느 파일이 문제인지 모른다)이면
        파일 하나씩 다시 보내 문제 파일만 격리되게 한다. 나머지 행동은 `react`(spool.Uploader 와 같은 규칙)."""
        resp = self._post(self.hub_url, self.token, self.device_id(), messages)
        action, why = hub_client.classify_upload(resp)
        if action == "ok":
            for p in paths:
                self.spool.done(p)
            self.uploaded += len(messages)
            self._backoff = spool.BACKOFF_MIN_SEC
            return action
        if action in ("quarantine", "split"):
            for p in paths:
                one = self.send_one(p)
                if one in ("retry", "wait", "stop"):
                    return one
            return "ok"
        return self.react(action, why, resp)

    def flush_pending(self) -> None:
        """오래된 것부터, 요청당 ``MAX_OBSERVATIONS`` 건까지 파일을 묶어서. 멈춤·대기 규칙은 `spool.Uploader` 와 같다."""
        if self._clock() < self.retry_at:
            return
        pending = self.spool.pending()
        tried: set = set()   # 이번 바퀴에 보낸 파일 — 200 뒤 삭제가 실패해 남아도 같은 바퀴에서 다시 보내지 않는다
        while pending and not self._stop.is_set() and not self.halted:
            group: list = []
            batch: list = []
            for p in pending:
                obs = self.spool.read(p)
                if group and len(batch) + len(obs) > hub_client.MAX_OBSERVATIONS:
                    break
                group.append(p)
                batch.extend(obs)
            tried.update(group)
            action = self.send_one(group[0]) if len(group) == 1 else self._send_group(group, batch)
            if action in ("retry", "wait"):
                return  # 다음 시도 시각이 정해졌다 — 그때 같은 파일부터
            # 분할·격리가 파일을 바꿀 수 있어 다시 본다(분할로 생긴 새 파일은 이름이 달라 이번 바퀴에 보낸다)
            pending = [p for p in self.spool.pending() if p not in tried]

    def heartbeat_due(self) -> bool:
        if self.halted or self._clock() < self.retry_at:
            return False
        return self._last_sent is None or self._clock() - self._last_sent >= self.heartbeat_sec

    def send_heartbeat(self) -> str:
        """빈 배치 1회 → 행동. 파일이 없으니 격리·분할은 재시도로 접는다(같은 400 이면 백오프가 300s 까지 늘어난다)."""
        resp = self._send(self.hub_url, self.token, self.device_id(), [])
        action, why = hub_client.classify_upload(resp)
        if action == "ok":
            self.heartbeats += 1
            self._backoff = spool.BACKOFF_MIN_SEC
            if self._heartbeat_failing:
                self._heartbeat_failing = False
                self._say("[허브] 하트비트가 다시 들어갑니다.")
            return action
        if action == "stop":
            self.stopped_reason = why
            self._say(f"[허브] {why}")
            return action
        if action == "wait":
            self._say(f"[허브] {why}")
            self._defer(max(1.0, resp.retry_after))
            return action
        # 다음 시도는 백오프와 하트비트 주기 중 늦은 쪽(`heartbeat_due` 가 마지막 시도 시각을 본다). 허브가 오래 죽어 있어도
        # 콘솔이 분마다 같은 줄로 차지 않게 연속 실패의 첫 번째만 알린다.
        if not self._heartbeat_failing:
            self._heartbeat_failing = True
            self._say(f"[허브] 하트비트 실패 — {why} {max(self._backoff, self.heartbeat_sec):.0f}초 뒤 다시 보냅니다"
                      "(복구되면 알려 줍니다).")
        self._defer(self._backoff)
        self._backoff = min(self._backoff * 2, spool.BACKOFF_MAX_SEC)
        return "retry"

    def tick(self) -> None:
        """루프 한 바퀴의 전송부 — 스풀부터(오래된 것부터), 비었으면 하트비트."""
        if self.halted:
            return
        self.flush_pending()
        # 하트비트 시각부터 — 대개 아니므로 스풀 폴더를 다시 훑지 않는다(2초 박자의 루프다).
        if self.heartbeat_due() and not self.spool.pending():
            self.send_heartbeat()

    def run(self) -> None:  # pragma: no cover - 스레드 진입점(본체는 drain_queue·tick)
        while not self._stop.is_set():
            self.drain_queue()
            self.tick()
        while self.drain_queue(wait=False) is not None:
            pass
