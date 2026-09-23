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
  토큰 원문은 저장하지 않는다(등록 응답 1회만). ``slot`` 은 등록에 쓴 초대 코드의 이름(설정 ``invite_codes`` 의 키) —
  같은 슬롯으로 다시 등록하면 새 기기가 슬롯을 받고 옛 기기는 같은 트랜잭션에서 제거된다(``register_slot_device``,
  2026-09-23 사용자 결정: 재설치에 관리자 개입 없음). 제거(``revoked_ts``)는 다음 요청부터 바로 먹는다 — 서버는
  요청마다 이 표를 본다. prune 대상이 아니다. **"등록됨"의 정의는 하나** (``_ACTIVE_WHERE``): ``revoked_ts IS NULL``.
  업로드 여부·마지막 업로드 시각은 정원에 영향을 주지 않는다 — 명단은 관리자만 바꾼다(2026-09-22 사용자 결정:
  아는 사람 최대 7명에게 직접 배포, 활성 여부에 따른 자동 제외 없음). ``alias`` 는 관리자가 붙이는 별칭
  (``label`` 은 기기가 등록 때 스스로 적은 값이라 신뢰하지 않는다).

writer: market 표는 서버 이벤트루프 하나만 쓴다. ``devices`` 는 ``devices.py``(관리 CLI, 별도 프로세스)가 드물게
한 행 UPDATE 를 넣는다 — WAL + busy timeout(``Database(timeout=)``) 아래서 서버의 업로드 트랜잭션과 안전하게 교차하고,
최악은 서버 쪽 ``storage_error``(500) 한 번인데 관측기는 5xx 를 재시도한다.

시각 규칙: ``seen_ts = min(agent_ts, recv_ts)`` — 스풀에 묵었다가 늦게 올라온 관측은 관측 시각을,
관측기 시계가 서버보다 앞서면 서버 시각을 쓴다. 신선도 판정은 전부 ``seen_ts`` 계열(``last_seen_ts``) —
소멸 추정(``expires_from_ts``·``expires_by_ts``)도 first/last_seen 에서 조회 때 계산한다(``kst_midnight``, 저장 안 함).
``agent_ts`` 의 허용 범위(보존 기간 이전·하루 넘게 미래는 400)는 서버 검증 몫(server.validate_upload).

PRAGMA: ``journal_mode=WAL`` + ``synchronous=NORMAL`` — WAL 에서 문서화된 안전 설정(정전 때 마지막 트랜잭션
몇 개만 위험)이고 업로드는 at-least-once 재전송이라 잃어도 다시 온다. FULL 이면 POST 커밋마다 SD 카드
fsync 가 이벤트루프를 세워 봇의 search 가 밀린다.
"""
from __future__ import annotations

import contextlib
import json
import math
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
  slot TEXT NOT NULL DEFAULT '',
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


#: 육의전 소멸 규칙(게임 사실, 2026-09-23 사용자): 등록일 D(KST)에 올린 물품은 팔리지 않으면 단기 = D+2 00:00,
#: 장기 = D+3 00:00 에 목록에서 사라진다. 등록 시각은 패킷에 없고(PACKET-MARKET §1) 관측 시각만 있으므로
#: 관측 T 에 살아 있었다 → 등록일 ∈ {date(T)−1, date(T)} 로부터 두 경계를 낸다(HUB-PROTOCOL §4):
#:   expires_from_ts = date(last_seen)+1 00:00                          — 이 시각부터 사라졌을 **수** 있다(하한)
#:   expires_by_ts   = max(date(first_seen)+N 00:00, expires_from_ts)   — 이 시각 뒤는 없다(상한)
#: 상한에 하한을 합치는 이유: 마지막 관측 때 살아 있었으므로 그 날 자정 전엔 사라질 수 없다 — 시계가 며칠 느린 기기가 first_seen 을
#: 과거로 끌어내려도(MIN 은 되돌아오지 않는다) 다른 기기가 계속 보는 행이 숨지 않고, from > by 도 생기지 않는다.
#: N = ``listing_expiry_days``. `기간`(@45) 이 검정 전(H-2609-11, D)이라 모든 행을 단기(2)로 본다(2026-09-23 사용자 결정) —
#: 장기 물품은 D+2 하루 동안 재관측이 없으면 하루 일찍 숨는다(수용). 검정 뒤 행별 일수는 후속.
#: KST 는 DST 가 없어 고정 오프셋(zoneinfo·tzdata 불필요 — Windows 테스트 환경에 tz 데이터가 없다).
KST_OFFSET_SEC = 9 * 3600
DEFAULT_LISTING_EXPIRY_DAYS = 2


def kst_midnight(ts: float, plus_days: int) -> float:
    """``ts``(epoch 초)가 속한 KST 날짜의 자정 + ``plus_days`` 일, epoch 초로."""
    return float((math.floor((float(ts) + KST_OFFSET_SEC) / 86400) + int(plus_days)) * 86400 - KST_OFFSET_SEC)


def expiry_bounds(first_seen_ts: float, last_seen_ts: float, days: int) -> tuple[float, float]:
    """(expires_from_ts, expires_by_ts) — 위 정의. 항상 from ≤ by."""
    frm = kst_midnight(last_seen_ts, 1)
    return frm, max(kst_midnight(first_seen_ts, days), frm)


#: ``expires_by_ts > now`` 를 열 그대로 비교하는 sargable 필터(SQL 에 날짜 산술 없음 — 정의는 파이썬 하나):
#:   kst_midnight(first, N) > now  ⟺  first ≥ kst_midnight(now, 1−N)   (N 정수, floor 산술)
#:   kst_midnight(last, 1)  > now  ⟺  last  ≥ kst_midnight(now, 0)
_LIVE_WHERE = "(l.first_seen_ts >= ? OR l.last_seen_ts >= ?)"


def _live_params(now: float, days: int) -> list:
    return [kst_midnight(now, 1 - int(days)), kst_midnight(now, 0)]


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

_DEVICE_COLUMNS = ("device_id, label, alias, slot, created_ts, created_ip, last_seen_ts, upload_count, "
                   "revoked_ts, note")
#: 배포 DB 에 additive 로 더한 열 — ``CREATE TABLE IF NOT EXISTS`` 는 이미 있는 표에 열을 못 더하므로 기동 때 없는 열만
#: ``ALTER TABLE … ADD COLUMN`` 한다(DEFAULT 가 있어 기존 행은 그대로). 열을 지우거나 바꾸는 마이그레이션은 여전히 없다.
_DEVICE_ADDED_COLUMNS = (("slot", "TEXT NOT NULL DEFAULT ''"),)


class Database:
    def __init__(self, path: str, timeout: float = 5.0, *,
                 listing_expiry_days: int = DEFAULT_LISTING_EXPIRY_DAYS) -> None:
        """``timeout`` = sqlite busy timeout(초) — 다른 프로세스(devices.py)가 잠근 동안 기다리는 시간.
        ``listing_expiry_days`` = 등록일 자정 기준 소멸 일수(설정 ``listing_expiry_days``, 조회·집계에만 쓴다 — 저장 무관)."""
        if isinstance(listing_expiry_days, bool) or not isinstance(listing_expiry_days, int) or listing_expiry_days < 1:
            raise ValueError("listing_expiry_days 는 1 이상의 정수")
        self._expiry_days = listing_expiry_days
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self._con = sqlite3.connect(path, timeout=float(timeout))
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.executescript(SCHEMA)
        self._con.commit()
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        # 먼저 PRAGMA 로 보고 ALTER 하면 서버와 devices.py 가 같은 순간 처음 열 때 둘 다 '없음'을 보고 진 쪽이 죽는다 —
        # 검사 없이 ALTER 를 시도하고 '이미 있음'만 삼키면 경쟁 창이 없다(열 7개 표라 비용 없음).
        for name, decl in _DEVICE_ADDED_COLUMNS:
            try:
                self._con.execute(f"ALTER TABLE devices ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
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
            if not self._con.in_transaction:
                # sqlite3 의 암묵 BEGIN 은 첫 DML 에서만 열린다 — 그 앞의 SELECT(슬롯 교체 대상 조회)가 autocommit 으로
                # 새면 devices.py 의 동시 unrevoke 를 못 본다. 쓰기 락을 먼저 잡아 읽기도 같은 트랜잭션 안에 둔다.
                self._con.execute("BEGIN IMMEDIATE")
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

    def create_device(self, label: str, token_hash: str, now: float, ip: str | None, *, slot: str = "") -> str:
        """기기 행 생성 → 발급한 ``device_id``. **서버는 쓰지 않는다**(등록은 ``register_slot_device``) — 테스트·수동 삽입용.
        슬롯 없는 행은 어떤 재등록에도 교체되지 않고 ``stats.devices_orphaned`` 에 잡힌다."""
        device_id, _ = self.register_slot_device(slot, label, token_hash, now, ip, replace=False)
        return device_id

    def register_slot_device(self, slot: str, label: str, token_hash: str, now: float, ip: str | None,
                             *, replace: bool = True) -> tuple[str, list[str]]:
        """슬롯 초대 코드로 등록 → (발급한 ``device_id``, 이 등록이 제거한 옛 기기 id 들).

        ``replace`` 면 같은 ``slot`` 의 등록(미제거) 기기를 **같은 트랜잭션**(``_tx`` 가 BEGIN IMMEDIATE 로 조회부터 잠근다)에서
        제거한다(``revoked_ts = now``, note 는 덧붙임 ``… / 재등록 교체 → <새 id>``) — 재설치·PC 교체가 관리자 개입 없이 끝난다.
        ``alias`` 는 교체되는 기기의 별칭을 **승계**하고(관리자가 붙인 이름이 재설치마다 사라지지 않게), 없으면 슬롯 이름.
        PK 충돌이면 id 를 다시 만든다(``DEVICE_ID_ATTEMPTS`` 회). ``token_hash`` UNIQUE 충돌(32바이트 난수라 사실상 불가)은
        재생성으로 못 풀어 IntegrityError 로 나간다.
        """
        last_error: sqlite3.IntegrityError | None = None
        for _ in range(DEVICE_ID_ATTEMPTS):
            device_id = new_device_id()
            try:
                with self._tx() as con:
                    olds: list[str] = []
                    alias = slot
                    if replace and slot:
                        rows = con.execute(
                            f"SELECT device_id, alias FROM devices WHERE slot = ? AND {_ACTIVE_WHERE} ORDER BY created_ts",
                            (slot,)).fetchall()
                        olds = [r[0] for r in rows]
                        inherited = [r[1] for r in rows if (r[1] or "").strip()]
                        if inherited:
                            alias = inherited[-1]          # 가장 최근 기기의 별칭
                    con.execute(
                        "INSERT INTO devices (device_id, label, alias, slot, token_hash, created_ts, created_ip) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (device_id, label, alias, slot, token_hash, now, ip))
                    tag = f"재등록 교체 → {device_id}"
                    for old in olds:
                        con.execute("UPDATE devices SET revoked_ts = ?, "
                                    "note = CASE WHEN note = '' THEN ? ELSE note || ' / ' || ? END WHERE device_id = ?",
                                    (now, tag, tag, old))
            except sqlite3.IntegrityError as e:
                last_error = e
                continue
            return device_id, olds
        raise sqlite3.IntegrityError(f"devices 삽입 {DEVICE_ID_ATTEMPTS}회 충돌") from last_error

    def get_device_by_token_hash(self, token_hash: str) -> dict | None:
        r = self._con.execute(
            "SELECT device_id, label, alias, slot, revoked_ts, upload_count, last_seen_ts FROM devices WHERE token_hash = ?",
            (token_hash,)).fetchone()
        return dict(r) if r is not None else None

    def get_device(self, device_id: str) -> dict | None:
        """관리용 — 토큰 해시는 싣지 않는다."""
        r = self._con.execute(f"SELECT {_DEVICE_COLUMNS} FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        return dict(r) if r is not None else None

    def count_active_devices(self) -> int:
        """등록된(미제거) 기기 수(``_ACTIVE_WHERE``) — ``stats.devices_registered``. 업로드를 한 번도 안 한 기기도
        센다(자동 제외 없음) — 빠지는 것은 관리자의 ``devices.py revoke`` 또는 같은 슬롯의 재등록 교체뿐."""
        return int(self._con.execute(f"SELECT COUNT(*) FROM devices WHERE {_ACTIVE_WHERE}").fetchone()[0])

    def orphan_devices(self, slots) -> list[dict]:
        """등록(미제거) 기기 중 ``slot`` 이 설정 ``invite_codes`` 에 없는 것 — 슬롯 이름을 바꾸거나 지웠거나(옛 이름의 기기는
        재등록 교체 대상에서 빠진다), 슬롯 없이 수동 삽입한 행. 기동 경고·``stats.devices_orphaned`` 용."""
        slots = set(slots)
        return [dict(r) for r in self._con.execute(
            f"SELECT {_DEVICE_COLUMNS} FROM devices WHERE {_ACTIVE_WHERE} ORDER BY created_ts, device_id").fetchall()
            if r["slot"] not in slots]

    def touch_device(self, device_id: str, now: float) -> None:
        """인증은 됐지만 거부된 업로드(400·403) — ``last_seen_ts`` 만(운영자가 잘못 설정된 exe 를 찾을 수 있게)."""
        with self._tx() as con:
            con.execute("UPDATE devices SET last_seen_ts = ? WHERE device_id = ?", (now, device_id))

    def list_devices(self, *, active_only: bool = False) -> list[dict]:
        """토큰 해시는 싣지 않는다(CLI 출력·로그로 새지 않게). ``active_only`` 면 미제거 기기만(재등록 교체가 쌓은 옛 행 제외)."""
        where = f"WHERE {_ACTIVE_WHERE} " if active_only else ""
        return [dict(r) for r in self._con.execute(
            f"SELECT {_DEVICE_COLUMNS} FROM devices {where}ORDER BY created_ts, device_id").fetchall()]

    def set_device_revoked(self, device_id: str, revoked_ts: float | None) -> bool:
        """제거(시각) 또는 복구(None) — 다음 요청부터 바로 먹는다. 반환 = 그런 기기가 있었는지."""
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

    @property
    def listing_expiry_days(self) -> int:
        return self._expiry_days

    def _listing_dict(self, r) -> dict:
        # expires_from/by 는 저장하지 않고 조회 때 계산(스키마 무변경) — 정의는 모듈 상단 주석·HUB-PROTOCOL §4.
        frm, by = expiry_bounds(r["first_seen_ts"], r["last_seen_ts"], self._expiry_days)
        return {"listing_key": r["listing_key"], "listing_id": r["listing_id"], "item_id": r["item_id"],
                "item_name": r["item_name"], "quantity": r["quantity"], "price": r["price"],
                "seller": r["seller"], "category": r["category"], "flag45": r["flag45"], "flag46": r["flag46"],
                "first_seen_ts": r["first_seen_ts"], "last_seen_ts": r["last_seen_ts"],
                "seen_count": r["seen_count"], "last_device": r["last_device"],
                "expires_from_ts": frm, "expires_by_ts": by}

    def search_market(self, q_norm: str, item_id, limit: int, min_seen_ts: float, now: float | None = None) -> tuple:
        """이름 부분일치(정규화 키 ``instr``)·아이템 id 로 목록 검색. 반환 (살아 있는 행 목록, 신선도 무관 매칭 수).

        살아 있는 행 = ``expires_by_ts > now``(소멸 상한이 아직 안 지남) **AND** ``last_seen_ts >= min_seen_ts``
        (관측 공백 안전망 ``max_age_sec``). ``now`` 가 None 이면 만료 필터를 걸지 않는다(옛 호출·DB 테스트 호환).
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
        live, live_params = "", []
        if now is not None:
            live, live_params = " AND " + _LIVE_WHERE, _live_params(now, self._expiry_days)
        rows = self._con.execute(
            _LISTING_SELECT + cond + " AND l.last_seen_ts >= ?" + live +
            " ORDER BY l.price ASC, l.last_seen_ts DESC, l.listing_key LIMIT ?",
            params + [float(min_seen_ts)] + live_params + [int(limit)]).fetchall()
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

    def market_stats(self, now: float, fresh_sec: float = 86400.0, slots=()) -> dict:
        """``slots`` = 설정된 슬롯 이름들 — ``devices_orphaned``(등록됐지만 어느 슬롯에도 안 속하는 기기) 계산용."""
        con = self._con

        def one(sql: str, *params):
            return con.execute(sql, params).fetchone()[0]

        # 관측을 올린 device_id 별 집계 + 등록 기기면 별칭·label(관리 시크릿으로 올린 자유 device_id 는 null).
        devices = [{"device_id": r["device_id"], "label": r["label"], "alias": r["alias"], "slot": r["slot"],
                    "observations": r["c"], "last_recv_ts": r["m"]}
                   for r in con.execute(
                       "SELECT o.device_id, d.label AS label, d.alias AS alias, d.slot AS slot, COUNT(*) AS c, "
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
            # 소멸 상한이 아직 안 지난 행 — search 기본 필터와 같은 정의(§3-2). fresh_listings 는 관측 나이 기준(별개).
            "live_listings": one("SELECT COUNT(*) FROM market_listings l WHERE " + _LIVE_WHERE,
                                 *_live_params(now, self._expiry_days)),
            "expiry_days": self._expiry_days,
            "latest_recv_ts": one("SELECT MAX(recv_ts) FROM market_observations"),
            "devices": devices,
            # 등록 = 정원을 차지하는 수(미제거). 자동 제외가 없으니 "활성"과 같은 수라 따로 싣지 않는다.
            "devices_registered": self.count_active_devices(),
            "devices_revoked": one("SELECT COUNT(*) FROM devices WHERE revoked_ts IS NOT NULL"),
            "devices_orphaned": len(self.orphan_devices(slots)),
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
