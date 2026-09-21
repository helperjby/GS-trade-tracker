"""hub DB — 정규화 한 정의 · prune 경계 · 스키마 재오픈 · 배치 롤백."""
from __future__ import annotations

import db as db_mod


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
