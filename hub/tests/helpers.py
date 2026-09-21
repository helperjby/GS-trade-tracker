"""테스트 공용 헬퍼 — pytest-aiohttp 미도입 방침이라 각 테스트가 ``asyncio.run(...)`` +
``aiohttp.test_utils`` 를 직접 쓴다(SEAssist dashboard 테스트와 같은 패턴, CI 의존성 = aiohttp + pytest).

데이터는 전부 합성 — 판매자 ``판매자A`` 류, 기기 ``DEV-1`` 류. 실제 판매자명·창 파일은 쓰지 않는다.
"""
from __future__ import annotations

import copy
import time

from aiohttp.test_utils import TestClient, TestServer

import server as server_mod

SECRET = "test-secret"
AUTH = {"Authorization": "Bearer " + SECRET}

#: 실제 응답(0x321f) 페이지의 행 직렬화 키 — SEAssist ``packet_market.row_to_dict`` + 관측기의 ``item_name``.
ROW_DEFAULTS = {
    "listing_id": 700001, "item_id": 853, "item_name": "봉인의돌",
    "quantity": 10, "quantity_hi": 0, "price": 45_000_000, "price_hi": 0,
    "seller": "판매자A", "unknown40": "00000000", "flag45": 2, "flag46": 0,
}


def make_cfg(tmp_path, **over) -> dict:
    cfg = copy.deepcopy(server_mod.DEFAULTS)
    cfg["secret"] = SECRET
    cfg["db_path"] = str(tmp_path / "hub.db")
    cfg.update(over)
    return cfg


async def start_client(cfg):
    app = server_mod.make_app(cfg)
    client = TestClient(TestServer(app))
    await client.start_server()
    return app, client


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
