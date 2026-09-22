"""육의전 시세 허브 서버 — aiohttp 단독 + stdlib sqlite3 (관측기 ``src/`` 무결합, 독립 배포).

라우팅: ``GET /``(무인증 상태 줄) · ``POST /api/market/observations``(관측기 업로드) ·
``GET /api/market/search`` · ``GET /api/market/listings`` · ``GET /api/market/stats``(미루봇·운영 확인).
``/api/*`` 는 ``Authorization: Bearer <secret>`` 필수. 계약 정본은 docs/HUB-PROTOCOL.md.

tolerant reader: 미지 키는 무시(관측 봉투는 payload_json 에 보존 — 행은 미지 키만)하되, 형·범위가 틀린 필수
필드는 400 으로 거부하고 그 요청은 **아무것도 쓰지 않는다**(전건 검증 뒤 트랜잭션 1개). 정수는 SQLite 의
signed 64bit 범위 안이어야 한다 — 검증을 통과한 값이 저장에서 터지면(500) 관측기가 그 배치를 영원히 재시도한다.

설정(``load_config``): ``secret`` 는 16자 이상 문자열이고 예시값(``CHANGE-ME``)이면 기동을 거부한다(기본 host
``0.0.0.0`` 이라 공개 시크릿으로 LAN 에 쓰기 API 가 열린다). 상대 ``db_path`` 는 CWD 가 아니라 **config 파일
폴더** 기준(레포 루트에서 띄워도 ``<repo>/data/hub.db`` 가 생기지 않는다). 환경변수 ``HUB_DB_PATH`` 가 있으면
그것이 이긴다 — 컨테이너는 Dockerfile 이 ``/data/hub.db`` 로 고정해 운영자가 config 를 안 고쳐도 볼륨에 쓴다.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import hmac
import json
import logging
import math
import os
import sqlite3
import time

from aiohttp import web

import db as db_mod

log = logging.getLogger("hub")

PROTO_V = 1
SERVICE = "yuktracker-hub"

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8800,
    "secret": "",
    "db_path": "data/hub.db",
    "retention_market_days": 30,
    "search_max_age_sec": 86400,
    "search_limit_default": 20,
    "search_limit_max": 100,
    "listings_limit_default": 500,
    "listings_limit_max": 2000,
    "max_observations_per_request": 100,
    "max_rows_per_observation": 64,
    #: 관측기 시계가 서버보다 이만큼 넘게 앞서면 400 — 그 안이면 recv_ts 로 깎는다(§4).
    "max_agent_ts_ahead_sec": 86400,
}
#: 관측 100건 × 행 64 × 행당 ~180B ≈ 1.2MB — aiohttp 기본 1MiB 보다 넉넉하게.
CLIENT_MAX_SIZE = 4 * 1024 * 1024
ENV_DB_PATH = "HUB_DB_PATH"
MIN_SECRET_LEN = 16
PLACEHOLDER_SECRETS = frozenset({"CHANGE-ME"})
#: SQLite INTEGER 범위 — 밖이면 sqlite3 가 OverflowError 를 던진다.
I64_MIN, I64_MAX = -(1 << 63), (1 << 63) - 1
#: 문자열 상한 — seller 는 listing_key(PK)에 들어가고 item_name 은 학습 표·정규화 키로 복사된다.
MAX_SELLER_LEN = 128
MAX_ITEM_NAME_LEN = 128
MAX_CATEGORY_LEN = 32

CFG_KEY = web.AppKey("cfg", dict)
DB_KEY = web.AppKey("db", db_mod.Database)


def load_config(path: str, env=None) -> dict:
    env = os.environ if env is None else env
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise SystemExit("config: JSON 객체가 아닙니다")
    cfg = copy.deepcopy(DEFAULTS)
    cfg.update(raw)
    secret = cfg.get("secret")
    if not isinstance(secret, str) or not secret:
        raise SystemExit("config: 'secret' 는 비어 있지 않은 문자열이어야 합니다 — config.json 에 설정하세요")
    if secret in PLACEHOLDER_SECRETS:
        raise SystemExit("config: 'secret' 가 예시값입니다 — config.json.example 을 복사했으면 실제 값으로 바꾸세요")
    if len(secret) < MIN_SECRET_LEN:
        raise SystemExit(f"config: 'secret' 는 {MIN_SECRET_LEN}자 이상이어야 합니다")
    db_path = env.get(ENV_DB_PATH) or cfg.get("db_path")
    if not isinstance(db_path, str) or not db_path:
        raise SystemExit("config: 'db_path' 가 비어 있습니다")
    # 루트 경로(`/data/hub.db` — 컨테이너 ENV)는 Windows 의 isabs 가 3.13 부터 거부하므로 선두 슬래시도 절대로 본다.
    rooted = os.path.isabs(db_path) or db_path[:1] in ("/", "\\")
    if db_path != ":memory:" and not rooted:
        db_path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(path)), db_path))
    cfg["db_path"] = db_path
    return cfg


# ---------------------------------------------------------------- auth

def _secret_eq(a: str, b: str) -> bool:
    # compare_digest 는 비ASCII str 비교를 거부 — 항상 UTF-8 바이트로 비교.
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _auth_middleware(secret: str):
    @web.middleware
    async def auth_mw(request: web.Request, handler):
        if request.path.startswith("/api/"):
            auth = request.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            if not (token and _secret_eq(token, secret)):
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)
    return auth_mw


# ---------------------------------------------------------------- 검증

class _Bad(Exception):
    """400 사유 — error 코드 + (관측 index, 필드 경로)."""

    def __init__(self, error: str = "bad_request", index=None, field=None) -> None:
        super().__init__(error)
        self.error, self.index, self.field = error, index, field

    def response(self) -> web.Response:
        body: dict = {"ok": False, "error": self.error}
        if self.index is not None:
            body["index"] = self.index
        if self.field:
            body["field"] = self.field
        return web.json_response(body, status=400)


def _is_int(v) -> bool:
    # bool 은 int 서브클래스 — true/false 를 수량·가격으로 오인하지 않는다. 범위는 SQLite INTEGER(i64).
    return isinstance(v, int) and not isinstance(v, bool) and I64_MIN <= v <= I64_MAX


def _is_opt_int(v) -> bool:
    return v is None or _is_int(v)


def _is_num(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(float(v)))


def _is_str_max(v, max_len: int) -> bool:
    return isinstance(v, str) and len(v) <= max_len


def validate_upload(body, cfg: dict, now: float) -> tuple:
    """POST 본문 전건 검증 → (device_id, observations). 첫 위반에서 ``_Bad``.

    행 필드는 SEAssist ``packet_market.row_to_dict`` 키 + 관측기가 붙인 ``item_name``(null 허용).
    미지 키는 통과(payload_json 에 남는다). ``agent_ts`` 는 ``[now − 보존기간, now + max_agent_ts_ahead_sec]``
    안이어야 한다 — 시계가 리셋된 기기의 관측(seen_ts≈0)은 200 을 받고도 어떤 조회에도 안 보이고 다음 prune 에
    지워져 소리 없이 사라지므로, 관측기가 격리·로그로 알 수 있게 400 으로 돌려준다.
    """
    if not isinstance(body, dict):
        raise _Bad()
    device_id = body.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 128:
        raise _Bad(field="device_id")
    obs_list = body.get("observations")
    if not isinstance(obs_list, list) or not obs_list:
        raise _Bad(field="observations")
    if len(obs_list) > int(cfg["max_observations_per_request"]):
        raise _Bad("too_many", field="observations")
    max_rows = int(cfg["max_rows_per_observation"])
    oldest = now - int(cfg["retention_market_days"]) * 86400
    newest = now + float(cfg["max_agent_ts_ahead_sec"])
    for i, obs in enumerate(obs_list):
        if not isinstance(obs, dict):
            raise _Bad(index=i)
        obs_id = obs.get("obs_id")
        if not isinstance(obs_id, str) or not 1 <= len(obs_id) <= 128:
            raise _Bad(index=i, field="obs_id")
        agent_ts = obs.get("agent_ts")
        if not _is_num(agent_ts):
            raise _Bad(index=i, field="agent_ts")
        if not oldest <= float(agent_ts) <= newest:
            raise _Bad("agent_ts_out_of_range", index=i, field="agent_ts")
        if not _is_int(obs.get("opcode")):
            raise _Bad(index=i, field="opcode")
        for k in ("page", "total_pages", "hdr4"):
            if not _is_opt_int(obs.get(k)):
                raise _Bad(index=i, field=k)
        rows = obs.get("rows")
        if not isinstance(rows, list):
            raise _Bad(index=i, field="rows")
        if len(rows) > max_rows:
            raise _Bad("too_many", index=i, field="rows")
        for j, row in enumerate(rows):
            if not isinstance(row, dict):
                raise _Bad(index=i, field=f"rows[{j}]")
            for k in ("listing_id", "item_id", "quantity", "price"):
                v = row.get(k)
                if not _is_int(v) or v < 0:
                    raise _Bad(index=i, field=f"rows[{j}].{k}")
            if not _is_str_max(row.get("seller"), MAX_SELLER_LEN):
                raise _Bad(index=i, field=f"rows[{j}].seller")
            name = row.get("item_name")
            if name is not None and not _is_str_max(name, MAX_ITEM_NAME_LEN):
                raise _Bad(index=i, field=f"rows[{j}].item_name")
            for k in ("flag45", "flag46"):
                if not _is_opt_int(row.get(k)):
                    raise _Bad(index=i, field=f"rows[{j}].{k}")
            category = row.get("category")
            if category is not None and not _is_str_max(category, MAX_CATEGORY_LEN):
                raise _Bad(index=i, field=f"rows[{j}].category")
    return device_id.strip(), obs_list


def _q_num(request: web.Request, key: str, default, cast, lo=None, hi=None):
    """관용적 쿼리 수치(limit·since_ts·max_age_sec) — 없거나 못 읽으면 default, 범위는 클램프(default 도 — 설정의
    `*_limit_default` 가 `*_limit_max` 를 넘어도 max 를 지킨다). ``cast`` 는 int|float."""
    raw = request.query.get(key)
    v = default
    if raw is not None and raw != "":
        try:
            v = cast(raw)
        except (ValueError, OverflowError):
            v = default
        if cast is float and not math.isfinite(v):
            v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _q_item_id(request: web.Request):
    """``item_id`` 는 있으면 정확해야 한다 — 음수·비정수·i64 초과는 400. 조용히 0 으로 깎거나 버리면 틀린 결과가
    옳은 것처럼 나간다(예: ``item_id=abc&q=봉인`` 이 id 필터 없이 전부 반환)."""
    raw = request.query.get("item_id")
    if raw is None or raw == "":
        return None
    try:
        v = int(raw)
    except ValueError:
        raise _Bad(field="item_id") from None
    if not 0 <= v <= I64_MAX:
        raise _Bad(field="item_id")
    return v


# ---------------------------------------------------------------- handlers

async def index(request: web.Request) -> web.Response:
    """무인증 상태 줄 — 데이터 없음(접속·healthcheck 확인용)."""
    return web.json_response({"service": SERVICE, "v": PROTO_V, "server_time": time.time()})


async def api_market_observations(request: web.Request) -> web.Response:
    cfg = request.app[CFG_KEY]
    try:
        body = await request.json()
    except ValueError:
        body = None
    now = time.time()
    try:
        device_id, obs_list = validate_upload(body, cfg, now)
    except _Bad as e:
        return e.response()
    try:
        accepted, duplicates, rows = request.app[DB_KEY].insert_market_observations(device_id, obs_list, now)
    except sqlite3.Error:
        # 검증을 통과한 뒤의 저장 오류 — JSON 5xx 로(관측기는 5xx 를 재시도, 본문은 로그에 남긴다).
        log.exception("market 관측 저장 실패: %s", device_id)
        return web.json_response({"ok": False, "error": "storage_error", "server_time": now}, status=500)
    log.info("market 관측 수신: %s 신규 %d / 중복 %d / 행 %d", device_id, accepted, duplicates, rows)
    return web.json_response({"ok": True, "accepted": accepted, "duplicates": duplicates,
                              "rows": rows, "server_time": now})


async def api_market_search(request: web.Request) -> web.Response:
    cfg = request.app[CFG_KEY]
    q = request.query.get("q", "")
    q_norm = db_mod.norm_item_name(q)
    try:
        item_id = _q_item_id(request)
        if not q_norm and item_id is None:
            raise _Bad(field="q")
    except _Bad as e:
        return e.response()
    limit = _q_num(request, "limit", int(cfg["search_limit_default"]), int, 1, int(cfg["search_limit_max"]))
    max_age = _q_num(request, "max_age_sec", float(cfg["search_max_age_sec"]), float, 0.0, None)
    now = time.time()
    rows, total = request.app[DB_KEY].search_market(q_norm, item_id, limit, now - max_age)
    return web.json_response({"v": PROTO_V, "server_time": now, "q": q, "q_norm": q_norm,
                              "item_id": item_id, "max_age_sec": max_age, "limit": limit,
                              "count": len(rows), "total_matches": total, "listings": rows})


async def api_market_listings(request: web.Request) -> web.Response:
    cfg = request.app[CFG_KEY]
    since = _q_num(request, "since_ts", 0.0, float, 0.0, None)
    since_key = request.query.get("since_key", "")
    limit = _q_num(request, "limit", int(cfg["listings_limit_default"]), int, 1, int(cfg["listings_limit_max"]))
    now = time.time()
    rows = request.app[DB_KEY].list_market_since(since, since_key, limit)
    next_since = rows[-1]["last_seen_ts"] if rows else since
    next_key = rows[-1]["listing_key"] if rows else since_key
    return web.json_response({"v": PROTO_V, "server_time": now, "since_ts": since, "since_key": since_key,
                              "limit": limit, "count": len(rows), "next_since_ts": next_since,
                              "next_since_key": next_key, "listings": rows})


async def api_market_stats(request: web.Request) -> web.Response:
    now = time.time()
    stats = request.app[DB_KEY].market_stats(now, float(request.app[CFG_KEY]["search_max_age_sec"]))
    return web.json_response({"v": PROTO_V, "server_time": now, **stats})


# ---------------------------------------------------------------- retention

async def _retention_loop(app: web.Application) -> None:
    cfg = app[CFG_KEY]
    while True:
        try:
            n_o, n_l = app[DB_KEY].prune(time.time(), int(cfg["retention_market_days"]))
            if n_o or n_l:
                log.info("retention: 관측 %d건 / 목록 %d건 삭제", n_o, n_l)
        except Exception:
            log.exception("retention 오류 — 태스크 유지")
        await asyncio.sleep(86400)


# ---------------------------------------------------------------- app

def make_app(cfg: dict, database: db_mod.Database | None = None) -> web.Application:
    """``database`` 를 주입하면 소유권은 호출자에게 — 앱이 닫혀도 close 하지 않는다(테스트·임베딩이 DB 를 재사용)."""
    app = web.Application(middlewares=[_auth_middleware(cfg["secret"])],
                          client_max_size=CLIENT_MAX_SIZE)
    owns_db = database is None
    app[CFG_KEY] = cfg
    app[DB_KEY] = database if database is not None else db_mod.Database(cfg["db_path"])
    app.router.add_get("/", index)
    app.router.add_post("/api/market/observations", api_market_observations)
    app.router.add_get("/api/market/search", api_market_search)
    app.router.add_get("/api/market/listings", api_market_listings)
    app.router.add_get("/api/market/stats", api_market_stats)

    async def _lifecycle(app_: web.Application):
        task = asyncio.create_task(_retention_loop(app_))
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if owns_db:
            app_[DB_KEY].close()

    app.cleanup_ctx.append(_lifecycle)
    return app


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="육의전 시세 허브 서버")
    parser.add_argument("--config",
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config(args.config)
    log.info("db_path=%s host=%s port=%s", cfg["db_path"], cfg["host"], cfg["port"])
    web.run_app(make_app(cfg), host=cfg["host"], port=int(cfg["port"]))


if __name__ == "__main__":
    main()
