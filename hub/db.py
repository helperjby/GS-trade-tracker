"""육의전 시세 허브 — SQLite 영속 계층. 동기 호출.

스키마와 의미는 docs/HUB-PROTOCOL.md 가 정본. 배포된 DB 는 마이그레이션하지 않는다 —
테이블·인덱스는 ``CREATE … IF NOT EXISTS`` 로 additive 하게만 더한다.

네 표:
- ``market_observations`` — 관측기가 올린 페이지(프레임) 1개 = 1행. ``obs_id`` PK 가 재전송 dedup.
  ``payload_json`` 은 봉투 원문이되 **행은 정규화된 키를 뺀 미지 키만** 남긴다(``payload_for_storage``) —
  행 본문은 ``market_listings`` 에 있으니 두 번 쓰지 않는다(SD 카드 쓰기·30일 저장량 절반).
- ``market_listings`` — 등록(판매 건) 1개의 **최신 상태**. 같은 등록을 여러 번 보면 upsert.
- ``market_item_names`` — 관측기가 함께 보낸 아이템 id→이름 누적(이름을 못 푼 행을 검색 때 보충).
- ``devices`` — 초대 코드로 자기등록한 관측기 기기: 허브가 발급한 ``device_id`` 와 업로드 토큰의 sha256.
  토큰 원문은 저장하지 않는다(등록 응답 1회만). 제거(``revoked_ts``)는 다음 요청부터 바로 먹는다 — 서버는
  요청마다 이 표를 본다. prune 대상이 아니다. **"등록됨"의 정의는 하나** (``_ACTIVE_WHERE``): ``revoked_ts IS NULL``.
  업로드 여부·마지막 업로드 시각은 정원에 영향을 주지 않는다 — 명단은 관리자만 바꾼다(2026-09-22 사용자 결정:
  아는 사람 최대 7명에게 직접 배포, 활성 여부에 따른 자동 제외 없음). ``alias`` 는 관리자가 붙이는 별칭
  (``label`` 은 기기가 등록 때 스스로 적은 값이라 신뢰하지 않는다).

writer: market 표는 서버 이벤트루프 하나만 쓴다. ``devices`` 는 ``devices.py``(관리 CLI, 별도 프로세스)가 드물게
한 행 UPDATE 를 넣는다 — WAL + busy timeout(``Database(timeout=)``) 아래서 서버의 업로드 트랜잭션과 안전하게 교차하고,
최악은 서버 쪽 ``storage_error``(500) 한 번인데 관측기는 5xx 를 재시도한다.

시각 규칙: ``seen_ts = min(agent_ts, recv_ts)`` — 스풀에 묵었다가 늦게 올라온 관측은 관측 시각을,
관측기 시계가 서버보다 앞서면 서버 시각을 쓴다. 신선도 판정은 전부 ``seen_ts`` 계열(``last_seen_ts``).
``agent_ts`` 의 허용 범위(보존 기간 이전·하루 넘게 미래는 400)는 서버 검증 몫(server.validate_upload).

PRAGMA: ``journal_mode=WAL`` + ``synchronous=NORMAL`` — WAL 에서 문서화된 안전 설정(정전 때 마지막 트랜잭션
몇 개만 위험)이고 업로드는 at-least-once 재전송이라 잃어도 다시 온다. FULL 이면 POST 커밋마다 SD 카드
fsync 가 이벤트루프를 세워 봇의 search 가 밀린다.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_observations (
  obs_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
  recv_ts REAL NOT NULL, agent_ts REAL NOT NULL,
  seen_ts REAL NOT NULL,
  opcode INTEGER NOT NULL, page INTEGER, total_pages INTEGER, n_items INTEGER NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS idx_market_obs_recv ON market_observations(recv_ts DESC);
CREATE TABLE IF NOT EXISTS market_listings (
  listing_key TEXT PRIMARY KEY,
  listing_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
  item_name TEXT, item_name_norm TEXT,
  quantity INTEGER NOT NULL, price INTEGER NOT NULL, seller TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'item',
  flag45 INTEGER, flag46 INTEGER,
  first_seen_ts REAL NOT NULL, last_seen_ts REAL NOT NULL, seen_count INTEGER NOT NULL DEFAULT 1,
  last_device TEXT NOT NULL, last_obs_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_market_listings_norm ON market_listings(item_name_norm);
CREATE INDEX IF NOT EXISTS idx_market_listings_item ON market_listings(item_id);
CREATE INDEX IF NOT EXISTS idx_market_listings_seen_key ON market_listings(last_seen_ts, listing_key);
CREATE TABLE IF NOT EXISTS market_item_names (
  item_id INTEGER PRIMARY KEY, item_name TEXT NOT NULL, item_name_norm TEXT NOT NULL,
  first_seen_ts REAL NOT NULL, last_seen_ts REAL NOT NULL, last_device TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY,
  label TEXT NOT NULL DEFAULT '',
  alias TEXT NOT NULL DEFAULT '',
  token_hash TEXT NOT NULL UNIQUE,
  created_ts REAL NOT NULL, created_ip TEXT,
  last_seen_ts REAL, upload_count INTEGER NOT NULL DEFAULT 0,
  revoked_ts REAL, note TEXT NOT NULL DEFAULT '');
"""

DEFAULT_CATEGORY = "item"
#: 행에서 ``market_listings`` 로 정규화되는 키 — payload_json 에는 이 밖의(미지) 키만 남긴다.
ROW_NORMALIZED_KEYS = frozenset({"listing_id", "item_id", "item_name", "quantity", "price", "seller",
                                 "category", "flag45", "flag46"})
#: device_id 생성 시도 상한 — 40비트 난수라 충돌은 사실상 없지만 무한 루프는 두지 않는다.
DEVICE_ID_ATTEMPTS = 5
#: "등록된 기기"의 **한** 정의 — 정원 판정(server)·stats·CLI 가 전부 이 조건을 쓴다. 파라미터 없음.
#: 업로드 여부로 자동 제외하지 않는다(사용자 결정) — 명단에서 빼는 것은 관리자의 ``devices.py revoke`` 뿐.
_ACTIVE_WHERE = "revoked_ts IS NULL"


def norm_item_name(name) -> str:
    """검색 키 정규화 — **한 정의**: 공백 전부 제거 + ``casefold``.

    저장(``item_name_norm``)·검색(``q``)·봇 키워드(``_squash`` 는 공백 제거만이라 이 규칙의 부분집합)가
    같은 함수를 써야 한다. 표시명의 선두 ``[M]`` 제거는 관측기(item_names) 몫 — 여기서는 손대지 않는다.
    """
    return "".join(str(name or "").split()).casefold()


def listing_key(listing_id: int, item_id: int, seller: str) -> str:
    """등록 1건의 키 — 등록 id 만으로는 서버가 번호를 재사용할 때 다른 판매 건이 섞일 수 있어
    아이템 id·판매자를 붙인다(같은 등록의 수량 감소·재관측은 같은 키)."""
    return f"{listing_id}:{item_id}:{seller}"


def new_device_id() -> str:
    """허브가 발급하는 기기 id — ``d-`` + 10 hex. 사용자 PC 이름(display_name·hostname)은 여러 사용자가 같을 수
    있어 식별자로 못 쓴다; 표시용 ``label`` 로만 받는다."""
    return "d-" + secrets.token_hex(5)


def _clean_name(value) -> str | None:
    """관측기가 보낸 표시명 — 문자열이고 공백이 아닐 때만 채택, 아니면 None(미해석)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def payload_for_storage(obs: dict) -> dict:
    """``payload_json`` 에 남길 봉투 — 행은 정규화된 키(``ROW_NORMALIZED_KEYS``)를 빼고 미지 키만, 전부 비면
    ``rows`` 자체를 뺀다. tolerant reader 약속(미지 키 보존)은 지키면서 행 본문을 두 번 쓰지 않는다."""
    out = {k: v for k, v in obs.items() if k != "rows"}
    extras = [{k: v for k, v in row.items() if k not in ROW_NORMALIZED_KEYS} for row in obs.get("rows") or []]
    if any(extras):
        out["rows"] = extras
    return out


_LISTING_SELECT = """
SELECT l.listing_key, l.listing_id, l.item_id, COALESCE(l.item_name, n.item_name) AS item_name,
       l.quantity, l.price, l.seller, l.category, l.flag45, l.flag46,
       l.first_seen_ts, l.last_seen_ts, l.seen_count, l.last_device, l.last_obs_id
  FROM market_listings l LEFT JOIN market_item_names n ON n.item_id = l.item_id
"""

#: DO UPDATE 안에서 무수식 열은 기존 행, ``excluded.`` 는 새 관측. 이름도 "더 나중에 본 관측이 쓴다" —
#: 새 이름이 NULL 이면 덮지 않고, 기존이 NULL 이면 채우고, 둘 다 있으면 seen_ts 가 같거나 큰 쪽이 이긴다.
_LISTING_UPSERT = """
INSERT INTO market_listings
  (listing_key, listing_id, item_id, item_name, item_name_norm, quantity, price,
   seller, category, flag45, flag46, first_seen_ts, last_seen_ts, seen_count,
   last_device, last_obs_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
ON CONFLICT(listing_key) DO UPDATE SET
  quantity    = CASE WHEN excluded.last_seen_ts >= last_seen_ts THEN excluded.quantity    ELSE quantity    END,
  price       = CASE WHEN excluded.last_seen_ts >= last_seen_ts THEN excluded.price       ELSE price       END,
  category    = CASE WHEN excluded.last_seen_ts >= last_seen_ts THEN excluded.category    ELSE category    END,
  flag45      = CASE WHEN excluded.last_seen_ts >= last_seen_ts THEN excluded.flag45      ELSE flag45      END,
  flag46      = CASE WHEN excluded.last_seen_ts >= last_seen_ts THEN excluded.flag46      ELSE flag46      END,
  last_device = CASE WHEN excluded.last_seen_ts >= last_seen_ts THEN excluded.last_device ELSE last_device END,
  last_obs_id = CASE WHEN excluded.last_seen_ts >= last_seen_ts THEN excluded.last_obs_id ELSE last_obs_id END,
  item_name      = CASE WHEN excluded.item_name IS NOT NULL
                             AND (item_name IS NULL OR excluded.last_seen_ts >= last_seen_ts)
                        THEN excluded.item_name ELSE item_name END,
  item_name_norm = CASE WHEN excluded.item_name_norm IS NOT NULL
                             AND (item_name_norm IS NULL OR excluded.last_seen_ts >= last_seen_ts)
                        THEN excluded.item_name_norm ELSE item_name_norm END,
  first_seen_ts  = MIN(first_seen_ts, excluded.first_seen_ts),
  last_seen_ts   = MAX(last_seen_ts, excluded.last_seen_ts),
  seen_count     = seen_count + 1
"""

_NAME_UPSERT = """
INSERT INTO market_item_names
  (item_id, item_name, item_name_norm, first_seen_ts, last_seen_ts, last_device)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(item_id) DO UPDATE SET
  item_name      = CASE WHEN excluded.last_seen_ts >= last_seen_ts THEN excluded.item_name      ELSE item_name      END,
  item_name_norm = CASE WHEN excluded.last_seen_ts >= last_seen_ts THEN excluded.item_name_norm ELSE item_name_norm END,
  last_device    = CASE WHEN excluded.last_seen_ts >= last_seen_ts THEN excluded.last_device    ELSE last_device    END,
  first_seen_ts  = MIN(first_seen_ts, excluded.first_seen_ts),
  last_seen_ts   = MAX(last_seen_ts, excluded.last_seen_ts)
"""

_DEVICE_COLUMNS = ("device_id, label, alias, created_ts, created_ip, last_seen_ts, upload_count, "
                   "revoked_ts, note")


class Database:
    def __init__(self, path: str, timeout: float = 5.0) -> None:
        """``timeout`` = sqlite busy timeout(초) — 다른 프로세스(devices.py)가 잠근 동안 기다리는 시간."""
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self._con = sqlite3.connect(path, timeout=float(timeout))
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.executescript(SCHEMA)
        self._con.commit()

    def close(self) -> None:
        self._con.close()

    @contextlib.contextmanager
    def _tx(self):
        """쓰기 한 덩이 — 성공하면 commit, 실패하면 **반드시 rollback** 한 뒤 다시 던진다.

        롤백을 빠뜨리면 실패한 문장이 연 암묵 트랜잭션이 이 연결에 그대로 남는다. 그 뒤로 이 연결은
        낡은 WAL 스냅샷을 붙들어 다른 프로세스(``devices.py``)가 커밋한 제거를 못 보고, 반대로 이 연결이
        쓰기 락을 쥐고 있으면 CLI 쪽이 ``database is locked`` 로 죽는다 — 지인 한 명의 불안정한 업로드가
        나머지 전원을 멈추게 하는 경로라 모든 쓰기를 여기로 통과시킨다.
        """
        try:
            yield self._con
            self._con.commit()
        except Exception:
            self._con.rollback()
            raise

    # ---- 수집 (POST /api/market/observations) ----

    def insert_market_observations(self, device_id: str, observations: list, recv_ts: float,
                                   seen_device: str | None = None) -> tuple:
        """검증이 끝난 관측 목록을 **트랜잭션 1개**로 반영. 반환 (신규 관측 수, 중복 수, 반영 행 수).

        관측마다 ``INSERT OR IGNORE`` — ``obs_id`` 가 이미 있으면(스풀 재전송) 중복으로 세고 행 upsert 를
        건너뛴다(``seen_count`` 이중 가산 방지). 행 upsert 는 "더 나중에 본 관측이 상태를 쓴다":
        ``last_seen_ts`` 가 기존보다 같거나 크면 quantity/price/플래그/기기/이름을 갱신하고, 순서가 뒤바뀌어
        올라온 옛 관측은 first_seen_ts 만 앞당긴다(빈 이름은 채운다). NULL 이름은 있는 이름을 덮지 않는다.
        ``seen_device`` 가 있으면(기기 토큰 업로드) 같은 트랜잭션에서 그 기기의 ``last_seen_ts``·``upload_count`` 도
        쓴다 — 커밋 1회, 실패하면 기기 기록도 남지 않는다.
        """
        accepted = duplicates = rows_applied = 0
        with self._tx() as con:          # 실패하면 관측·행·기기 기록 전부 롤백 — 커밋 1회
            for obs in observations:
                agent_ts = float(obs["agent_ts"])
                seen_ts = min(agent_ts, recv_ts)
                rows = obs.get("rows") or []
                cur = con.execute(
                    """INSERT OR IGNORE INTO market_observations
                         (obs_id, device_id, recv_ts, agent_ts, seen_ts, opcode, page, total_pages,
                          n_items, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (obs["obs_id"], device_id, recv_ts, agent_ts, seen_ts, int(obs["opcode"]),
                     obs.get("page"), obs.get("total_pages"), len(rows),
                     json.dumps(payload_for_storage(obs), ensure_ascii=False)))
                if cur.rowcount != 1:
                    duplicates += 1
                    continue
                accepted += 1
                for row in rows:
                    name = _clean_name(row.get("item_name"))
                    norm = norm_item_name(name) if name else None
                    category = row.get("category")
                    if not isinstance(category, str) or not category:
                        category = DEFAULT_CATEGORY
                    listing_id, item_id = int(row["listing_id"]), int(row["item_id"])
                    seller = str(row["seller"])
                    con.execute(_LISTING_UPSERT,
                                (listing_key(listing_id, item_id, seller), listing_id, item_id, name, norm,
                                 int(row["quantity"]), int(row["price"]), seller, category,
                                 row.get("flag45"), row.get("flag46"), seen_ts, seen_ts, device_id,
                                 obs["obs_id"]))
                    rows_applied += 1
                    if name:
                        con.execute(_NAME_UPSERT, (item_id, name, norm, seen_ts, seen_ts, device_id))
            if seen_device is not None:
                con.execute("UPDATE devices SET last_seen_ts = ?, upload_count = upload_count + 1 WHERE device_id = ?",
                            (recv_ts, seen_device))
        return accepted, duplicates, rows_applied

    # ---- 기기 (POST /api/market/register · 업로드 인증 · devices.py) ----

    def create_device(self, label: str, token_hash: str, now: float, ip: str | None) -> str:
        """기기 행 생성 → 발급한 ``device_id``. PK 충돌이면 id 를 다시 만든다(``DEVICE_ID_ATTEMPTS`` 회).
        ``token_hash`` UNIQUE 충돌(32바이트 난수라 사실상 불가)은 재생성으로 못 풀어 IntegrityError 로 나간다."""
        last_error: sqlite3.IntegrityError | None = None
        for _ in range(DEVICE_ID_ATTEMPTS):
            device_id = new_device_id()
            try:
                self._con.execute(
                    "INSERT INTO devices (device_id, label, token_hash, created_ts, created_ip) VALUES (?, ?, ?, ?, ?)",
                    (device_id, label, token_hash, now, ip))
            except sqlite3.IntegrityError as e:
                self._con.rollback()
                last_error = e
                continue
            self._con.commit()
            return device_id
        raise sqlite3.IntegrityError(f"devices 삽입 {DEVICE_ID_ATTEMPTS}회 충돌") from last_error

    def get_device_by_token_hash(self, token_hash: str) -> dict | None:
        r = self._con.execute(
            "SELECT device_id, label, alias, revoked_ts, upload_count, last_seen_ts FROM devices WHERE token_hash = ?",
            (token_hash,)).fetchone()
        return dict(r) if r is not None else None

    def get_device(self, device_id: str) -> dict | None:
        """관리용 — 토큰 해시는 싣지 않는다."""
        r = self._con.execute(f"SELECT {_DEVICE_COLUMNS} FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        return dict(r) if r is not None else None

    def count_active_devices(self) -> int:
        """등록된(미제거) 기기 수(``_ACTIVE_WHERE``) — 등록 정원(``max_devices``) 판정용. 업로드를 한 번도 안 한
        기기도 자리를 차지한다(자동 제외 없음) — 자리를 비우는 것은 관리자의 ``devices.py revoke`` 뿐."""
        return int(self._con.execute(f"SELECT COUNT(*) FROM devices WHERE {_ACTIVE_WHERE}").fetchone()[0])

    def touch_device(self, device_id: str, now: float) -> None:
        """인증은 됐지만 거부된 업로드(400·403) — ``last_seen_ts`` 만(운영자가 잘못 설정된 exe 를 찾을 수 있게)."""
        with self._tx() as con:
            con.execute("UPDATE devices SET last_seen_ts = ? WHERE device_id = ?", (now, device_id))

    def list_devices(self) -> list[dict]:
        """토큰 해시는 싣지 않는다(CLI 출력·로그로 새지 않게)."""
        return [dict(r) for r in self._con.execute(
            f"SELECT {_DEVICE_COLUMNS} FROM devices ORDER BY created_ts, device_id").fetchall()]

    def set_device_revoked(self, device_id: str, revoked_ts: float | None) -> bool:
        """제거(시각) 또는 복구(None) — 제거하면 정원 자리가 바로 빈다. 반환 = 그런 기기가 있었는지."""
        with self._tx() as con:
            cur = con.execute("UPDATE devices SET revoked_ts = ? WHERE device_id = ?", (revoked_ts, device_id))
        return cur.rowcount == 1

    def set_device_alias(self, device_id: str, alias: str) -> bool:
        """관리자가 붙이는 별칭 — 기기가 스스로 적은 ``label`` 은 그대로 두고 표시만 이 값을 앞세운다.
        빈 문자열이면 별칭 해제. 반환 = 그런 기기가 있었는지."""
        with self._tx() as con:
            cur = con.execute("UPDATE devices SET alias = ? WHERE device_id = ?", (alias, device_id))
        return cur.rowcount == 1

    def set_device_note(self, device_id: str, note: str) -> bool:
        with self._tx() as con:
            cur = con.execute("UPDATE devices SET note = ? WHERE device_id = ?", (note, device_id))
        return cur.rowcount == 1

    # ---- 조회 (GET /api/market/*) ----

    @staticmethod
    def _listing_dict(r) -> dict:
        return {"listing_key": r["listing_key"], "listing_id": r["listing_id"], "item_id": r["item_id"],
                "item_name": r["item_name"], "quantity": r["quantity"], "price": r["price"],
                "seller": r["seller"], "category": r["category"], "flag45": r["flag45"], "flag46": r["flag46"],
                "first_seen_ts": r["first_seen_ts"], "last_seen_ts": r["last_seen_ts"],
                "seen_count": r["seen_count"], "last_device": r["last_device"]}

    def search_market(self, q_norm: str, item_id, limit: int, min_seen_ts: float) -> tuple:
        """이름 부분일치(정규화 키 ``instr``)·아이템 id 로 목록 검색. 반환 (신선한 행 목록, 신선도 무관 매칭 수).

        이름은 행 자체의 ``item_name`` 이 NULL 이면 학습 표의 이름으로 보충해 매칭·표시한다.
        정렬은 단가 오름차순 → 최근 관측 우선 → 키(결정적).
        """
        where, params = [], []
        if q_norm:
            where.append("instr(COALESCE(l.item_name_norm, n.item_name_norm, ''), ?) > 0")
            params.append(q_norm)
        if item_id is not None:
            where.append("l.item_id = ?")
            params.append(int(item_id))
        if not where:
            return [], 0
        cond = " WHERE " + " AND ".join(where)
        total = self._con.execute(
            "SELECT COUNT(*) AS c FROM market_listings l LEFT JOIN market_item_names n "
            "ON n.item_id = l.item_id" + cond, params).fetchone()["c"]
        rows = self._con.execute(
            _LISTING_SELECT + cond + " AND l.last_seen_ts >= ?"
            " ORDER BY l.price ASC, l.last_seen_ts DESC, l.listing_key LIMIT ?",
            params + [float(min_seen_ts), int(limit)]).fetchall()
        return [self._listing_dict(r) for r in rows], int(total)

    def list_market_since(self, since_ts: float, since_key: str, limit: int) -> list:
        """복합 keyset 커서 ``(last_seen_ts, listing_key) > (since_ts, since_key)`` 인 행을 오래된 순으로 —
        소비자의 증분 폴링용. ``last_seen_ts`` 하나만 엄격 초과로 보면 같은 시각의 행(한 페이지의 행은 전부
        같은 ``seen_ts``)이 limit 를 넘길 때 나머지가 영구 누락된다. ``since_key`` 가 빈 문자열이면 ``since_ts``
        와 같은 시각의 행을 전부 포함한다(옛 소비자 = at-least-once)."""
        rows = self._con.execute(
            _LISTING_SELECT + " WHERE l.last_seen_ts > ? OR (l.last_seen_ts = ? AND l.listing_key > ?)"
            " ORDER BY l.last_seen_ts ASC, l.listing_key ASC LIMIT ?",
            (float(since_ts), float(since_ts), str(since_key), int(limit))).fetchall()
        return [self._listing_dict(r) for r in rows]

    def market_stats(self, now: float, fresh_sec: float = 86400.0) -> dict:
        con = self._con

        def one(sql: str, *params):
            return con.execute(sql, params).fetchone()[0]

        # 관측을 올린 device_id 별 집계 + 등록 기기면 별칭·label(관리 시크릿으로 올린 자유 device_id 는 null).
        devices = [{"device_id": r["device_id"], "label": r["label"], "alias": r["alias"],
                    "observations": r["c"], "last_recv_ts": r["m"]}
                   for r in con.execute(
                       "SELECT o.device_id, d.label AS label, d.alias AS alias, COUNT(*) AS c, "
                       "MAX(o.recv_ts) AS m "
                       "FROM market_observations o LEFT JOIN devices d ON d.device_id = o.device_id "
                       "GROUP BY o.device_id ORDER BY o.device_id").fetchall()]
        return {
            "observations": one("SELECT COUNT(*) FROM market_observations"),
            "listings": one("SELECT COUNT(*) FROM market_listings"),
            "items": one("SELECT COUNT(DISTINCT item_id) FROM market_listings"),
            "item_names": one("SELECT COUNT(*) FROM market_item_names"),
            "fresh_listings": one("SELECT COUNT(*) FROM market_listings WHERE last_seen_ts >= ?",
                                  now - float(fresh_sec)),
            "fresh_sec": float(fresh_sec),
            "latest_recv_ts": one("SELECT MAX(recv_ts) FROM market_observations"),
            "devices": devices,
            # 등록 = 정원을 차지하는 수(미제거). 자동 제외가 없으니 "활성"과 같은 수라 따로 싣지 않는다.
            "devices_registered": self.count_active_devices(),
            "devices_revoked": one("SELECT COUNT(*) FROM devices WHERE revoked_ts IS NOT NULL"),
        }

    # ---- 보존 ----

    def prune(self, now: float, market_days: int = 30) -> tuple:
        """보존 기간 초과분 삭제 — 관측은 ``recv_ts``, 목록은 ``last_seen_ts`` 가 cutoff **미만**인 행만
        (경계값 보존). 학습 표(``market_item_names``)와 기기 표(``devices``)는 작고 계속 쓸모 있어 지우지 않는다.
        반환 (삭제된 관측 수, 삭제된 목록 행 수).
        """
        cutoff = now - int(market_days) * 86400
        # 두 DELETE 는 한 트랜잭션 — 두 번째가 잠금으로 실패해도 첫 번째가 커밋 대기로 남아 24시간짜리
        # 쓰기 락이 되지 않는다(_retention_loop 는 예외를 삼키고 하루를 잔다).
        with self._tx() as con:
            cur_o = con.execute("DELETE FROM market_observations WHERE recv_ts < ?", (cutoff,))
            cur_l = con.execute("DELETE FROM market_listings WHERE last_seen_ts < ?", (cutoff,))
        return cur_o.rowcount, cur_l.rowcount
