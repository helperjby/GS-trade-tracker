"""hub `GET /api/market/ping` — 기기 토큰 확인(관측기 `--selftest`). HUB-PROTOCOL §3-7.

판정은 전부 인증 미들웨어가 하므로 여기서 보는 것은 **미들웨어가 이 라우트에도 같은 규칙을 적용하는가**다:
공개 리스너 통과(조회 라우트가 아니다) · 무효 토큰 401 · 취소 403 · 기기당 속도제한(업로드 버킷 공유) ·
그리고 **DB 무변경**(last_seen_ts 는 "마지막 업로드 시도" 의미를 지킨다).
"""
from __future__ import annotations

import asyncio
import time

from helpers import AUTH, PROXY, app_db, device_auth, get, make_cfg, register, start_client


def _run(coro):
    return asyncio.run(coro)


async def _registered(client):
    st, data = await register(client, label="PC-1")
    assert st == 200
    return data["device_id"], data["token"]


def test_ping_device_token_ok_and_does_not_touch_db(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            dev_id, token = await _registered(client)
            st, data = await get(client, "/api/market/ping", headers=device_auth(token))
            assert st == 200 and data["ok"] is True and data["v"] == 1
            assert (data["device_id"], data["label"]) == (dev_id, "slot1")   # 초기 별칭 = 슬롯 이름이 label 보다 앞선다
            assert data["server_time"] > 0
            # ping 은 기기 행을 쓰지 않는다 — 업로드가 있었는지는 last_seen_ts 로만 읽힌다
            (d,) = app_db(app).list_devices()
            assert d["last_seen_ts"] is None and d["upload_count"] == 0
            # 관리자 별칭이 있으면 그것이 앞선다(devices.py list·서버 로그와 같은 규칙)
            assert app_db(app).set_device_alias(dev_id, "친구1")
            _, data2 = await get(client, "/api/market/ping", headers=device_auth(token))
            assert data2["label"] == "친구1"
        finally:
            await client.close()
    _run(scn())


def test_ping_works_through_public_listener_and_proxy_header(tmp_path):
    """Funnel 뒤의 지인 PC 가 부르는 경로 — 조회 라우트(403 not_public)와 달리 통과해야 한다."""
    async def scn():
        cfg = make_cfg(tmp_path)
        app, client = await start_client(cfg, public_only=True)
        try:
            dev_id, token = await _registered(client)
            st, data = await get(client, "/api/market/ping",
                                 headers={**device_auth(token), **PROXY})
            assert st == 200 and data["device_id"] == dev_id
            # 같은 리스너의 조회 라우트는 그대로 막힌다
            st2, data2 = await get(client, "/api/market/stats", headers=device_auth(token))
            assert (st2, data2["error"]) == (403, "not_public")
            # 공개 요청의 관리 시크릿은 무시된다(자격으로 쓰이지 않는다)
            st3, _ = await get(client, "/api/market/ping", headers=AUTH)
            assert st3 == 401
        finally:
            await client.close()
    _run(scn())


def test_ping_rejects_missing_bad_and_revoked_tokens(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            dev_id, token = await _registered(client)
            resp = await client.get("/api/market/ping")
            assert resp.status == 401 and (await resp.json())["error"] == "unauthorized"
            st, data = await get(client, "/api/market/ping", headers=device_auth(token + "x"))
            assert (st, data["error"]) == (401, "unauthorized")
            assert app_db(app).set_device_revoked(dev_id, time.time())
            st2, data2 = await get(client, "/api/market/ping", headers=device_auth(token))
            assert (st2, data2["error"]) == (403, "device_revoked")
            # 복구하면 다시 200 — 취소는 soft
            assert app_db(app).set_device_revoked(dev_id, None)
            st3, _ = await get(client, "/api/market/ping", headers=device_auth(token))
            assert st3 == 200
        finally:
            await client.close()
    _run(scn())


def test_ping_admin_secret_on_direct_connection(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            st, data = await get(client, "/api/market/ping", headers=AUTH)
            assert st == 200 and (data["device_id"], data["label"]) == ("admin", "")
        finally:
            await client.close()
    _run(scn())


def test_ping_shares_the_device_upload_rate_limit(tmp_path):
    """업로드 버킷을 같이 쓴다 — selftest 가 폭주해도 기기 단위 상한 안에 있다(HUB-PROTOCOL §3-7)."""
    async def scn():
        app, client = await start_client(make_cfg(tmp_path, upload_limit_per_min=2))
        try:
            _, token = await _registered(client)
            hdr = device_auth(token)
            assert (await get(client, "/api/market/ping", headers=hdr))[0] == 200
            assert (await get(client, "/api/market/ping", headers=hdr))[0] == 200
            resp = await client.get("/api/market/ping", headers=hdr)
            assert resp.status == 429 and (await resp.json())["error"] == "rate_limited"
            assert int(resp.headers["Retry-After"]) >= 1
        finally:
            await client.close()
    _run(scn())
