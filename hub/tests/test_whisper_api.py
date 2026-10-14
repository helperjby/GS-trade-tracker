"""hub 귓속말 중계 — `POST /api/whisper/observations` · `GET /api/whisper/recent` (HUB-PROTOCOL §3-9).

보는 것: 업로드는 **직접 접속 전용**(공개 리스너·Funnel 헤더면 자격과 무관하게 403 not_public) · 허용 슬롯(`whisper_slots`)
밖 기기 403 · 재전송 dedup · 미지 키는 저장 안 함 · recent 의 since_id·기준점 · 검증 400(배치 전체 거부) · 보존(시간 단위) ·
속도제한 버킷 분리. 데이터는 합성(받는이 `캐릭A`, 보낸이 `보낸이A`, 기기 `DEV-1`).
"""
from __future__ import annotations

import asyncio
import time

import pytest

import server as server_mod
from helpers import AUTH, INVITE2, PROXY, app_db, device_auth, get, make_cfg, register, start_client


def _run(coro):
    return asyncio.run(coro)


def _msg(n: int = 1, *, agent_ts: float | None = None, receiver: str = "캐릭A", sender: str = "보낸이A",
         text: str = "합성 귓속말 1", obs_id: str | None = None, **extra) -> dict:
    m = {"obs_id": obs_id or f"w-{n:032x}", "agent_ts": time.time() if agent_ts is None else agent_ts,
         "receiver": receiver, "sender": sender, "text": text}
    m.update(extra)
    return m


def _body(*messages, device_id: str = "DEV-1") -> dict:
    return {"v": 1, "device_id": device_id, "messages": list(messages)}


async def _post(client, payload, headers=AUTH):
    resp = await client.post("/api/whisper/observations", json=payload, headers=headers)
    return resp.status, await resp.json()


async def _recent(client, headers=AUTH, **params):
    return await get(client, "/api/whisper/recent", params=params or None, headers=headers)


def test_upload_dedup_and_recent(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            t0 = time.time() - 60
            st, data = await _post(client, _body(_msg(1, agent_ts=t0), _msg(2, agent_ts=t0 + 5, text="합성 귓속말 2")))
            assert st == 200 and (data["accepted"], data["duplicates"]) == (2, 0)
            st, data = await _post(client, _body(_msg(1, agent_ts=t0)))
            assert st == 200 and (data["accepted"], data["duplicates"]) == (0, 1)
            # 같은 글을 다시 보내면(다른 obs_id) 병합 없이 새 건
            st, data = await _post(client, _body(_msg(3, agent_ts=t0 + 10)))
            assert data["accepted"] == 1
            st, data = await _recent(client)
            assert st == 200 and data["count"] == 3
            m = data["messages"][0]
            assert set(m) == {"id", "receiver", "sender", "text", "seen_ts", "recv_ts"}
            assert (m["receiver"], m["sender"], m["text"]) == ("캐릭A", "보낸이A", "합성 귓속말 1")
            assert m["seen_ts"] == pytest.approx(t0)
            assert [x["text"] for x in data["messages"]] == ["합성 귓속말 1", "합성 귓속말 2", "합성 귓속말 1"]
        finally:
            await client.close()
    _run(scn())


def test_recent_since_id_and_latest_window(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            t0 = time.time() - 600
            await _post(client, _body(*[_msg(k, agent_ts=t0 + k, text=f"합성 {k}") for k in range(5)]))
            _, data = await _recent(client, limit="2")
            assert [m["text"] for m in data["messages"]] == ["합성 3", "합성 4"]   # 가장 최근 2건, 오름차순
            first_id = data["messages"][0]["id"]
            _, data = await _recent(client, since_id=str(first_id - 2), limit="2")
            assert [m["text"] for m in data["messages"]] == ["합성 2", "합성 3"]
            _, data = await _recent(client, since_id=str(first_id + 1))
            assert data["count"] == 0
            st, data = await _recent(client, since_id="abc")
            assert (st, data["field"]) == (400, "since_id")
        finally:
            await client.close()
    _run(scn())


def test_unknown_keys_are_not_stored(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            st, _ = await _post(client, _body(_msg(1, slot=2, pid=1234, raw="deadbeef")))
            assert st == 200
            cols = [r[1] for r in app_db(app)._con.execute("PRAGMA table_info(whisper_messages)").fetchall()]
            assert "payload_json" not in cols
        finally:
            await client.close()
    _run(scn())


def test_public_requests_are_refused_before_auth(tmp_path):
    """사적 메시지 업로드는 Funnel 로 열지 않는다 — 공개 리스너·Funnel 헤더면 유효한 기기 토큰·관리 시크릿이어도 403."""
    async def scn():
        cfg = make_cfg(tmp_path, whisper_slots=["slot1"], admin_public=True)
        app, client = await start_client(cfg, public_only=True)
        try:
            st, d1 = await register(client, label="PC-1", headers=PROXY)
            assert st == 200
            for hdr in ({**device_auth(d1["token"]), **PROXY}, {**AUTH, **PROXY}, PROXY):
                st, data = await _post(client, _body(_msg(1), device_id=d1["device_id"]), headers=hdr)
                assert (st, data["error"]) == (403, "not_public")
            assert app_db(app).whisper_recent(10) == []
        finally:
            await client.close()
        # 관리 리스너라도 Funnel 헤더가 붙으면 공개로 본다
        app, client = await start_client(make_cfg(tmp_path, whisper_slots=["slot1"]))
        try:
            st, data = await _post(client, _body(_msg(1)), headers={**AUTH, **PROXY})
            assert (st, data["error"]) == (403, "not_public")
        finally:
            await client.close()
    _run(scn())


def test_device_token_needs_whisper_slot(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path, whisper_slots=["slot1"]))
        try:
            st, d1 = await register(client, label="PC-1")
            st, d2 = await register(client, invite=INVITE2, label="PC-2")
            hdr1, hdr2 = device_auth(d1["token"]), device_auth(d2["token"])
            st, data = await _post(client, _body(_msg(1), device_id=d1["device_id"]), headers=hdr1)
            assert st == 200 and data["accepted"] == 1
            st, data = await _post(client, _body(_msg(2), device_id=d2["device_id"]), headers=hdr2)
            assert (st, data["error"]) == (403, "whisper_not_allowed")
            st, data = await _post(client, _body(_msg(3), device_id="DEV-X"), headers=hdr1)
            assert (st, data["error"], data["device_id"]) == (403, "device_mismatch", d1["device_id"])
            assert len(app_db(app).whisper_recent(10)) == 1
            devs = {d["device_id"]: d for d in app_db(app).list_devices()}
            assert devs[d1["device_id"]]["upload_count"] == 1
            assert devs[d2["device_id"]]["upload_count"] == 0 and devs[d2["device_id"]]["last_seen_ts"] is not None
            # recent 는 관리 시크릿 전용 — 기기 토큰은 401
            st, _ = await _recent(client, headers=hdr1)
            assert st == 401
        finally:
            await client.close()
    _run(scn())


def test_empty_whisper_slots_blocks_device_tokens(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            st, d1 = await register(client, label="PC-1")
            st, data = await _post(client, _body(_msg(1), device_id=d1["device_id"]), headers=device_auth(d1["token"]))
            assert (st, data["error"]) == (403, "whisper_not_allowed")
            resp = await client.post("/api/whisper/observations", json=_body(_msg(1)))
            assert resp.status == 401
        finally:
            await client.close()
    _run(scn())


@pytest.mark.parametrize("mutate, field", [
    (lambda b: b.update(messages=[]), "messages"),
    (lambda b: b.update(messages=None), "messages"),
    (lambda b: b.update(device_id=""), "device_id"),
    (lambda b: b["messages"][0].update(receiver="  "), "receiver"),
    (lambda b: b["messages"][0].pop("receiver"), "receiver"),
    (lambda b: b["messages"][0].update(sender="\n"), "sender"),
    (lambda b: b["messages"][0].update(sender="가" * 65), "sender"),
    (lambda b: b["messages"][0].update(text=None), "text"),
    (lambda b: b["messages"][0].update(text="가" * 257), "text"),
    (lambda b: b["messages"][0].update(agent_ts=True), "agent_ts"),
    (lambda b: b["messages"][0].pop("obs_id"), "obs_id"),
])
def test_validation_rejects_whole_batch(tmp_path, mutate, field):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            payload = _body(_msg(1), _msg(2))
            mutate(payload)
            st, data = await _post(client, payload)
            assert st == 400 and data["field"] == field
            assert app_db(app).whisper_recent(10) == []
        finally:
            await client.close()
    _run(scn())


def test_empty_text_and_control_characters(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            st, data = await _post(client, _body(_msg(1, text=""), _msg(2, sender=" 보낸\x1b[2J이 ", text="첫 줄\n가짜 줄")))
            assert st == 200 and data["accepted"] == 2
            _, data = await _recent(client)
            assert data["messages"][0]["text"] == ""
            assert (data["messages"][1]["sender"], data["messages"][1]["text"]) == ("보낸 [2J이", "첫 줄 가짜 줄")
        finally:
            await client.close()
    _run(scn())


def test_too_many_messages(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path, max_whisper_messages_per_request=2))
        try:
            st, data = await _post(client, _body(_msg(1), _msg(2), _msg(3)))
            assert (st, data["error"]) == (400, "too_many")
        finally:
            await client.close()
    _run(scn())


def test_prune_whisper_by_recv_ts_and_ids_are_not_reused(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            await _post(client, _body(_msg(1), _msg(2)))
            db = app_db(app)
            now = time.time()
            assert db.prune_whisper(now + 23 * 3600, 24) == 0
            last_id = db.whisper_recent(10)[-1]["id"]
            assert db.prune_whisper(now + 25 * 3600, 24) == 2
            assert db.whisper_recent(10) == []
            await _post(client, _body(_msg(3)))
            assert db.whisper_recent(10)[0]["id"] > last_id
        finally:
            await client.close()
    _run(scn())


def test_whisper_has_its_own_rate_bucket(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path, whisper_slots=["slot1"], whisper_upload_limit_per_min=2,
                                                  upload_limit_per_min=50))
        try:
            st, d1 = await register(client, label="PC-1")
            hdr = device_auth(d1["token"])
            codes = [(await _post(client, _body(_msg(n), device_id=d1["device_id"]), headers=hdr))[0] for n in range(3)]
            assert codes == [200, 200, 429]
        finally:
            await client.close()
    _run(scn())


def test_whisper_slot_token_is_scoped_to_whisper_routes(tmp_path):
    """리뷰 #20 — 귓속말 전용 슬롯의 토큰이 새어도 공개 리스너에서 육의전·사통 쓰기를 열지 못한다."""
    async def scn():
        cfg = make_cfg(tmp_path, whisper_slots=["slot1"], satong_slots=["slot1"])
        app, client = await start_client(cfg, public_only=True)
        try:
            st, d1 = await register(client, label="PC-1", headers=PROXY)
            hdr = {**device_auth(d1["token"]), **PROXY}
            for path in ("/api/market/ping",):
                st, data = await get(client, path, headers=hdr)
                assert (st, data["error"]) == (403, "whisper_slot_only")
            resp = await client.post("/api/satong/observations", json={}, headers=hdr)
            assert (resp.status, (await resp.json())["error"]) == (403, "whisper_slot_only")
            resp = await client.post("/api/market/observations", json={}, headers=hdr)
            assert (resp.status, (await resp.json())["error"]) == (403, "whisper_slot_only")
            # 다른 슬롯은 그대로
            st, d2 = await register(client, invite=INVITE2, label="PC-2", headers=PROXY)
            st, _ = await get(client, "/api/market/ping", headers={**device_auth(d2["token"]), **PROXY})
            assert st == 200
        finally:
            await client.close()
    _run(scn())


def test_out_of_window_whisper_is_rejected_alone(tmp_path):
    """리뷰 #20 — 보존 시간 밖의 한 건은 그 건만 버리고 같은 배치의 새 귓속말은 받는다."""
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            st, data = await _post(client, _body(_msg(1, agent_ts=time.time() - 25 * 3600), _msg(2, text="새 것")))
            assert st == 200 and (data["accepted"], data["duplicates"], data["rejected"]) == (1, 0, 1)
            _, data = await _recent(client)
            assert [m["text"] for m in data["messages"]] == ["새 것"]
        finally:
            await client.close()
    _run(scn())


def test_recent_last_id_survives_prune_and_marks_an_empty_table(tmp_path):
    """리뷰 #20 — 표가 비어도 `last_id` 로 기준점을 잡고, 지운 뒤에도 발급한 최대 id 를 준다(DB 재생성이면 작아진다)."""
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            _, data = await _recent(client)
            assert (data["count"], data["last_id"]) == (0, 0)
            await _post(client, _body(_msg(1), _msg(2)))
            _, data = await _recent(client)
            assert data["last_id"] == data["messages"][-1]["id"]
            last = data["last_id"]
            assert app_db(app).prune_whisper(time.time() + 25 * 3600, 24) == 2
            _, data = await _recent(client)
            assert (data["count"], data["last_id"]) == (0, last)
        finally:
            await client.close()
    _run(scn())


def test_deleted_whispers_are_overwritten(tmp_path):
    async def scn():
        app, client = await start_client(make_cfg(tmp_path))
        try:
            assert app_db(app)._con.execute("PRAGMA secure_delete").fetchone()[0] == 1
        finally:
            await client.close()
    _run(scn())


def test_config_whisper_keys(tmp_path):
    p = tmp_path / "config.json"

    def load(**over):
        import json
        raw = {"secret": "x" * 16, "invite_codes": {"slot1": "code-0001", "seassist": "code-0002"}, **over}
        p.write_text(json.dumps(raw), encoding="utf-8")
        return server_mod.load_config(str(p), env={})

    cfg = load()
    assert cfg["whisper_slots"] == [] and cfg["retention_whisper_hours"] == 24
    assert load(whisper_slots=[" seassist "])["whisper_slots"] == ["seassist"]
    with pytest.raises(SystemExit, match="whisper_slots"):
        load(whisper_slots=["slot9"])
    with pytest.raises(SystemExit, match="retention_whisper_hours"):
        load(retention_whisper_hours=0)
