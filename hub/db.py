"""육의전 시세 허브 — SQLite 영속 계층. 단일 writer(서버 이벤트루프 스레드) 전용, 동기 호출.

스키마와 의미는 docs/HUB-PROTOCOL.md 가 정본. 배포된 DB 는 마이그레이션하지 않는다 —
테이블·인덱스는 ``CREATE … IF NOT EXISTS`` 로 additive 하게만 더한다.

세 표:
- ``market_observations`` — 관측기가 올린 페이지(프레임) 1개 = 1행. ``obs_id`` PK 가 재전송 dedup.
- ``market_listings`` — 등록(판매 건) 1개의 **최신 상태**. 같은 등록을 여러 번 보면 upsert.
- ``market_item_names`` — 관측기가 함께 보낸 아이템 id→이름 누적(이름을 못 푼 행을 검색 때 보충).

시각 규칙: ``seen_ts = min(agent_ts, recv_ts)`` — 스풀에 묵었다가 늦게 올라온 관측은 관측 시각을,
관측기 시계가 서버보다 앞서면 서버 시각을 쓴다. 신선도 판정은 전부 ``seen_ts`` 계열(``last_seen_ts``).
"""
from __future__ import annotations

import json
import os
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
CREATE INDEX IF NOT EXISTS idx_market_listings_seen ON market_listings(last_seen_ts);
CREATE TABLE IF NOT EXISTS market_item_names (
  item_id INTEGER PRIMARY KEY, item_name TEXT NOT NULL, item_name_norm TEXT NOT NULL,
  first_seen_ts REAL NOT NULL, last_seen_ts REAL NOT NULL, last_device TEXT NOT NULL);
"""

DEFAULT_CATEGORY = "item"


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


def _clean_name(value) -> str | None:
    """관측기가 보낸 표시명 — 문자열이고 공백이 아닐 때만 채택, 아니면 None(미해석)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


_LISTING_SELECT = """
SELECT l.listing_id, l.item_id, COALESCE(l.item_name, n.item_name) AS item_name,
       l.quantity, l.price, l.seller, l.category, l.flag45, l.flag46,
       l.first_seen_ts, l.last_seen_ts, l.seen_count, l.last_device, l.last_obs_id
  FROM market_listings l LEFT JOIN market_item_names n ON n.item_id = l.item_id
"""

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
  item_name      = COALESCE(excluded.item_name, item_name),
  item_name_norm = COALESCE(excluded.item_name_norm, item_name_norm),
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


class Database:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self._con = sqlite3.connect(path)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(SCHEMA)
        self._con.commit()

    def close(self) -> None:
        self._con.close()

    # ---- 수집 (POST /api/market/observations) ----

    def insert_market_observations(self, device_id: str, observations: list, recv_ts: float) -> tuple:
        """검증이 끝난 관측 목록을 **트랜잭션 1개**로 반영. 반환 (신규 관측 수, 중복 수, 반영 행 수).

        관측마다 ``INSERT OR IGNORE`` — ``obs_id`` 가 이미 있으면(스풀 재전송) 중복으로 세고 행 upsert 를
        건너뛴다(``seen_count`` 이중 가산 방지). 행 upsert 는 "더 나중에 본 관측이 상태를 쓴다":
        ``last_seen_ts`` 가 기존보다 같거나 크면 quantity/price/플래그/기기를 갱신하고, 순서가 뒤바뀌어
        올라온 옛 관측은 first_seen_ts 만 앞당긴다. 이름은 NULL 이 아닌 값을 우선 보존한다.
        """
        con = self._con
        accepted = duplicates = rows_applied = 0
        try:
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
                     json.dumps(obs, ensure_ascii=False)))
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
            con.commit()
        except Exception:
            con.rollback()
            raise
        return accepted, duplicates, rows_applied

    # ---- 조회 (GET /api/market/*) ----

    @staticmethod
    def _listing_dict(r) -> dict:
        return {"listing_id": r["listing_id"], "item_id": r["item_id"], "item_name": r["item_name"],
                "quantity": r["quantity"], "price": r["price"], "seller": r["seller"],
                "category": r["category"], "flag45": r["flag45"], "flag46": r["flag46"],
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

    def list_market_since(self, since_ts: float, limit: int) -> list:
        """``last_seen_ts > since_ts`` 인 행을 오래된 순으로 — 소비자의 증분 폴링용(엄격 초과라 이어 읽기 안전)."""
        rows = self._con.execute(
            _LISTING_SELECT + " WHERE l.last_seen_ts > ? ORDER BY l.last_seen_ts ASC, l.listing_key LIMIT ?",
            (float(since_ts), int(limit))).fetchall()
        return [self._listing_dict(r) for r in rows]

    def market_stats(self, now: float, fresh_sec: float = 86400.0) -> dict:
        con = self._con

        def one(sql: str, *params):
            return con.execute(sql, params).fetchone()[0]

        devices = [{"device_id": r["device_id"], "observations": r["c"], "last_recv_ts": r["m"]}
                   for r in con.execute(
                       "SELECT device_id, COUNT(*) AS c, MAX(recv_ts) AS m FROM market_observations "
                       "GROUP BY device_id ORDER BY device_id").fetchall()]
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
        }

    # ---- 보존 ----

    def prune(self, now: float, market_days: int = 30) -> tuple:
        """보존 기간 초과분 삭제 — 관측은 ``recv_ts``, 목록은 ``last_seen_ts`` 가 cutoff **미만**인 행만
        (경계값 보존). 학습 표(``market_item_names``)는 작고 계속 쓸모 있어 지우지 않는다.
        반환 (삭제된 관측 수, 삭제된 목록 행 수).
        """
        cutoff = now - int(market_days) * 86400
        cur_o = self._con.execute("DELETE FROM market_observations WHERE recv_ts < ?", (cutoff,))
        cur_l = self._con.execute("DELETE FROM market_listings WHERE last_seen_ts < ?", (cutoff,))
        self._con.commit()
        return cur_o.rowcount, cur_l.rowcount
