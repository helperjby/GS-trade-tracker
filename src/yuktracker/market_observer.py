"""엔진 `market_cb` → 업로드 봉투. 스니퍼 스레드에서 도는 코드라 **가볍고 절대 던지지 않는다**.

여기서 하는 일은 셋뿐이다: 행에 아이템 이름을 붙이고, 봉투를 만들고, 큐에 넣는다.
파일 쓰기·네트워크는 업로더 스레드 몫이다(`spool.py`).

시각 — 엔진의 `now_fn` 은 `time.monotonic` 이라 `observation.ts` 는 **단조시계**다. 허브 계약의
`agent_ts` 는 epoch 초(float)이므로 콜백 시점의 두 시계 차이로 되돌린다:
`wall = time.time() - (time.monotonic() - observation.ts)`. 스풀에 묵었다 늦게 올라가도 "그때 본 목록"의
시각이 남는다(허브는 `seen_ts = min(agent_ts, recv_ts)`).

`obs_id` = `{local_id}:{agent_ts_ms}:{sha1(body)[:12]}` — `local_id` 는 설치본 고유 id 이고 허브가 발급한
`device_id` 와 **별개**다. 재등록으로 `device_id` 가 바뀌어도 스풀에 남아 있던 관측의 id 가 흔들리지
않는다(허브의 dedup 은 `obs_id` 하나로 한다).
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Callable, Optional

from .item_names import ItemTable, load_item_table_result
from .seassist.logger import get_logger
from .seassist.packet_market import row_to_dict

#: 아이템 표 재검사 주기 — 클라 패치를 따라간다(`os.stat` 한 번이라 싸다).
TABLE_RECHECK_SEC = 600.0


class ItemNames:
    """아이템 표 한 개를 들고 있다가 클라가 패치되면 갈아끼운다.

    참조 교체 한 번(원자적)이라 스니퍼 스레드가 읽는 중에 업로더 스레드가 바꿔도 안전하다 —
    읽는 쪽은 언제나 완전한 표 하나를 본다(반쪽 표가 존재하지 않는다).
    """

    def __init__(self, table: Optional[ItemTable] = None, gcs_path: Optional[Path] = None,
                 *, clock: Callable[[], float] = time.monotonic) -> None:
        self.table = table
        self.gcs_path = Path(gcs_path) if gcs_path else None
        self._clock = clock
        self._checked = clock()

    @classmethod
    def load(cls, *, client_dir: str = "", clock: Callable[[], float] = time.monotonic) -> "ItemNames":
        """시작 시 1회. 실패해도 예외를 내지 않는다 — 이름 없이(전부 null) 관측은 계속한다."""
        result = load_item_table_result(client_dir=client_dir or None)
        if result.table is None:
            get_logger().warning("아이템 표를 읽지 못했습니다(%s) — 이름 없이 관측합니다: %s",
                                 result.origin, result.error or "클라이언트를 찾지 못함")
        return cls(result.table, result.gcs_path, clock=clock)

    @property
    def rows(self) -> int:
        return len(self.table) if self.table is not None else 0

    def lookup(self, item_id: int) -> Optional[str]:
        t = self.table
        return t.lookup(item_id) if t is not None else None

    def envelope(self) -> Optional[dict]:
        """업로드 봉투의 `item_table` — 어느 빌드의 표로 이름을 붙였는지 허브가 되짚을 수 있게."""
        t = self.table
        if t is None:
            return None
        return {"gcs_sha256": t.source.gcs_sha256, "rows": t.source.rows,
                "archive_ts": t.source.archive_ts}

    def recheck(self, *, interval: float = TABLE_RECHECK_SEC) -> bool:
        """주기적 `os.stat` — 크기·mtime 이 달라졌으면 다시 읽는다(클라 패치 추종). 바뀌었으면 True."""
        now = self._clock()
        if now - self._checked < interval:
            return False
        self._checked = now
        t, p = self.table, self.gcs_path
        if t is None or p is None:
            return False
        try:
            st = os.stat(p)
        except OSError:
            return False
        if (st.st_size, st.st_mtime_ns) == (t.source.gcs_size, t.source.gcs_mtime_ns):
            return False
        result = load_item_table_result(gcs_path=p)
        if result.table is None:
            get_logger().warning("아이템 표 재읽기 실패 — 이전 표를 계속 씁니다: %s", result.error)
            return False
        self.table = result.table
        return True


class MarketObserver:
    """엔진 콜백 본체 — `market_cb(slot_idx, page, observation, *, pid)`."""

    def __init__(self, local_id: str, names: ItemNames, *,
                 enqueue: Optional[Callable[[dict], None]] = None,
                 say: Optional[Callable[[str], None]] = None,
                 wall: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.local_id = local_id
        self.names = names
        self._enqueue = enqueue
        self._say = say
        self._wall = wall
        self._mono = mono
        self.pages = 0
        self.rows = 0
        self.unknown_item = 0

    # ── 봉투 ────────────────────────────────────────────────────────────
    def agent_ts(self, observation) -> float:
        """단조시계 관측 시각 → epoch 초. 두 시계를 **같은 순간에** 읽어 차이를 되돌린다."""
        return self._wall() - (self._mono() - float(observation.ts))

    def obs_id(self, body: bytes, agent_ts: float) -> str:
        digest = hashlib.sha1(body).hexdigest()[:12]
        return f"{self.local_id}:{int(agent_ts * 1000)}:{digest}"

    def envelope(self, page, observation) -> dict:
        agent_ts = self.agent_ts(observation)
        rows = []
        for row in page.rows:
            d = row_to_dict(row)
            name = self.names.lookup(row.item_id)
            if name is None:
                self.unknown_item += 1
            d["item_name"] = name
            rows.append(d)
        env = {
            "obs_id": self.obs_id(observation.body, agent_ts),
            "agent_ts": agent_ts,
            "opcode": int(page.opcode),
            "page": None,          # 응답에 페이지 번호가 없다(H-2609-08) — 요청에만 있다
            "total_pages": int(page.total_pages),
            "hdr4": int(page.hdr4),
            "anomalies": list(page.anomalies),
            "rows": rows,
        }
        table = self.names.envelope()
        if table is not None:
            env["item_table"] = table
        return env

    # ── 콜백 ────────────────────────────────────────────────────────────
    def __call__(self, slot_idx: int, page, observation, *, pid: int = 0) -> None:
        """엔진이 예외를 삼키고 세지만(`market_callback_errors`), 여기서도 스스로 막는다 —
        스니퍼 스레드에서 새어 나가면 그 창의 패킷 커버리지를 통째로 잃는다."""
        try:
            env = self.envelope(page, observation)
            self.pages += 1
            self.rows += page.count
            if self._say is not None:
                extra = f" ⚠{','.join(page.anomalies)}" if page.anomalies else ""
                self._say(f"[육의전] 클라{slot_idx + 1} 목록 {page.count}행 / 총 {page.total_pages}쪽{extra}")
            if self._enqueue is not None:
                self._enqueue(env)
        except Exception:
            get_logger().exception("육의전 관측 처리 실패 — 이 페이지는 버린다")
