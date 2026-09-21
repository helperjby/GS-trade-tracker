"""육의전 시세 허브 서버 — aiohttp 단독 + stdlib sqlite3 (관측기 ``src/`` 무결합, 독립 배포).

라우팅: ``GET /``(무인증 상태 줄) · ``POST /api/market/observations``(관측기 업로드) ·
``GET /api/market/search`` · ``GET /api/market/listings`` · ``GET /api/market/stats``(미루봇·운영 확인).
``/api/*`` 는 ``Authorization: Bearer <secret>`` 필수. 계약 정본은 docs/HUB-PROTOCOL.md.

tolerant reader: 미지 키는 무시(관측 봉투 원문은 payload_json 에 보존)하되, 형이 틀린 필수 필드는
400 으로 거부하고 그 요청은 **아무것도 쓰지 않는다**(전건 검증 뒤 트랜잭션 1개).
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hmac
import json
import logging
import math
import os
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
}
#: 관측 100건 × 행 64 × 행당 ~180B ≈ 1.2MB — aiohttp 기본 1MiB 보다 넉넉하게.
CLIENT_MAX_SIZE = 4 * 1024 * 1024


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    cfg = copy.deepcopy(DEFAULTS)
    cfg.update(raw)
    if not cfg.get("secret"):
        raise SystemExit("config: 'secret' 가 비어 있습니다 — config.json 에 설정하세요")
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
    # bool 은 int 서브클래스 — true/false 를 수량·가격으로 오인하지 않는다.
    return isinstance(v, int) and not isinstance(v, bool)


def _is_opt_int(v) -> bool:
    return v is None or _is_int(v)


def _is_num(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(float(v)))


def validate_upload(body, cfg: dict) -> tuple:
    """POST 본문 전건 검증 → (device_id, observations). 첫 위반에서 ``_Bad``.

    행 필드는 SEAssist ``packet_market.row_to_dict`` 키 + 관측기가 붙인 ``item_name``(null 허용).
    미지 키는 통과(payload_json 에 남는다).
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
    for i, obs in enumerate(obs_list):
        if not isinstance(obs, dict):
            raise _Bad(index=i)
        obs_id = obs.get("obs_id")
        if not isinstance(obs_id, str) or not 1 <= len(obs_id) <= 128:
            raise _Bad(index=i, field="obs_id")
        if not _is_num(obs.get("agent_ts")):
            raise _Bad(index=i, field="agent_ts")
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
            if not isinstance(row.get("seller"), str):
                raise _Bad(index=i, field=f"rows[{j}].seller")
            name = row.get("item_name")
            if name is not None and not isinstance(name, str):
                raise _Bad(index=i, field=f"rows[{j}].item_name")
            for k in ("flag45", "flag46"):
                if not _is_opt_int(row.get(k)):
                    raise _Bad(index=i, field=f"rows[{j}].{k}")
            category = row.get("category")
            if category is not None and not isinstance(category, str):
                raise _Bad(index=i, field=f"rows[{j}].category")
    return device_id.strip(), obs_list


def _q_int(request: web.Request, key: str, default, lo=None, hi=None):
    """관용적 쿼리 정수 — 없거나 못 읽으면 default, 범위는 클램프."""
    raw = request.query.get(key)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _q_float(request: web.Request, key: str, default, lo=None, hi=None):
    raw = request.query.get(key)
    if raw is None or raw == "":
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    if not math.isfinite(v):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


# ---------------------------------------------------------------- handlers

async def index(request: web.Request) -> web.Response:
    """무인증 상태 줄 — 데이터 없음(접속·healthcheck 확인용)."""
    return web.json_response({"service": SERVICE, "v": PROTO_V, "server_time": time.time()})


async def api_market_observations(request: web.Request) -> web.Response:
    app = request.app
    try:
        body = await request.json()
    except ValueError:
        body = None
    try:
        device_id, obs_list = validate_upload(body, app["cfg"])
    except _Bad as e:
        return e.response()
    now = time.time()
    accepted, duplicates, rows = app["db"].insert_market_observations(device_id, obs_list, now)
    log.info("market 관측 수신: %s 신규 %d / 중복 %d / 행 %d", device_id, accepted, duplicates, rows)
    return web.json_response({"ok": True, "accepted": accepted, "duplicates": duplicates,
                              "rows": rows, "server_time": now})


async def api_market_search(request: web.Request) -> web.Response:
    app = request.app
    cfg = app["cfg"]
    q = request.query.get("q", "")
    q_norm = db_mod.norm_item_name(q)
    item_id = _q_int(request, "item_id", None, 0, None)
    if not q_norm and item_id is None:
        return web.json_response({"ok": False, "error": "bad_request", "field": "q"}, status=400)
    limit = _q_int(request, "limit", int(cfg["search_limit_default"]), 1, int(cfg["search_limit_max"]))
    max_age = _q_float(request, "max_age_sec", float(cfg["search_max_age_sec"]), 0.0, None)
    now = time.time()
    rows, total = app["db"].search_market(q_norm, item_id, limit, now - max_age)
    return web.json_response({"v": PROTO_V, "server_time": now, "q": q, "q_norm": q_norm,
                              "item_id": item_id, "max_age_sec": max_age, "limit": limit,
                              "count": len(rows), "total_matches": total, "listings": rows})


async def api_market_listings(request: web.Request) -> web.Response:
    app = request.app
    cfg = app["cfg"]
    since = _q_float(request, "since_ts", 0.0, 0.0, None)
    limit = _q_int(request, "limit", int(cfg["listings_limit_default"]), 1, int(cfg["listings_limit_max"]))
    now = time.time()
    rows = app["db"].list_market_since(since, limit)
    next_since = rows[-1]["last_seen_ts"] if rows else since
    return web.json_response({"v": PROTO_V, "server_time": now, "since_ts": since, "limit": limit,
                              "count": len(rows), "next_since_ts": next_since, "listings": rows})


async def api_market_stats(request: web.Request) -> web.Response:
    app = request.app
    now = time.time()
    stats = app["db"].market_stats(now, float(app["cfg"]["search_max_age_sec"]))
    return web.json_response({"v": PROTO_V, "server_time": now, **stats})


# ---------------------------------------------------------------- retention

async def _retention_loop(app: web.Application) -> None:
    cfg = app["cfg"]
    while True:
        try:
            n_o, n_l = app["db"].prune(time.time(), int(cfg["retention_market_days"]))
            if n_o or n_l:
                log.info("retention: 관측 %d건 / 목록 %d건 삭제", n_o, n_l)
        except Exception:
            log.exception("retention 오류 — 태스크 유지")
        await asyncio.sleep(86400)


# ---------------------------------------------------------------- app

def make_app(cfg: dict, database: db_mod.Database | None = None) -> web.Application:
    app = web.Application(middlewares=[_auth_middleware(cfg["secret"])],
                          client_max_size=CLIENT_MAX_SIZE)
    app["cfg"] = cfg
    app["db"] = database if database is not None else db_mod.Database(cfg["db_path"])
    app.router.add_get("/", index)
    app.router.add_post("/api/market/observations", api_market_observations)
    app.router.add_get("/api/market/search", api_market_search)
    app.router.add_get("/api/market/listings", api_market_listings)
    app.router.add_get("/api/market/stats", api_market_stats)

    async def _start_tasks(app_: web.Application) -> None:
        app_["retention_task"] = asyncio.get_event_loop().create_task(_retention_loop(app_))

    async def _cleanup(app_: web.Application) -> None:
        task = app_.get("retention_task")
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        app_["db"].close()

    app.on_startup.append(_start_tasks)
    app.on_cleanup.append(_cleanup)
    return app


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="육의전 시세 허브 서버")
    parser.add_argument("--config",
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config(args.config)
    web.run_app(make_app(cfg), host=cfg["host"], port=int(cfg["port"]))


if __name__ == "__main__":
    main()
