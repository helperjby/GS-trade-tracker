"""hub 기기 등록·기기 토큰·공개/직접 구분(리스너 둘 + 헤더)·속도제한 — HUB-PROTOCOL §0·§3-0·§3-1·§3-6."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import socket
import time

import aiohttp

import server as server_mod
from helpers import (AUTH, INVITE, PROXY, SECRET, app_db, body, device_auth, get, make_cfg, obs, post, register,
                     start_client)

READ_ROUTES = ("/api/market/search?q=x", "/api/market/listings", "/api/market/stats")


def _run(coro):
    return asyncio.run(coro)


def test_register_happy_path_no_bearer_and_token_hashed(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            st, data = await register(client, label="  DESKTOP-ABC ", headers=PROXY, extra_key="ignored")
            assert st == 200 and data["ok"] is True and data["v"] == 1 and "server_time" in data
            assert re.fullmatch(r"d-[0-9a-f]{10}", data["device_id"])
            assert len(data["token"]) >= 40
            (d,) = app_db(app).list_devices()
            assert (d["device_id"], d["label"], d["created_ip"]) == (data["device_id"], "DESKTOP-ABC", "203.0.113.7")
            assert d["revoked_ts"] is None and d["upload_count"] == 0 and d["last_seen_ts"] is None
            assert "token_hash" not in d          # list 는 해시를 싣지 않는다
            stored = app_db(app)._con.execute("SELECT token_hash FROM devices").fetchone()[0]
            assert stored == hashlib.sha256(data["token"].encode()).hexdigest()
            # 재등록(재설치·토큰 분실) = 새 기기 — id 가 다르고 옛 행은 남는다
            st2, data2 = await register(client, label="DESKTOP-ABC")
            assert st2 == 200 and data2["device_id"] != data["device_id"]
            # label 생략·null 은 빈 문자열, 직접 접속의 created_ip 는 소켓 peer; 초대 코드 앞뒤 공백은 벗긴다(콘솔 붙여넣기)
            st3, data3 = await register(client, invite=f"  {INVITE} ", label=None)
            assert st3 == 200
            d3 = [d for d in app_db(app).list_devices() if d["device_id"] == data3["device_id"]][0]
            assert d3["label"] == "" and d3["created_ip"] == "127.0.0.1"
            assert app_db(app).count_active_devices() == 3
        finally:
            await client.close()
    _run(scn())


def test_register_bad_invite_validation_full_and_closed(tmp_path):
    async def scn():
        # 시도 12회 — 등록 IP 상한(기본 10/h)은 별도 테스트, 여기선 넉넉히
        app, client = await start_client(make_cfg(tmp_path, max_devices=1, register_limit_per_hour=100))
        try:
            st, data = await register(client, invite="wrong-code")
            assert (st, data["error"]) == (401, "bad_invite")
            resp = await client.post("/api/market/register", data=b"nope", headers={"Content-Type": "application/json"})
            assert resp.status == 400 and (await resp.json())["error"] == "bad_request"
            for bad in (123, "", "   "):
                st, data = await register(client, invite=bad)
                assert (st, data["field"]) == (400, "invite_code"), bad
            for bad in ("x" * 65, "esc\x1b[31m", "tab\tbed", 7):
                st, data = await register(client, label=bad)
                assert (st, data["field"]) == (400, "label"), bad
            assert app_db(app).count_active_devices() == 0   # 거부는 아무것도 안 만든다
            # 3일 전에 등록만 하고 한 번도 안 올린 행도 자리를 차지한다 — 자동 제외가 없다(사용자 결정)
            orphan = app_db(app).create_device("orphan", "h-orphan", time.time() - 3 * 86400, None)
            st, data = await register(client)
            assert (st, data["error"]) == (403, "registration_full")   # 미업로드 1대만으로 정원이 찬다
            # 관리자가 빼야 자리가 난다
            app_db(app).set_device_revoked(orphan, time.time())
            st, first = await register(client)
            assert st == 200
            st, data = await register(client)
            assert (st, data["error"]) == (403, "registration_full")
            app_db(app).set_device_revoked(first["device_id"], time.time())
            st, data = await register(client)
            assert st == 200                                            # 제거된 기기는 정원에서 빠진다
            st, s = await get(client, "/api/market/stats")
            # registered = 미제거(새 기기 1) / revoked = orphan + first, devices_max = 설정된 정원
            assert (s["devices_registered"], s["devices_revoked"], s["devices_max"]) == (1, 2, 1)
            assert orphan in [d["device_id"] for d in app_db(app).list_devices()]
            # 정원 초과 뒤 잘못된 코드 → 코드 검사가 정원보다 먼저(401)
            st, data = await register(client, invite="wrong-code")
            assert (st, data["error"]) == (401, "bad_invite")
        finally:
            await client.close()
        app2, client2 = await start_client(make_cfg(tmp_path / "closed", invite_code=""))
        try:
            st, data = await register(client2)
            assert (st, data["error"]) == (403, "registration_closed")
            st, data = await register(client2, invite="")             # 닫힘이 본문 검사보다 먼저
            assert (st, data["error"]) == (403, "registration_closed")
        finally:
            await client2.close()
    _run(scn())


def test_register_body_size_limited_before_parse(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            big = b'{"invite_code":"' + b"x" * 5000 + b'"}'
            resp = await client.post("/api/market/register", data=big, headers={"Content-Type": "application/json"})
            assert resp.status == 413 and (await resp.json())["error"] == "too_large"
            resp = await client.post("/api/market/register", data=b"{}", chunked=True,
                                     headers={"Content-Type": "application/json"})
            assert resp.status == 411 and (await resp.json())["error"] == "length_required"
            assert app_db(app).list_devices() == []
        finally:
            await client.close()
    _run(scn())


def test_register_rate_limited_per_ip_uses_last_xff_entry(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path, register_limit_per_hour=2))
        try:
            for _ in range(2):
                st, _data = await register(client, headers=PROXY)
                assert st == 200
            resp = await client.post("/api/market/register", json={"invite_code": "brute"}, headers=PROXY)
            assert resp.status == 429
            data = await resp.json()
            assert data["error"] == "rate_limited" and data["ok"] is False
            assert int(resp.headers["Retry-After"]) == data["retry_after"] >= 1
            # 앞 항목 위조는 같은 버킷 — 마지막 항목(프록시가 붙인 원 IP)으로 센다
            spoof = {**PROXY, "X-Forwarded-For": "198.51.100.9, 203.0.113.7"}
            st, data = await register(client, headers=spoof)
            assert (st, data["error"]) == (429, "rate_limited")
            # 다른 원 IP 는 다른 버킷
            st, data = await register(client, headers={**PROXY, "X-Forwarded-For": "198.51.100.9"})
            assert st == 200
            # 직접 접속(프록시 헤더 없음)은 소켓 peer 버킷 — X-Forwarded-For 를 스스로 붙여도 무시된다
            st, data = await register(client, headers={"X-Forwarded-For": "203.0.113.7"})
            assert st == 200
            assert app_db(app).list_devices()[-1]["created_ip"] == "127.0.0.1"
            # 공개인데 X-Forwarded-For 가 없으면 'public:?' 한 버킷(브리지 IP 와 섞이지 않는다)
            st, data = await register(client, headers={"Tailscale-Funnel-Request": "?1"})
            assert st == 200 and app_db(app).list_devices()[-1]["created_ip"] == "public:?"
            # 등록 429 는 기기를 만들지 않는다
            assert len(app_db(app).list_devices()) == 5
        finally:
            await client.close()
    _run(scn())


def test_device_token_upload_ok_counts_and_stats_label(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            st, reg = await register(client, label="PC-1", headers=PROXY)
            dev_id, tok = reg["device_id"], reg["token"]
            st, data = await post(client, body(obs(obs_id="k1"), device_id=dev_id), headers={**device_auth(tok), **PROXY})
            assert st == 200 and data["accepted"] == 1
            st, data = await post(client, body(obs(obs_id="k1"), device_id=dev_id), headers=device_auth(tok))
            assert st == 200 and data["duplicates"] == 1              # 재전송(중복)도 200 — 인증된 업로드로 센다
            (d,) = app_db(app).list_devices()
            assert d["upload_count"] == 2 and d["last_seen_ts"] is not None
            st, s = await get(client, "/api/market/stats")
            (entry,) = s["devices"]
            assert (entry["device_id"], entry["label"], entry["observations"]) == (dev_id, "PC-1", 1)
            assert (s["devices_registered"], s["devices_revoked"]) == (1, 0)
            # 관리 시크릿(직접 접속) 업로드는 자유 device_id·label null 그대로
            st, data = await post(client, body(obs(obs_id="k2"), device_id="DEV-9"))
            assert st == 200
            st, s = await get(client, "/api/market/stats")
            assert [(x["device_id"], x["label"]) for x in s["devices"]] == [("DEV-9", None), (dev_id, "PC-1")]
        finally:
            await client.close()
    _run(scn())


def test_device_token_mismatch_and_400_touch_last_seen_but_write_nothing(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            st, reg = await register(client)
            dev_id, tok = reg["device_id"], reg["token"]
            st, data = await post(client, body(obs(obs_id="m1"), device_id="DEV-1"), headers=device_auth(tok))
            assert (st, data["error"], data["device_id"]) == (403, "device_mismatch", dev_id)
            (d,) = app_db(app).list_devices()
            assert d["upload_count"] == 0 and d["last_seen_ts"] is not None   # 거부돼도 last_seen — 잘못 설정된 exe 가 보인다
            seen1 = d["last_seen_ts"]
            await asyncio.sleep(0.01)
            # 400 검증이 403 보다 먼저 — 형식이 틀린 본문은 device_mismatch 로 새지 않는다; 역시 last_seen 만
            st, data = await post(client, {"v": 1, "device_id": "DEV-1"}, headers=device_auth(tok))
            assert (st, data["field"]) == (400, "observations")
            (d,) = app_db(app).list_devices()
            assert d["upload_count"] == 0 and d["last_seen_ts"] > seen1
            st, s = await get(client, "/api/market/stats")
            assert s["observations"] == 0
        finally:
            await client.close()
    _run(scn())


def test_revoked_device_403_and_unrevoke_restores(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            st, reg = await register(client)
            dev_id, tok = reg["device_id"], reg["token"]
            payload = body(obs(obs_id="r1"), device_id=dev_id)
            assert (await post(client, payload, headers=device_auth(tok)))[0] == 200
            assert app_db(app).set_device_revoked(dev_id, time.time())
            st, data = await post(client, body(obs(obs_id="r2"), device_id=dev_id), headers=device_auth(tok))
            assert (st, data["error"]) == (403, "device_revoked")
            st, s = await get(client, "/api/market/stats")
            assert (s["observations"], s["devices_registered"], s["devices_revoked"]) == (1, 0, 1)
            # 취소된 토큰으로 조회 라우트 → 그냥 401 (취소 여부를 범위 밖 라우트에서 알려주지 않는다)
            st, data = await get(client, "/api/market/stats", headers=device_auth(tok))
            assert (st, data) == (401, {"error": "unauthorized"})
            assert app_db(app).set_device_revoked(dev_id, None)
            st, data = await post(client, body(obs(obs_id="r2"), device_id=dev_id), headers=device_auth(tok))
            assert st == 200
        finally:
            await client.close()
    _run(scn())


def test_revoked_device_hammering_is_rate_limited(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path, upload_limit_per_min=2))
        try:
            st, reg = await register(client)
            dev_id, tok = reg["device_id"], reg["token"]
            app_db(app).set_device_revoked(dev_id, time.time())
            statuses = []
            for i in range(4):
                st, data = await post(client, body(obs(obs_id=f"h{i}"), device_id=dev_id), headers=device_auth(tok))
                statuses.append(st)
            assert statuses == [403, 403, 429, 429]      # 취소 검사보다 기기 한도가 먼저 — DB 조회만 반복시키지 못한다
        finally:
            await client.close()
    _run(scn())


def test_device_token_on_read_routes_is_401(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            st, reg = await register(client)
            for path in READ_ROUTES:
                st, data = await get(client, path, headers=device_auth(reg["token"]))
                assert (st, data) == (401, {"error": "unauthorized"}), path
            st, data = await post(client, body(obs()), headers=device_auth("not-a-token"))
            assert (st, data) == (401, {"error": "unauthorized"})
        finally:
            await client.close()
    _run(scn())


def test_admin_routes_via_proxy_403_before_credentials(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            for path in READ_ROUTES:
                for hdrs in ({**AUTH, **PROXY}, {"Authorization": "Bearer wrong", **PROXY}, PROXY,
                             {"tailscale-funnel-request": "", **AUTH}):     # 값·대소문자 무관, 존재만
                    st, data = await get(client, path, headers=hdrs)
                    assert (st, data) == (403, {"ok": False, "error": "not_public"}), (path, hdrs)
                # X-Forwarded-For 만으론 공개가 아니다(직접 접속 클라이언트가 붙인 것) — 관리 시크릿 정상
                st, data = await get(client, path, headers={**AUTH, "X-Forwarded-For": "203.0.113.7"})
                assert st == 200, path
            # 공개 요청의 관리 시크릿 업로드 → 401 (시크릿은 Funnel 을 타지 않는다)
            st, data = await post(client, body(obs()), headers={**AUTH, **PROXY})
            assert (st, data) == (401, {"error": "unauthorized"})
            st, s = await get(client, "/api/market/stats")
            assert s["observations"] == 0
            # GET / 는 공개로도 200
            resp = await client.get("/", headers=PROXY)
            assert resp.status == 200
            # 관리 라우트의 403 은 인증 실패로 세지 않는다 — 이후 직접 접속 정상
            st, _ = await get(client, "/api/market/stats")
            assert st == 200
        finally:
            await client.close()
        app2, client2 = await start_client(make_cfg(tmp_path / "pub", admin_public=True))
        try:
            st, data = await get(client2, "/api/market/stats", headers={**AUTH, **PROXY})
            assert st == 200
            st, data = await post(client2, body(obs()), headers={**AUTH, **PROXY})
            assert st == 200
            st, data = await get(client2, "/api/market/stats", headers={"Authorization": "Bearer wrong", **PROXY})
            assert st == 401
        finally:
            await client2.close()
    _run(scn())


def test_public_listener_treats_every_request_as_public(tmp_path):
    """공개 리스너(Funnel 대상) 앱 — 헤더가 없어도 전부 공개: 관리 시크릿 무시, 조회 403. 헤더 이름 오타·프록시 교체에
    기대지 않는 1차 방어."""
    async def scn():
        app, client = await start_client(make_cfg(tmp_path), public_only=True)
        try:
            for path in READ_ROUTES:
                st, data = await get(client, path)                                   # 올바른 시크릿, 헤더 없음
                assert (st, data) == (403, {"ok": False, "error": "not_public"}), path
            st, data = await post(client, body(obs()))                                # 관리 시크릿 업로드도 거부
            assert (st, data) == (401, {"error": "unauthorized"})
            resp = await client.get("/")
            assert resp.status == 200
            st, reg = await register(client)                                         # XFF 없음 → 'public:?'
            assert st == 200 and app_db(app).list_devices()[0]["created_ip"] == "public:?"
            st, reg2 = await register(client, headers={"X-Forwarded-For": "203.0.113.7"})   # 프록시 헤더 없이도 XFF 채택
            assert st == 200 and app_db(app).list_devices()[1]["created_ip"] == "203.0.113.7"
            st, data = await post(client, body(obs(obs_id="p1"), device_id=reg["device_id"]), headers=device_auth(reg["token"]))
            assert st == 200
        finally:
            await client.close()
        app2, client2 = await start_client(make_cfg(tmp_path / "pub", admin_public=True), public_only=True)
        try:
            st, data = await get(client2, "/api/market/stats")                        # 명시적 opt-in 만 예외
            assert st == 200
        finally:
            await client2.close()
    _run(scn())


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_serve_runs_admin_and_public_listeners_sharing_db(tmp_path):
    async def scn():
        cfg = make_cfg(tmp_path, host="127.0.0.1", port=_free_port(), public_port=_free_port())
        started = asyncio.Event()
        task = asyncio.create_task(server_mod.serve(cfg, started=started))
        await asyncio.wait_for(started.wait(), 10)
        admin, public = f"http://127.0.0.1:{cfg['port']}", f"http://127.0.0.1:{cfg['public_port']}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(public + "/api/market/stats", headers=AUTH) as r:
                    assert r.status == 403 and (await r.json())["error"] == "not_public"
                async with s.post(public + "/api/market/register", json={"v": 1, "invite_code": INVITE, "label": "L"}) as r:
                    assert r.status == 200
                    reg = await r.json()
                async with s.post(public + "/api/market/observations", json=body(obs(obs_id="s1"), device_id=reg["device_id"]),
                                  headers=device_auth(reg["token"])) as r:
                    assert r.status == 200
                async with s.get(admin + "/api/market/stats", headers=AUTH) as r:          # 같은 DB — 공개로 올린 관측이 보인다
                    assert r.status == 200
                    st = await r.json()
                    assert st["observations"] == 1 and st["devices"][0]["label"] == "L"
                async with s.get(admin + "/api/market/stats", headers={**AUTH, **PROXY}) as r:  # 관리 리스너의 헤더 2차 방어
                    assert r.status == 403
                async with s.get(admin + "/api/market/stats", headers={"Authorization": "Bearer " + SECRET + "x"}) as r:
                    assert r.status == 401
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # 리스너가 닫혔다
        with contextlib.suppress(Exception):
            async with aiohttp.ClientSession() as s:
                async with s.get(admin + "/") as r:
                    raise AssertionError(f"still listening: {r.status}")
    _run(scn())


def test_upload_rate_limit_429_per_device_not_admin(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path, upload_limit_per_min=2))
        try:
            st, reg = await register(client, label="A")
            st, reg2 = await register(client, label="B")
            for i in range(2):
                st, data = await post(client, body(obs(obs_id=f"u{i}"), device_id=reg["device_id"]),
                                      headers=device_auth(reg["token"]))
                assert st == 200
            resp = await client.post("/api/market/observations", json=body(obs(obs_id="u9"), device_id=reg["device_id"]),
                                     headers=device_auth(reg["token"]))
            assert resp.status == 429 and resp.headers["Retry-After"]
            assert (await resp.json())["error"] == "rate_limited"
            # 다른 기기는 별개 버킷, 관리 시크릿은 무제한
            st, data = await post(client, body(obs(obs_id="v1"), device_id=reg2["device_id"]), headers=device_auth(reg2["token"]))
            assert st == 200
            for i in range(3):
                st, data = await post(client, body(obs(obs_id=f"a{i}")))
                assert st == 200
            d = [x for x in app_db(app).list_devices() if x["device_id"] == reg["device_id"]][0]
            assert d["upload_count"] == 2                         # 429 는 기록되지 않는다
        finally:
            await client.close()
    _run(scn())


def test_auth_failure_storm_429_only_for_public_ip_after_token_check(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path, auth_fail_limit_per_min=3))
        try:
            st, reg = await register(client)
            good = {**device_auth(reg["token"]), **PROXY}
            wrong = {"Authorization": "Bearer wrong", **PROXY}
            for _ in range(3):
                st, data = await post(client, body(obs()), headers=wrong)
                assert (st, data) == (401, {"error": "unauthorized"})
            st, data = await post(client, body(obs()), headers=wrong)
            assert (st, data["error"]) == (429, "rate_limited")
            # 같은 공개 IP(공유 NAT) 의 올바른 기기 토큰은 막히지 않는다 — 토큰 검사가 먼저
            st, data = await post(client, body(obs(obs_id="ok0"), device_id=reg["device_id"]), headers=good)
            assert st == 200
            st, data = await post(client, body(obs()), headers=wrong)
            assert st == 429
            # 다른 공개 IP 의 실패는 별개 버킷
            st, data = await post(client, body(obs()), headers={**wrong, "X-Forwarded-For": "198.51.100.9"})
            assert st == 401
            # 직접 접속은 세지 않는다 — 브리지 IP 를 봇과 공유하므로 401 폭주가 봇을 막으면 안 된다
            for _ in range(5):
                st, data = await get(client, "/api/market/stats", headers={"Authorization": "Bearer wrong"})
                assert st == 401
            st, data = await get(client, "/api/market/stats")
            assert st == 200
        finally:
            await client.close()
    _run(scn())
