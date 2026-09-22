"""테스트 공용 헬퍼 — pytest-aiohttp 미도입 방침이라 각 테스트가 ``asyncio.run(...)`` +
``aiohttp.test_utils`` 를 직접 쓴다(SEAssist dashboard 테스트와 같은 패턴, CI 의존성 = aiohttp + pytest).

데이터는 전부 합성 — 판매자 ``판매자A`` 류, 기기 ``DEV-1`` 류, IP 는 TEST-NET(203.0.113.0/24·198.51.100.0/24).
실제 판매자명·창 파일은 쓰지 않는다.
"""
from __future__ import annotations

import copy
import time

from aiohttp.test_utils import TestClient, TestServer

import server as server_mod

SECRET = "test-secret"
AUTH = {"Authorization": "Bearer " + SECRET}
INVITE = "test-invite-code"
#: Funnel 경유 흉내 — tailscaled 가 붙이는 두 헤더(HUB-PROTOCOL §0). 값은 무관, 존재만 본다.
PROXY = {"Tailscale-Funnel-Request": "?1", "X-Forwarded-For": "203.0.113.7"}

#: 실제 응답(0x321f) 페이지의 행 직렬화 키 — SEAssist ``packet_market.row_to_dict`` + 관측기의 ``item_name``.
ROW_DEFAULTS = {
    "listing_id": 700001, "item_id": 853, "item_name": "봉인의돌",
    "quantity": 10, "quantity_hi": 0, "price": 45_000_000, "price_hi": 0,
    "seller": "판매자A", "unknown40": "00000000", "flag45": 2, "flag46": 0,
}


def make_cfg(tmp_path, **over) -> dict:
    cfg = copy.deepcopy(server_mod.DEFAULTS)
    cfg["secret"] = SECRET
    cfg["invite_code"] = INVITE
    cfg["db_path"] = str(tmp_path / "hub.db")
    cfg.update(over)
    return cfg


async def start_client(cfg, database=None, *, public_only=False):
    """``public_only=True`` 면 공개 리스너 앱(모든 요청을 공개로 취급 — Funnel 이 가리키는 쪽)."""
    app = server_mod.make_app(cfg, database, public_only=public_only)
    client = TestClient(TestServer(app))
    await client.start_server()
    return app, client


def app_db(app):
    """앱이 쓰는 Database — 문자열 키가 아니라 ``web.AppKey`` 라 헬퍼로 감춘다."""
    return app[server_mod.DB_KEY]


def row(**over) -> dict:
    r = dict(ROW_DEFAULTS)
    r.update(over)
    return r


def obs(obs_id: str = "DEV-1:1000000:aaaaaaaaaaaa", agent_ts: float | None = None,
        rows=None, **over) -> dict:
    """관측 봉투 1개(페이지 1개) — 관측기(PR-Y1b)가 보내는 모양. agent_ts 기본 = 지금."""
    o = {"obs_id": obs_id, "agent_ts": time.time() if agent_ts is None else agent_ts,
         "opcode": 0x321F, "page": None, "total_pages": 3, "hdr4": 0, "anomalies": [],
         "item_table": {"gcs_sha256": "00" * 32, "rows": 4001, "archive_ts": "2026-09-21T02:04:11Z"},
         "rows": [row()] if rows is None else rows}
    o.update(over)
    return o


def body(*observations, device_id: str = "DEV-1") -> dict:
    return {"v": 1, "device_id": device_id, "observations": list(observations)}


async def post(client, payload, headers=AUTH):
    resp = await client.post("/api/market/observations", json=payload, headers=headers)
    return resp.status, await resp.json()


async def get(client, path: str, params=None, headers=AUTH):
    resp = await client.get(path, params=params, headers=headers)
    return resp.status, await resp.json()


async def register(client, invite=INVITE, label="PC-1", headers=None, **extra):
    """``POST /api/market/register`` — 무 Bearer. ``headers=PROXY`` 면 Funnel 경유 흉내."""
    payload = {"v": 1, "invite_code": invite, "label": label, **extra}
    resp = await client.post("/api/market/register", json=payload, headers=headers)
    return resp.status, await resp.json()


def device_auth(token: str) -> dict:
    return {"Authorization": "Bearer " + token}
