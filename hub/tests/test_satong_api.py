"""hub 사통팔달 — `POST /api/satong/observations` · `GET /api/satong/recent` (HUB-PROTOCOL §3-8).

보는 것: 하트비트(빈 배치)가 수집기 상태만 남기는가 · 재전송 중복·창 안 병합·창 밖 분리 · 허용 슬롯(`satong_slots`) 밖
기기 403 · recent 는 관리 시크릿·직접 접속 전용 · `online` 판정(하트비트 나이·클라 연결) · 검증 400 · 보존.
데이터는 합성(보낸이 `보낸이A`, 기기 `DEV-1`).
"""
from __future__ import annotations

import asyncio
import time

import pytest

import server as server_mod
from helpers import AUTH, INVITE2, PROXY, app_db, device_auth, get, make_cfg, register, start_client


def _run(coro):
    return asyncio.run(coro)


def _msg(n: int = 1, *, agent_ts: float | None = None, sender: str = "보낸이A", text: str = "합성 외치기 1",
         obs_id: str | None = None, **extra) -> dict:
    m = {"obs_id": obs_id or f"L1:{1_000_000 + n}:{n:012x}", "agent_ts": time.time() if agent_ts is None else agent_ts,
         "sender": sender, "text": text, "opcode": 0x1F82, "anomalies": [], "mid": 0, "tail": "00010000000000"}
    m.update(extra)
    return m


def _body(*messages, device_id: str = "DEV-1", server_id: int = 7, connected: bool = True,
          last_message_ts=None) -> dict:
    return {"v": 1, "device_id": device_id, "server_id": server_id,
            "collector": {"client_connected": connected, "last_message_ts": last_message_ts,
                          "messages": len(messages), "agent_ts": time.time()},
            "messages": list(messages)}


async def _post(client, payload, headers=AUTH):
    resp = await client.post("/api/satong/observations", json=payload, headers=headers)
    return resp.status, await resp.json()


async def _recent(client, headers=AUTH, **params):
    return await get(client, "/api/satong/recent", params={"server_id": "7", **params}, headers=headers)


def test_heartbeat_only_records_collector_and_no_messages(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            st, data = await _post(client, _body())
            assert st == 200 and (data["accepted"], data["merged"], data["duplicates"]) == (0, 0, 0)
            st, data = await _recent(client)
            assert st == 200 and data["count"] == 0 and data["messages"] == []
            assert data["online"] is True and data["last_message_ts"] is None
            (c,) = data["collectors"]
            assert c["device_id"] == "DEV-1" and c["client_connected"] is True and c["heartbeat_age_sec"] < 5
            # 클라가 끊기면 하트비트가 와도 오프라인
            await _post(client, _body(connected=False))
            _, data = await _recent(client)
            assert data["online"] is False and data["collectors"][0]["online"] is False
        finally:
            await client.close()
    _run(scn())


def test_stale_heartbeat_is_offline(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path, satong_offline_sec=180))
        try:
            await _post(client, _body())
            con = app_db(app)._con
            con.execute("UPDATE satong_collectors SET last_heartbeat_ts = ?", (time.time() - 181,))
            con.commit()
            _, data = await _recent(client)
            assert data["online"] is False and data["collectors"][0]["heartbeat_age_sec"] > 180
            # 다른 서버 번호의 수집기는 이 서버의 판정에 섞이지 않는다
            await _post(client, _body(server_id=8, device_id="DEV-2"))
            _, data = await _recent(client)
            assert data["online"] is False and len(data["collectors"]) == 1
        finally:
            await client.close()
    _run(scn())


def test_messages_retransmit_and_merge_rules(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            t0 = time.time() - 60
            st, data = await _post(client, _body(_msg(1, agent_ts=t0), _msg(2, agent_ts=t0 + 15, text="합성 외치기 2")))
            assert st == 200 and (data["accepted"], data["merged"], data["duplicates"]) == (2, 0, 0)
            # 같은 배치 재전송 = 중복(행·seen_count 무변경)
            st, data = await _post(client, _body(_msg(1, agent_ts=t0), _msg(2, agent_ts=t0 + 15, text="합성 외치기 2")))
            assert (data["accepted"], data["merged"], data["duplicates"]) == (0, 0, 2)
            # 다른 PC 가 같은 외치기를 3초 뒤에 올림 = 병합(다른 obs_id)
            st, data = await _post(client, _body(_msg(9, agent_ts=t0 + 3, obs_id="L2:1:000000000009"), device_id="DEV-2"))
            assert (data["accepted"], data["merged"], data["duplicates"]) == (0, 1, 0)
            # 같은 사람이 같은 글을 다음 큐 순번(15초 뒤)에 또 외침 = 새 행
            st, data = await _post(client, _body(_msg(3, agent_ts=t0 + 30)))
            assert (data["accepted"], data["merged"]) == (1, 0)
            _, data = await _recent(client)
            assert [m["text"] for m in data["messages"]] == ["합성 외치기 1", "합성 외치기 2", "합성 외치기 1"]
            ids = [m["id"] for m in data["messages"]]
            assert ids == sorted(ids)
            first = data["messages"][0]
            assert first["seen_count"] == 2 and first["first_seen_ts"] == pytest.approx(t0)
            assert first["last_seen_ts"] == pytest.approx(t0 + 3)
            assert data["last_message_ts"] == pytest.approx(t0 + 30)
            # 진단 키는 payload 에만(열에 없는 키) — 응답 행에는 싣지 않는다
            assert set(first) == {"id", "sender", "text", "first_seen_ts", "last_seen_ts", "seen_count"}
            (payload,) = app_db(app)._con.execute(
                "SELECT payload_json FROM satong_messages WHERE id = ?", (first["id"],)).fetchone()
            assert '"tail"' in payload and '"sender"' not in payload
        finally:
            await client.close()
    _run(scn())


def test_recent_since_id_and_latest_window(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            t0 = time.time() - 600
            msgs = [_msg(k, agent_ts=t0 + 15 * k, text=f"합성 {k}") for k in range(5)]
            await _post(client, _body(*msgs))
            _, data = await _recent(client, limit="2")
            assert [m["text"] for m in data["messages"]] == ["합성 3", "합성 4"]     # 가장 최근 2건, 오름차순
            first_id = data["messages"][0]["id"]
            _, data = await _recent(client, since_id=str(first_id - 2), limit="2")
            assert [m["text"] for m in data["messages"]] == ["합성 2", "합성 3"]
            _, data = await _recent(client, since_id=str(data["messages"][-1]["id"] + 1))
            assert data["count"] == 0
            st, data = await _recent(client, since_id="abc")
            assert (st, data["field"]) == (400, "since_id")
            resp = await client.get("/api/satong/recent", headers=AUTH)
            assert resp.status == 400 and (await resp.json())["field"] == "server_id"
        finally:
            await client.close()
    _run(scn())


def test_device_token_needs_satong_slot(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path, satong_slots=["slot1"]), public_only=True)
        try:
            st, d1 = await register(client, label="PC-1", headers=PROXY)
            assert st == 200 and d1["slot"] == "slot1"
            st, d2 = await register(client, invite=INVITE2,
                                    label="PC-2", headers=PROXY)
            assert st == 200 and d2["slot"] == "slot2"
            hdr1 = {**device_auth(d1["token"]), **PROXY}
            hdr2 = {**device_auth(d2["token"]), **PROXY}
            st, data = await _post(client, _body(_msg(1), device_id=d1["device_id"]), headers=hdr1)
            assert st == 200 and data["accepted"] == 1
            # 허용 슬롯 밖 = 403, 아무것도 안 쓴다
            st, data = await _post(client, _body(_msg(2, text="가짜"), device_id=d2["device_id"]), headers=hdr2)
            assert (st, data["error"]) == (403, "satong_not_allowed")
            # 다른 기기 id 를 사칭 = device_mismatch
            st, data = await _post(client, _body(_msg(3), device_id="DEV-X"), headers=hdr1)
            assert (st, data["error"], data["device_id"]) == (403, "device_mismatch", d1["device_id"])
            assert app_db(app).satong_recent(7, 10)[0]["text"] == "합성 외치기 1"
            assert len(app_db(app).satong_recent(7, 10)) == 1
            # 기기 행: 업로드 1회 기록, 거부도 last_seen
            devs = {d["device_id"]: d for d in app_db(app).list_devices()}
            assert devs[d1["device_id"]]["upload_count"] == 1
            assert devs[d2["device_id"]]["last_seen_ts"] is not None and devs[d2["device_id"]]["upload_count"] == 0
            # recent 는 공개 리스너에서 막힌다(관리 시크릿·기기 토큰 무관)
            st, data = await _recent(client, headers=hdr1)
            assert (st, data["error"]) == (403, "not_public")
        finally:
            await client.close()
    _run(scn())


def test_empty_satong_slots_blocks_device_tokens_but_admin_direct_works(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            st, d1 = await register(client, label="PC-1")
            st, data = await _post(client, _body(_msg(1), device_id=d1["device_id"]), headers=device_auth(d1["token"]))
            assert (st, data["error"]) == (403, "satong_not_allowed")
            st, data = await _post(client, _body(_msg(1)))            # 관리 시크릿, 직접 접속
            assert st == 200 and data["accepted"] == 1
            resp = await client.post("/api/satong/observations", json=_body(_msg(2)))
            assert resp.status == 401
        finally:
            await client.close()
    _run(scn())


@pytest.mark.parametrize("mutate, field", [
    (lambda b: b.pop("collector"), "collector"),
    (lambda b: b["collector"].pop("client_connected"), "collector.client_connected"),
    (lambda b: b["collector"].update(client_connected=1), "collector.client_connected"),
    (lambda b: b.update(server_id=-1), "server_id"),
    (lambda b: b.update(server_id=True), "server_id"),
    (lambda b: b.update(messages=None), "messages"),
    (lambda b: b["messages"][0].update(sender="  "), "sender"),
    (lambda b: b["messages"][0].update(sender="가" * 65), "sender"),
    (lambda b: b["messages"][0].update(text=None), "text"),
    (lambda b: b["messages"][0].update(agent_ts=1.0), "agent_ts"),
    (lambda b: b["messages"][0].pop("obs_id"), "obs_id"),
])
def test_validation_rejects_whole_batch(tmp_path, mutate, field):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            payload = _body(_msg(1))
            mutate(payload)
            st, data = await _post(client, payload)
            assert st == 400 and data["field"] == field
            assert app_db(app).satong_recent(7, 10) == [] and app_db(app).satong_collectors(7) == []
        finally:
            await client.close()
    _run(scn())


def test_empty_text_is_accepted(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            st, data = await _post(client, _body(_msg(1, text="")))
            assert st == 200 and data["accepted"] == 1
        finally:
            await client.close()
    _run(scn())


def test_too_many_messages(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path, max_satong_messages_per_request=2))
        try:
            st, data = await _post(client, _body(_msg(1), _msg(2), _msg(3)))
            assert (st, data["error"]) == (400, "too_many")
        finally:
            await client.close()
    _run(scn())


def test_prune_satong(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            now = time.time()
            await _post(client, _body(_msg(1, agent_ts=now - 6.9 * 86400), _msg(2, agent_ts=now, text="최근")))
            db = app_db(app)
            # 메시지는 last_seen_ts 기준, obs_id 는 recv_ts 기준 — 지금 받았으니 obs_id 는 아직 남는다
            assert db.prune_satong(now + 86400, 7) == (1, 0)
            assert [m["text"] for m in db.satong_recent(7, 10)] == ["최근"]
            assert db.prune_satong(now + 8 * 86400, 7) == (1, 2)
        finally:
            await client.close()
    _run(scn())


def test_config_satong_slots_validation(tmp_path):
    p = tmp_path / "config.json"

    def load(**over):
        import json
        raw = {"secret": "x" * 16, "invite_codes": {"slot1": "code-0001"}, **over}
        p.write_text(json.dumps(raw), encoding="utf-8")
        return server_mod.load_config(str(p), env={})

    assert load()["satong_slots"] == []
    assert load(satong_slots=[" slot1 ", "slot1"])["satong_slots"] == ["slot1"]
    with pytest.raises(SystemExit, match="invite_codes"):
        load(satong_slots=["slot9"])
    with pytest.raises(SystemExit, match="목록"):
        load(satong_slots="slot1")
    with pytest.raises(SystemExit, match="retention_satong_days"):
        load(retention_satong_days=0)
