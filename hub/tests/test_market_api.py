"""hub HTTP API — 인증 · 검증(전건 거부) · 수집·dedup · upsert · 이름 학습 · 검색 정규화 · 신선도 · 증분 목록 · 통계."""
from __future__ import annotations

import asyncio
import time

from helpers import AUTH, body, get, make_cfg, obs, post, row, start_client


def _run(coro):
    return asyncio.run(coro)


def test_bearer_required_on_api_but_not_root(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            for method, path in (("post", "/api/market/observations"), ("get", "/api/market/search?q=x"),
                                 ("get", "/api/market/listings"), ("get", "/api/market/stats")):
                resp = await getattr(client, method)(path)  # 헤더 없음
                assert resp.status == 401, path
                assert await resp.json() == {"error": "unauthorized"}
                resp = await getattr(client, method)(path, headers={"Authorization": "Bearer wrong"})
                assert resp.status == 401, path
            resp = await client.get("/")
            assert resp.status == 200
            data = await resp.json()
            assert data["service"] == "yuktracker-hub" and data["v"] == 1 and "server_time" in data
        finally:
            await client.close()
    _run(scn())


def test_post_rejects_malformed_and_writes_nothing(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path, max_observations_per_request=3))
        try:
            resp = await client.post("/api/market/observations", data=b"not json",
                                     headers={**AUTH, "Content-Type": "application/json"})
            assert resp.status == 400 and (await resp.json())["error"] == "bad_request"
            st, data = await post(client, [1, 2])
            assert (st, data["error"]) == (400, "bad_request")
            st, data = await post(client, {"observations": [obs()]})
            assert (st, data["field"]) == (400, "device_id")
            st, data = await post(client, body())
            assert (st, data["field"]) == (400, "observations")
            st, data = await post(client, body(*[obs(obs_id=f"o{i}") for i in range(4)]))
            assert (st, data["error"]) == (400, "too_many")
            bad_rows = [
                (row(price="45000000"), "rows[0].price"),
                (row(quantity=True), "rows[0].quantity"),
                (row(listing_id=-1), "rows[0].listing_id"),
                (row(seller=None), "rows[0].seller"),
                (row(item_name=123), "rows[0].item_name"),
                (row(flag45="2"), "rows[0].flag45"),
                ("행이 dict 가 아님", "rows[0]"),
            ]
            for r, field in bad_rows:
                st, data = await post(client, body(obs(obs_id="good"), obs(obs_id="bad", rows=[r])))
                assert st == 400 and data["ok"] is False, (field, data)
                assert (data["index"], data["field"]) == (1, field), (field, data)
            st, data = await post(client, body(obs(agent_ts="1000")))
            assert (st, data["index"], data["field"]) == (400, 0, "agent_ts")
            st, data = await post(client, body(obs(opcode=None)))
            assert (st, data["field"]) == (400, "opcode")
            st, data = await post(client, body(obs(page="1")))
            assert (st, data["field"]) == (400, "page")
            st, data = await post(client, body(obs(rows=[row()] * 65)))
            assert (st, data["error"], data["field"]) == (400, "too_many", "rows")
            st, data = await post(client, body(obs(obs_id="")))
            assert (st, data["field"]) == (400, "obs_id")
            # 위 요청들 중 첫 관측("good")까지 전부 거부됐어야 한다 — 아무것도 안 써짐
            st, stats = await get(client, "/api/market/stats")
            assert (stats["observations"], stats["listings"]) == (0, 0)
        finally:
            await client.close()
    _run(scn())


def test_post_batch_accept_and_dedup(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            payload = body(
                obs(obs_id="a", rows=[row(listing_id=1),
                                      row(listing_id=2, item_id=3506, item_name="봉인의서", price=1_000)]),
                obs(obs_id="b", rows=[row(listing_id=3, seller="판매자B")]))
            st, data = await post(client, payload)
            assert st == 200 and data["ok"] is True
            assert (data["accepted"], data["duplicates"], data["rows"]) == (2, 0, 3)
            # 같은 obs_id 재전송(스풀 재시도) → 중복, 상태 불변
            st, data = await post(client, payload)
            assert (data["accepted"], data["duplicates"], data["rows"]) == (0, 2, 0)
            st, data = await get(client, "/api/market/search", {"q": "봉인의"})
            assert data["count"] == 3 and all(l["seen_count"] == 1 for l in data["listings"])
            st, stats = await get(client, "/api/market/stats")
            assert (stats["observations"], stats["listings"], stats["items"]) == (2, 3, 2)
        finally:
            await client.close()
    _run(scn())


def test_upsert_latest_observation_wins_and_first_seen_kept(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            now = time.time()
            t0, t1, t2 = now - 600, now - 300, now - 100
            await post(client, body(obs(obs_id="t1", agent_ts=t1, rows=[row(quantity=10)])))
            await post(client, body(obs(obs_id="t2", agent_ts=t2,
                                        rows=[row(quantity=7, price=44_000_000)]), device_id="DEV-2"))
            st, data = await get(client, "/api/market/search", {"q": "봉인의돌"})
            (lst,) = data["listings"]
            assert (lst["quantity"], lst["price"], lst["seen_count"], lst["last_device"]) == (7, 44_000_000, 2, "DEV-2")
            assert abs(lst["first_seen_ts"] - t1) < 1e-6 and abs(lst["last_seen_ts"] - t2) < 1e-6
            # 역순 업로드(더 오래된 관측이 나중에 도착) → 상태 유지, first_seen 만 앞당김, seen_count 3
            await post(client, body(obs(obs_id="t0", agent_ts=t0, rows=[row(quantity=12, price=50_000_000)])))
            st, data = await get(client, "/api/market/search", {"q": "봉인의돌"})
            (lst,) = data["listings"]
            assert (lst["quantity"], lst["price"], lst["seen_count"], lst["last_device"]) == (7, 44_000_000, 3, "DEV-2")
            assert abs(lst["first_seen_ts"] - t0) < 1e-6 and abs(lst["last_seen_ts"] - t2) < 1e-6
            # 같은 등록 id 라도 아이템·판매자가 다르면 다른 키
            await post(client, body(obs(obs_id="t3", rows=[row(item_id=3506, item_name="봉인의서")])))
            st, stats = await get(client, "/api/market/stats")
            assert stats["listings"] == 2
        finally:
            await client.close()
    _run(scn())


def test_item_name_learned_from_other_device(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            now = time.time()
            # DEV-1 은 표가 없어 이름 null → 이름 검색엔 안 잡히고 id 검색엔 잡힌다
            await post(client, body(obs(obs_id="n1", agent_ts=now - 50, rows=[row(listing_id=11, item_name=None)])))
            st, data = await get(client, "/api/market/search", {"q": "봉인의돌"})
            assert (data["count"], data["total_matches"]) == (0, 0)
            st, data = await get(client, "/api/market/search", {"item_id": "853"})
            assert data["count"] == 1 and data["listings"][0]["item_name"] is None
            # DEV-2 가 같은 item_id 를 이름과 함께 → 학습 표 → DEV-1 행도 이름으로 검색·표시된다
            await post(client, body(obs(obs_id="n2", agent_ts=now - 40,
                                        rows=[row(listing_id=12, item_name="봉인의돌", seller="판매자B")]),
                                    device_id="DEV-2"))
            st, data = await get(client, "/api/market/search", {"q": "봉인의돌"})
            assert data["count"] == 2 and {l["item_name"] for l in data["listings"]} == {"봉인의돌"}
            st, stats = await get(client, "/api/market/stats")
            assert stats["item_names"] == 1
        finally:
            await client.close()
    _run(scn())


def test_search_normalization_and_params(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            await post(client, body(obs(obs_id="s1", rows=[
                row(listing_id=21, item_name="봉인의돌"),
                row(listing_id=22, item_id=9001, item_name="Test Sword", price=10),
                row(listing_id=23, item_id=9002, item_name="정기의구슬(風)", price=20),
            ])))
            cases = (("봉인의 돌", 1), ("봉인의돌", 1), ("  봉인  의돌 ", 1), ("testsword", 1),
                     ("TEST SWORD", 1), ("구슬(風)", 1), ("없는아이템", 0), ("의", 2))
            for q, expect in cases:
                st, data = await get(client, "/api/market/search", {"q": q})
                assert (st, data["count"]) == (200, expect), (q, data)
            st, data = await get(client, "/api/market/search", {"q": "   "})
            assert (st, data["field"]) == (400, "q")
            st, data = await get(client, "/api/market/search")
            assert st == 400
            st, data = await get(client, "/api/market/search", {"item_id": "9001"})
            assert data["count"] == 1 and data["listings"][0]["item_name"] == "Test Sword" and data["q_norm"] == ""
            # limit 클램프: max 100, 최소 1, 쓰레기는 기본 20
            st, data = await get(client, "/api/market/search", {"q": "의", "limit": "200"})
            assert data["limit"] == 100
            st, data = await get(client, "/api/market/search", {"q": "의", "limit": "0"})
            assert (data["limit"], data["count"], data["total_matches"]) == (1, 1, 2)
            st, data = await get(client, "/api/market/search", {"q": "의", "limit": "abc"})
            assert data["limit"] == 20
        finally:
            await client.close()
    _run(scn())


def test_search_freshness_ordering_and_clock_clamp(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            now = time.time()
            await post(client, body(
                obs(obs_id="f1", agent_ts=now - 3 * 86400, rows=[row(listing_id=31, price=100)]),  # 오래됨
                obs(obs_id="f2", agent_ts=now - 60, rows=[row(listing_id=32, price=300, seller="판매자B"),
                                                          row(listing_id=33, price=200, seller="판매자C")]),
                obs(obs_id="f3", agent_ts=now + 3600, rows=[row(listing_id=34, price=400, seller="판매자D")]),  # 앞선 시계
            ))
            st, data = await get(client, "/api/market/search", {"q": "봉인의돌"})
            assert (data["count"], data["total_matches"]) == (3, 4)
            assert [l["price"] for l in data["listings"]] == [200, 300, 400]  # 단가 오름차순
            assert all(l["last_seen_ts"] <= data["server_time"] for l in data["listings"])  # 미래 agent_ts → 서버 시각
            st, data = await get(client, "/api/market/search", {"q": "봉인의돌", "max_age_sec": "1e9"})
            assert data["count"] == 4 and data["listings"][0]["price"] == 100
            st, data = await get(client, "/api/market/search", {"q": "봉인의돌", "max_age_sec": "0"})
            assert (data["count"], data["total_matches"]) == (0, 4)
            st, data = await get(client, "/api/market/search", {"q": "봉인의돌", "max_age_sec": "nan"})
            assert data["max_age_sec"] == 86400.0  # 쓰레기는 기본값
        finally:
            await client.close()
    _run(scn())


def test_listings_incremental(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path, listings_limit_max=2))
        try:
            now = time.time()
            for i in range(4):
                await post(client, body(obs(obs_id=f"l{i}", agent_ts=now - 100 + i * 10,
                                            rows=[row(listing_id=40 + i, seller=f"판매자{i}")])))
            st, data = await get(client, "/api/market/listings", {"since_ts": "0", "limit": "10"})
            assert (data["limit"], data["count"]) == (2, 2)  # limit 클램프(max 2)
            assert [l["listing_id"] for l in data["listings"]] == [40, 41]
            since = data["next_since_ts"]
            st, data = await get(client, "/api/market/listings", {"since_ts": repr(since)})
            assert [l["listing_id"] for l in data["listings"]] == [42, 43]  # 엄격 초과 — 41 은 다시 안 온다
            since = data["next_since_ts"]
            st, data = await get(client, "/api/market/listings", {"since_ts": repr(since)})
            assert data["count"] == 0 and data["next_since_ts"] == since
        finally:
            await client.close()
    _run(scn())


def test_stats_shape_and_devices(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            await post(client, body(obs(obs_id="d1"),
                                    obs(obs_id="d2", rows=[row(listing_id=2, item_id=3506, item_name="봉인의서")])))
            await post(client, body(obs(obs_id="d3", rows=[row(listing_id=3, seller="판매자Z")]), device_id="DEV-2"))
            st, s = await get(client, "/api/market/stats")
            assert st == 200 and s["v"] == 1
            assert (s["observations"], s["listings"], s["items"], s["item_names"], s["fresh_listings"]) == (3, 3, 2, 2, 3)
            assert s["latest_recv_ts"] <= s["server_time"] and s["fresh_sec"] == 86400.0
            assert [(d["device_id"], d["observations"]) for d in s["devices"]] == [("DEV-1", 2), ("DEV-2", 1)]
        finally:
            await client.close()
    _run(scn())


def test_unknown_keys_tolerated_and_payload_kept(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            o = obs(obs_id="u1", extra_top="ignored", rows=[row(listing_id=5, future_field={"x": 1})])
            st, data = await post(client, {**body(o), "unknown": True})
            assert st == 200 and data["accepted"] == 1
            raw = app["db"]._con.execute(
                "SELECT payload_json FROM market_observations WHERE obs_id='u1'").fetchone()[0]
            assert '"extra_top"' in raw and '"future_field"' in raw
        finally:
            await client.close()
    _run(scn())
