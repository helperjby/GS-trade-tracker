"""hub DB — 정규화 한 정의 · prune 경계 · 스키마 재오픈 · 배치 롤백 · 기기 표(등록 정의·별칭·같은 트랜잭션 기록)."""
from __future__ import annotations

import re
import sqlite3

import pytest

import db as db_mod

DAY = 86400.0


def _open(tmp_path):
    return db_mod.Database(str(tmp_path / "t.db"))


def _obs(obs_id, agent_ts, rows):
    return {"obs_id": obs_id, "agent_ts": agent_ts, "opcode": 0x321F, "rows": rows}


def _row(**over):
    r = {"listing_id": 1, "item_id": 853, "item_name": "봉인의돌", "quantity": 1, "price": 100,
         "seller": "판매자A", "flag45": 2, "flag46": 0}
    r.update(over)
    return r


def test_norm_item_name_and_listing_key():
    assert db_mod.norm_item_name("봉인의 돌") == "봉인의돌"
    assert db_mod.norm_item_name("  Test  Sword ") == "testsword"
    assert db_mod.norm_item_name("[M]봉인의돌") == "[m]봉인의돌"  # [M] 제거는 관측기 몫
    assert db_mod.norm_item_name(None) == "" and db_mod.norm_item_name("") == ""
    assert db_mod.listing_key(7, 853, "판매자A") == "7:853:판매자A"


def test_prune_boundaries_keep_item_names(tmp_path):
    d = _open(tmp_path)
    now = 1_000_000_000.0
    cutoff = now - 30 * 86400
    d.insert_market_observations("A", [_obs("old", cutoff - 1, [_row(listing_id=1)])], cutoff - 1)
    d.insert_market_observations("A", [_obs("edge", cutoff, [_row(listing_id=2)])], cutoff)
    d.insert_market_observations("A", [_obs("new", now, [_row(listing_id=3)])], now)
    assert d.prune(now) == (1, 1)
    assert d.prune(now) == (0, 0)  # 멱등
    assert sorted(r[0] for r in d._con.execute("SELECT obs_id FROM market_observations")) == ["edge", "new"]
    assert sorted(r[0] for r in d._con.execute("SELECT listing_id FROM market_listings")) == [2, 3]
    assert d._con.execute("SELECT COUNT(*) FROM market_item_names").fetchone()[0] == 1  # 학습 표는 유지
    d.close()


def test_reopen_is_idempotent_and_keeps_rows(tmp_path):
    d = _open(tmp_path)
    d.insert_market_observations("A", [_obs("o1", 10.0, [_row()])], 10.0)
    d.close()
    d2 = _open(tmp_path)  # 같은 파일 재오픈 — CREATE IF NOT EXISTS 라 오류·유실 없음
    assert d2.market_stats(20.0)["observations"] == 1
    rows, total = d2.search_market("봉인의돌", None, 10, 0.0)
    assert total == 1 and rows[0]["listing_id"] == 1
    assert d2.search_market("", None, 10, 0.0) == ([], 0)  # 조건 없는 검색은 빈 결과
    d2.close()


def test_pragmas_wal_and_synchronous_normal(tmp_path):
    d = _open(tmp_path)
    assert d._con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert d._con.execute("PRAGMA synchronous").fetchone()[0] == 1   # NORMAL — POST 마다 SD fsync 로 루프를 세우지 않는다
    d.close()


def test_list_since_keyset_cursor(tmp_path):
    d = _open(tmp_path)
    d.insert_market_observations("A", [_obs("o", 100.0, [_row(listing_id=i, seller=f"s{i}") for i in range(5)])], 100.0)
    page = d.list_market_since(0.0, "", 2)
    assert [r["listing_id"] for r in page] == [0, 1]
    page = d.list_market_since(page[-1]["last_seen_ts"], page[-1]["listing_key"], 2)
    assert [r["listing_id"] for r in page] == [2, 3]
    page = d.list_market_since(page[-1]["last_seen_ts"], page[-1]["listing_key"], 2)
    assert [r["listing_id"] for r in page] == [4]
    assert d.list_market_since(100.0, page[-1]["listing_key"], 2) == []
    assert [r["listing_id"] for r in d.list_market_since(100.0, "", 10)] == [0, 1, 2, 3, 4]   # 키 없으면 같은 ts 전부
    assert d.list_market_since(100.0000001, "", 10) == []
    d.close()


def test_item_name_latest_wins_null_never_overwrites(tmp_path):
    d = _open(tmp_path)
    d.insert_market_observations("A", [_obs("t200", 200.0, [_row(item_name="봉인의돌")])], 200.0)
    d.insert_market_observations("A", [_obs("t100", 100.0, [_row(item_name="[M]봉인의돌")])], 200.0)  # 늦게 온 옛 관측
    d.insert_market_observations("A", [_obs("t300", 300.0, [_row(item_name=None)])], 300.0)          # 이름 못 푼 PC
    (r,) = d._con.execute("SELECT item_name, item_name_norm, quantity FROM market_listings").fetchall()
    assert tuple(r) == ("봉인의돌", "봉인의돌", 1)
    d.insert_market_observations("A", [_obs("t400", 400.0, [_row(item_name="봉인의돌(상)")])], 400.0)
    (r,) = d._con.execute("SELECT item_name FROM market_listings").fetchall()
    assert r[0] == "봉인의돌(상)"
    (n,) = d._con.execute("SELECT item_name FROM market_item_names").fetchall()
    assert n[0] == "봉인의돌(상)"
    d.close()


def test_payload_for_storage_strips_normalized_row_keys():
    obs = {"obs_id": "x", "agent_ts": 1.0, "hdr4": 0, "anomalies": ["a"],
           "rows": [{"listing_id": 1, "item_id": 2, "quantity": 3, "price": 4, "seller": "s", "item_name": "n",
                     "flag45": 2, "flag46": 0, "unknown40": "00", "quantity_hi": 0}, {"listing_id": 9, "seller": "t"}]}
    out = db_mod.payload_for_storage(obs)
    assert out["anomalies"] == ["a"] and out["hdr4"] == 0 and out["obs_id"] == "x"
    assert out["rows"] == [{"unknown40": "00", "quantity_hi": 0}, {}]
    assert "rows" not in db_mod.payload_for_storage({"obs_id": "y", "rows": [{"listing_id": 1}]})
    assert "rows" not in db_mod.payload_for_storage({"obs_id": "z"})
    assert obs["rows"][0]["seller"] == "s"   # 원본은 그대로


def test_insert_rolls_back_whole_batch_on_error(tmp_path):
    d = _open(tmp_path)
    bad = [_obs("ok", 10.0, [_row()]),
           {"obs_id": "broken", "agent_ts": 10.0, "opcode": 1, "rows": [{"listing_id": "x"}]}]
    try:
        d.insert_market_observations("A", bad, 10.0)
    except (KeyError, ValueError, TypeError):
        pass
    else:
        raise AssertionError("예외가 나야 한다")
    assert d.market_stats(20.0)["observations"] == 0  # 첫 관측도 롤백
    d.close()


# ---- 기기 표 ----

def test_devices_create_lookup_record_revoke_note(tmp_path):
    d = _open(tmp_path)
    a = d.create_device("PC-A", "h1", 100.0, "203.0.113.7")
    assert re.fullmatch(r"d-[0-9a-f]{10}", a)
    assert d.get_device_by_token_hash("h1") == {"device_id": a, "label": "PC-A", "alias": "", "revoked_ts": None,
                                                 "upload_count": 0, "last_seen_ts": None}
    assert d.get_device_by_token_hash("nope") is None
    assert d.get_device("d-0000000000") is None
    # 기기 토큰 업로드 — 관측과 같은 트랜잭션에서 last_seen/upload_count
    d.insert_market_observations(a, [_obs("o1", 150.0, [_row()])], 150.0, seen_device=a)
    d.touch_device(a, 160.0)                          # 거부된 업로드(400/403)는 last_seen 만
    dev = d.get_device_by_token_hash("h1")
    assert (dev["upload_count"], dev["last_seen_ts"]) == (1, 160.0)
    assert d.count_active_devices() == 1
    assert d.set_device_revoked(a, 200.0) is True and d.count_active_devices() == 0
    assert d.get_device_by_token_hash("h1")["revoked_ts"] == 200.0
    assert d.set_device_revoked("d-0000000000", 200.0) is False
    assert d.set_device_note(a, "메모") is True and d.set_device_note("d-0000000000", "x") is False
    assert d.set_device_revoked(a, None) is True and d.count_active_devices() == 1
    assert d.get_device(a) == {"device_id": a, "label": "PC-A", "alias": "", "created_ts": 100.0,
                               "created_ip": "203.0.113.7", "last_seen_ts": 160.0, "upload_count": 1,
                               "revoked_ts": None, "note": "메모"}
    assert d.list_devices() == [d.get_device(a)]
    with pytest.raises(sqlite3.IntegrityError):     # token_hash UNIQUE — 재생성으로 못 푼다
        d.create_device("PC-B", "h1", 300.0, None)
    b = d.create_device("PC-B", "h2", 300.0, None)
    assert [x["device_id"] for x in d.list_devices()] == [a, b]   # created_ts 순
    d.close()


def test_insert_with_seen_device_rolls_back_device_record_too(tmp_path):
    d = _open(tmp_path)
    a = d.create_device("A", "h1", 1.0, None)
    bad = [_obs("ok", 10.0, [_row()]), {"obs_id": "broken", "agent_ts": 10.0, "opcode": 1, "rows": [{"listing_id": "x"}]}]
    with pytest.raises((KeyError, ValueError, TypeError)):
        d.insert_market_observations(a, bad, 10.0, seen_device=a)
    dev = d.get_device(a)
    assert (dev["upload_count"], dev["last_seen_ts"]) == (0, None)    # 커밋 1회 — 실패하면 기기 기록도 없다
    d.close()


def test_registered_definition_has_no_activity_expiry(tmp_path):
    """정원이 세는 것은 미제거 기기 **전부** — 업로드 여부·마지막 업로드 시각은 영향이 없다(사용자 결정).
    자리를 비우는 유일한 길은 관리자의 revoke."""
    d = _open(tmp_path)
    now = 100 * DAY
    seen_recent = d.create_device("a", "h1", now - 50 * DAY, None)
    d.touch_device(seen_recent, now - 1 * DAY)
    seen_ancient = d.create_device("b", "h2", now - 50 * DAY, None)
    d.touch_device(seen_ancient, now - 49 * DAY)      # 49일 전이 마지막 — 그래도 자리를 차지한다
    unseen_new = d.create_device("c", "h3", now - 0.5 * DAY, None)
    unseen_ancient = d.create_device("d", "h4", now - 90 * DAY, None)   # 한 번도 안 올린 오래된 행도 그대로
    revoked = d.create_device("e", "h5", now, None)
    d.set_device_revoked(revoked, now)
    assert d.count_active_devices() == 4                  # 제거된 1대만 빠진다
    s = d.market_stats(now)
    assert (s["devices_registered"], s["devices_revoked"]) == (4, 1)
    assert "devices_active" not in s                      # 자동 제외가 없어 "등록"과 같은 수 — 따로 싣지 않는다
    # 관리자가 빼야 자리가 빈다
    assert d.set_device_revoked(unseen_ancient, now) is True
    assert d.count_active_devices() == 3
    assert d.set_device_revoked(unseen_ancient, None) is True
    assert d.count_active_devices() == 4                  # 복구하면 다시 차지
    for dev_id in (seen_ancient, unseen_ancient, unseen_new):
        assert d.get_device(dev_id)["revoked_ts"] is None
    d.close()


def test_admin_alias_is_separate_from_device_reported_label(tmp_path):
    d = _open(tmp_path)
    a = d.create_device("PC-A", "h1", 1.0, None)
    assert d.get_device(a)["alias"] == ""
    assert d.set_device_alias(a, "형") is True
    dev = d.get_device(a)
    assert (dev["alias"], dev["label"]) == ("형", "PC-A")        # label 은 그대로
    assert d.get_device_by_token_hash("h1")["alias"] == "형"     # 서버 로그가 쓰는 경로
    assert d.set_device_alias(a, "") is True and d.get_device(a)["alias"] == ""   # 해제
    assert d.set_device_alias("d-0000000000", "x") is False
    d.close()


def test_device_id_collision_is_regenerated(tmp_path, monkeypatch):
    d = _open(tmp_path)
    ids = iter(["d-aaaaaaaaaa", "d-aaaaaaaaaa", "d-bbbbbbbbbb"])
    monkeypatch.setattr(db_mod, "new_device_id", lambda: next(ids))
    assert d.create_device("A", "h1", 1.0, None) == "d-aaaaaaaaaa"
    assert d.create_device("B", "h2", 2.0, None) == "d-bbbbbbbbbb"   # 충돌 1회 → 재생성
    d.close()


def test_reopen_keeps_devices_and_prune_ignores_them(tmp_path):
    d = _open(tmp_path)
    a = d.create_device("A", "h1", 1.0, None)
    d.close()
    d2 = _open(tmp_path)
    assert d2.prune(10**9) == (0, 0)
    assert [x["device_id"] for x in d2.list_devices()] == [a]
    assert d2.get_device_by_token_hash("h1")["device_id"] == a
    d2.close()


def test_stats_joins_label_and_counts(tmp_path):
    d = _open(tmp_path)
    a = d.create_device("PC-A", "h1", 1.0, None)
    b = d.create_device("PC-B", "h2", 2.0, None)
    d.set_device_revoked(b, 3.0)
    d.insert_market_observations(a, [_obs("o1", 10.0, [_row()])], 10.0, seen_device=a)
    d.insert_market_observations("ADMIN", [_obs("o2", 10.0, [_row(listing_id=2)])], 10.0)
    d.set_device_alias(a, "형")
    s = d.market_stats(20.0, 86400.0)
    assert [(x["device_id"], x["label"], x["alias"], x["observations"]) for x in s["devices"]] == [
        ("ADMIN", None, None, 1), (a, "PC-A", "형", 1)]
    assert (s["devices_registered"], s["devices_revoked"]) == (1, 1)
    d.close()
