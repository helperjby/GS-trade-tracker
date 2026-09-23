"""hub HTTP API — 인증 · 검증(전건 거부) · 수집·dedup · upsert · 이름 학습 · 검색 정규화 · 신선도 · 증분 목록 · 통계."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import warnings

import pytest
from aiohttp import web

import db as db_mod
from helpers import AUTH, app_db, body, get, make_cfg, obs, post, row, start_client


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
                # i64 밖 — 검증에서 잡아야 sqlite OverflowError → 500(관측기가 그 배치를 영원히 재시도)이 안 난다
                (row(price=2**63), "rows[0].price"),
                (row(item_id=-(2**63) - 1), "rows[0].item_id"),
                (row(flag45=2**63), "rows[0].flag45"),
                # 문자열 상한 — seller 는 PK 일부, item_name 은 학습 표·정규화 키로 복사된다
                (row(seller="판" * 129), "rows[0].seller"),
                (row(item_name="이" * 129), "rows[0].item_name"),
                (row(category="c" * 33), "rows[0].category"),
            ]
            for r, field in bad_rows:
                st, data = await post(client, body(obs(obs_id="good"), obs(obs_id="bad", rows=[r])))
                assert st == 400 and data["ok"] is False, (field, data)
                assert (data["index"], data["field"]) == (1, field), (field, data)
            st, data = await post(client, body(obs(agent_ts="1000")))
            assert (st, data["index"], data["field"]) == (400, 0, "agent_ts")
            # 시계가 리셋·폭주한 기기 — 200 을 주면 어떤 조회에도 안 보이고 prune 에 사라진다 → 400 으로 격리시킨다
            now = time.time()
            st, data = await post(client, body(obs(obs_id="good"), obs(obs_id="clock", agent_ts=0.0)))
            assert (st, data["error"], data["index"], data["field"]) == (400, "agent_ts_out_of_range", 1, "agent_ts")
            st, data = await post(client, body(obs(agent_ts=now - 31 * 86400)))
            assert (st, data["error"]) == (400, "agent_ts_out_of_range")
            st, data = await post(client, body(obs(agent_ts=now + 2 * 86400)))
            assert (st, data["error"], data["field"]) == (400, "agent_ts_out_of_range", "agent_ts")
            st, data = await post(client, body(obs(opcode=None)))
            assert (st, data["field"]) == (400, "opcode")
            st, data = await post(client, body(obs(opcode=2**63)))
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
            # item_id 는 있으면 정확해야 한다 — 음수를 0 으로 깎거나 쓰레기를 버리고 q 만으로 답하면 틀린 결과가 조용히 나간다
            for bad in ({"item_id": "-5"}, {"item_id": "abc"}, {"item_id": "abc", "q": "봉인"},
                        {"item_id": str(2**63)}, {"item_id": "1.5", "q": "봉인"}):
                st, data = await get(client, "/api/market/search", bad)
                assert (st, data["error"], data["field"]) == (400, "bad_request", "item_id"), bad
            st, data = await get(client, "/api/market/search", {"item_id": "9001", "q": "없는아이템"})
            assert (st, data["count"], data["item_id"]) == (200, 0, 9001)   # AND 필터
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
            assert (data["count"], data["total_matches"], data["expiry_days"]) == (3, 4, 2)
            assert [l["price"] for l in data["listings"]] == [200, 300, 400]  # 단가 오름차순
            assert all(l["last_seen_ts"] <= data["server_time"] for l in data["listings"])  # 미래 agent_ts → 서버 시각
            # max_age_sec 를 아무리 늘려도 소멸 상한(첫 관측일+2 00:00 KST)이 지난 행은 안 나온다 — 만료 필터가 1차
            st, data = await get(client, "/api/market/search", {"q": "봉인의돌", "max_age_sec": "1e9"})
            assert data["count"] == 3 and data["listings"][0]["price"] == 200
            st, data = await get(client, "/api/market/search", {"q": "봉인의돌", "max_age_sec": "0"})
            assert (data["count"], data["total_matches"]) == (0, 4)
            st, data = await get(client, "/api/market/search", {"q": "봉인의돌", "max_age_sec": "nan"})
            assert data["max_age_sec"] == 259200.0  # 쓰레기는 기본값(72h 안전망)
        finally:
            await client.close()
    _run(scn())


def _today0() -> float:
    """오늘 00:00 KST — 자정 직전이면 넘긴 뒤 잰다(테스트의 '오늘'과 서버 호출의 '오늘'이 갈리지 않게)."""
    now = time.time()
    left = db_mod.kst_midnight(now, 1) - now
    if left < 30:
        time.sleep(left + 1)
        now = time.time()
    return db_mod.kst_midnight(now, 0)


def test_search_hides_by_expiry_not_by_24h(tmp_path):
    """소멸 규칙(등록일 D → D+2 00:00 KST, 단기 고정): 관측 시각으로 낸 상한이 지나야 숨긴다 — 24h 가 지나도 살아 있는 행은
    보이고, 24h 안이라도 상한이 지난 행은 숨는다. 실제 시계를 쓰되 오늘 KST 자정 기준 오프셋이라 실행 시각과 무관하게 결정적."""
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            today0 = _today0()                                    # 오늘 00:00 KST
            await post(client, body(
                # 어제 00:00:01 관측 → 등록일 ∈ {그제, 어제} → 상한 = 내일 00:00 (살아 있음, 나이 > 24h)
                obs(obs_id="y", agent_ts=today0 - 86400 + 1, rows=[row(listing_id=41, price=100)]),
                # 그제 12:00 관측 → 상한 = 오늘 00:00 ≤ now (확실히 사라짐, 나이는 36h~60h)
                obs(obs_id="dby", agent_ts=today0 - 86400 - 43200, rows=[row(listing_id=42, price=50, seller="판매자B")]),
                # 어제 23:59:59 첫 관측 + 오늘 00:00:01 재관측(자정 걸침) → from = by = 내일 00:00 → 등록일 = 어제 확정
                obs(obs_id="s1", agent_ts=today0 - 1, rows=[row(listing_id=43, price=70, seller="판매자C")]),
                obs(obs_id="s2", agent_ts=today0 + 1, rows=[row(listing_id=43, price=70, seller="판매자C")]),
            ))
            st, data = await get(client, "/api/market/search", {"q": "봉인의돌"})
            assert st == 200 and (data["count"], data["total_matches"]) == (2, 3)
            by_id = {l["listing_id"]: l for l in data["listings"]}
            assert set(by_id) == {41, 43}
            assert by_id[41]["expires_by_ts"] == today0 + 86400          # 내일 00:00 KST
            assert by_id[41]["expires_from_ts"] == today0                # 어제 관측 → 오늘 00:00 부터 사라졌을 수 있음
            assert by_id[43]["expires_from_ts"] == by_id[43]["expires_by_ts"] == today0 + 86400  # 자정 걸침 → 확정
            # 증분 폴링은 만료 필터 없이 전부 + 같은 필드
            st, data = await get(client, "/api/market/listings", {"since_ts": "0"})
            assert data["count"] == 3 and all("expires_by_ts" in l and "expires_from_ts" in l for l in data["listings"])
            st, s = await get(client, "/api/market/stats")
            assert (s["live_listings"], s["expiry_days"], s["fresh_sec"]) == (2, 2, 259200.0)
        finally:
            await client.close()
    _run(scn())


def test_expiry_days_from_config(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path, listing_expiry_days=3))
        try:
            today0 = _today0()
            await post(client, body(obs(obs_id="dby", agent_ts=today0 - 86400 - 43200, rows=[row(listing_id=42)])))
            st, data = await get(client, "/api/market/search", {"q": "봉인의돌"})
            assert (data["count"], data["expiry_days"]) == (1, 3)                 # 장기 3일이면 그제 관측도 내일 00:00 까지
            assert data["listings"][0]["expires_by_ts"] == today0 + 86400
        finally:
            await client.close()
    _run(scn())


def test_short_max_age_warns_at_startup(tmp_path, caplog):
    """배포 config 가 예시 파일 복사본이면 옛 기본 86400 이 명시돼 있다 — 만료 전 행을 나이 필터가 먼저 숨기므로 기동 때 경고."""
    async def scn(cfg):
        with caplog.at_level("WARNING", logger="hub"):
            app, client = await start_client(cfg)
            await client.close()
    caplog.clear(); _run(scn(make_cfg(tmp_path, search_max_age_sec=86400)))
    assert any("search_max_age_sec=86400" in r.message and "259200" in r.message for r in caplog.records)
    caplog.clear(); _run(scn(make_cfg(tmp_path)))
    assert not any("search_max_age_sec" in r.message for r in caplog.records)


def test_listings_keyset_cursor_delivers_every_row_at_the_same_ts(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path, listings_limit_max=2))
        try:
            now = time.time()
            # 한 페이지 = 행 5개, 전부 같은 seen_ts — ts 만 엄격 초과하던 커서는 3·4·5번째 행을 영원히 놓쳤다.
            await post(client, body(obs(obs_id="same", agent_ts=now - 100,
                                        rows=[row(listing_id=50 + i, seller=f"판매자{i}") for i in range(5)])))
            await post(client, body(obs(obs_id="later", agent_ts=now - 50, rows=[row(listing_id=60)])))
            got, params, calls = [], {"since_ts": "0", "limit": "10"}, 0
            while True:
                st, data = await get(client, "/api/market/listings", params)
                calls += 1
                assert st == 200 and data["limit"] == 2          # limit 클램프(max 2)
                if not data["count"]:
                    assert (data["next_since_ts"], data["next_since_key"]) == (float(params["since_ts"]), params["since_key"])
                    break
                assert all(l["listing_key"] for l in data["listings"])
                got += [l["listing_id"] for l in data["listings"]]
                params = {"since_ts": repr(data["next_since_ts"]), "since_key": data["next_since_key"]}
            assert (got, calls) == ([50, 51, 52, 53, 54, 60], 4)
            # since_ts 만 주는 옛 소비자 — 같은 ts 의 행이 다시 온다(at-least-once), 유실은 없다
            st, first = await get(client, "/api/market/listings", {"since_ts": "0"})
            st, again = await get(client, "/api/market/listings", {"since_ts": repr(first["next_since_ts"])})
            assert [l["listing_id"] for l in again["listings"]] == [50, 51]
        finally:
            await client.close()
    _run(scn())


def test_item_name_latest_wins_and_null_never_overwrites(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            now = time.time()
            await post(client, body(obs(obs_id="m2", agent_ts=now - 200, rows=[row(item_name="봉인의돌")])))
            await post(client, body(obs(obs_id="m1", agent_ts=now - 300, rows=[row(item_name="옛이름")])))  # 늦게 온 옛 관측
            st, data = await get(client, "/api/market/search", {"item_id": "853"})
            assert data["listings"][0]["item_name"] == "봉인의돌"
            await post(client, body(obs(obs_id="m3", agent_ts=now - 100, rows=[row(item_name=None, quantity=3)])))  # 표 없는 PC
            st, data = await get(client, "/api/market/search", {"item_id": "853"})
            (lst,) = data["listings"]
            assert (lst["item_name"], lst["quantity"]) == ("봉인의돌", 3)
            await post(client, body(obs(obs_id="m4", agent_ts=now - 50, rows=[row(item_name="새이름")])))
            st, data = await get(client, "/api/market/search", {"q": "새이름"})
            assert data["count"] == 1
            st, data = await get(client, "/api/market/search", {"q": "옛이름"})
            assert data["total_matches"] == 0
        finally:
            await client.close()
    _run(scn())


def test_injected_database_is_not_closed_by_app(tmp_path):
    async def scn():
        ext = db_mod.Database(":memory:")
        app, client = await start_client(make_cfg(tmp_path), database=ext)
        await post(client, body(obs(obs_id="x1")))
        await client.close()          # 앱 cleanup — 주입한 DB 의 소유권은 호출자, 닫히지 않아야 한다
        assert ext.market_stats(time.time())["observations"] == 1
        app2, client2 = await start_client(make_cfg(tmp_path), database=ext)   # 같은 DB 를 다른 앱이 재사용
        try:
            st, s = await get(client2, "/api/market/stats")
            assert s["observations"] == 1
        finally:
            await client2.close()
        ext.close()
    _run(scn())


def test_no_app_key_warning_and_owned_db_closed_on_cleanup(tmp_path):
    async def scn():
        with warnings.catch_warnings():
            warnings.simplefilter("error", web.NotAppKeyWarning)
            app, client = await start_client(make_cfg(tmp_path))
            await post(client, body(obs(obs_id="w1")))
            await client.close()
        with pytest.raises(sqlite3.ProgrammingError):
            app_db(app).market_stats(0.0)      # 앱이 만든 DB 는 cleanup_ctx 가 닫는다
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
            assert s["latest_recv_ts"] <= s["server_time"] and s["fresh_sec"] == 259200.0
            assert (s["live_listings"], s["expiry_days"]) == (3, 2)
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
            con = app_db(app)._con
            stored = json.loads(con.execute(
                "SELECT payload_json FROM market_observations WHERE obs_id='u1'").fetchone()[0])
            assert stored["extra_top"] == "ignored" and stored["item_table"]["rows"] == 4001
            # 행 본문(seller·price…)은 market_listings 에만 — payload 에는 행의 미지 키만 남는다
            assert stored["rows"] == [{"quantity_hi": 0, "price_hi": 0, "unknown40": "00000000", "future_field": {"x": 1}}]
            plain = {k: v for k, v in row(listing_id=6).items() if k in db_mod.ROW_NORMALIZED_KEYS}
            await post(client, body(obs(obs_id="u2", rows=[plain])))
            stored2 = json.loads(con.execute(
                "SELECT payload_json FROM market_observations WHERE obs_id='u2'").fetchone()[0])
            assert "rows" not in stored2 and stored2["obs_id"] == "u2"
        finally:
            await client.close()
    _run(scn())
