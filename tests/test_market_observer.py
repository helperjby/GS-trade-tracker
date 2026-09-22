"""관측 콜백 계약 — 봉투 모양 · 단조시계→벽시계 · 이름 해석/미해석 · 표 재검사 (PR-Y1b).

픽스처는 합성이다(`packet_frames.py`). 실클라·실판매자명 없음.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from yuktracker import market_observer as MO
from yuktracker.item_names import ItemTable, TableSource
from yuktracker.seassist import gersang_protocol as gp
from yuktracker.seassist.packet_market import parse_market_page

from packet_frames import SELLER_A, market_body, market_row


def _source(**over) -> TableSource:
    base = dict(gcs_path="C:\\clientdir\\gersang.gcs", gcs_size=100, gcs_mtime_ns=5,
                gcs_sha256="a" * 64, archive_ts="2026-01-01T00:00:00Z", stream_offset=0,
                rows=2, skipped=0, duplicate_ids=0, decode_errors=0,
                extracted_at="2026-01-01T00:00:00Z", extractor_version=1)
    base.update(over)
    return TableSource(**base)


def _table(items=None) -> ItemTable:
    return ItemTable(items if items is not None else {6215: "작은바람의속성석", 853: "[M]봉인의돌"},
                     _source())


def _page(rows=None, **kw):
    page = parse_market_page(market_body(rows if rows is not None else [market_row()], **kw))
    assert page is not None
    return page


def _obs(_page=None, ts: float = 500.0) -> gp.MarketObservation:
    """관측 1건. `body` 는 `obs_id` 의 sha1 에만 쓰이므로 합성 바이트 하나면 된다."""
    return gp.MarketObservation(ts=ts, opcode=gp.OP_MARKET_LIST, body=b"\x00\x1f\x32\x00\x00")


class EnvelopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.names = MO.ItemNames(_table())
        # 벽시계 1,000,000.5 인 순간에 단조시계는 600 — 관측은 단조 500(=10초 전)에 있었다.
        self.obs = MO.MarketObserver("ab12cd34", self.names, wall=lambda: 1_000_000.5,
                                     mono=lambda: 600.0)

    def test_agent_ts_walks_monotonic_back_to_wall_clock(self) -> None:
        page = _page()
        env = self.obs.envelope(page, _obs(page, ts=590.0))
        self.assertAlmostEqual(env["agent_ts"], 1_000_000.5 - 10.0, places=6)

    def test_obs_id_shape_is_stable_for_the_same_body(self) -> None:
        page = _page()
        observation = _obs(page, ts=590.0)
        first = self.obs.envelope(page, observation)["obs_id"]
        again = self.obs.envelope(page, observation)["obs_id"]
        self.assertEqual(first, again, "같은 관측은 같은 obs_id — 허브 dedup 의 전제")
        local, ms, digest = first.split(":")
        self.assertEqual(local, "ab12cd34")
        self.assertEqual(int(ms), int((1_000_000.5 - 10.0) * 1000))
        self.assertRegex(digest, r"^[0-9a-f]{12}$")
        self.assertLessEqual(len(first), 128, "허브의 obs_id 상한")

    def test_rows_carry_names_and_unresolved_ones_are_null(self) -> None:
        rows = [market_row(item_id=6215), market_row(item_id=853), market_row(item_id=999999)]
        env = self.obs.envelope(_page(rows, total_pages=3), _obs(_page(rows)))
        self.assertEqual([r["item_name"] for r in env["rows"]],
                         ["작은바람의속성석", "봉인의돌", None])  # [M] 은 표시명에서 지운다
        self.assertEqual(self.obs.unknown_item, 1)
        self.assertEqual(env["rows"][0]["seller"], SELLER_A, "업로드는 원본 판매자명(마스킹은 표시용)")
        self.assertEqual(env["item_table"], {"gcs_sha256": "a" * 64, "rows": 2,
                                             "archive_ts": "2026-01-01T00:00:00Z"})

    def test_envelope_fields_match_the_hub_contract(self) -> None:
        page = _page([market_row(quantity_hi=1)], total_pages=239, hdr4=5)
        env = self.obs.envelope(page, _obs(page))
        self.assertEqual(env["opcode"], gp.OP_MARKET_LIST)
        self.assertIsNone(env["page"], "응답에 페이지 번호가 없다 — 허브도 NULL 로 받는다")
        self.assertEqual((env["total_pages"], env["hdr4"]), (239, 5))
        self.assertEqual(env["anomalies"], ["hdr4", "quantity_hi"])
        row = env["rows"][0]
        self.assertEqual(set(row), {"listing_id", "item_id", "quantity", "quantity_hi", "price",
                                    "price_hi", "seller", "unknown40", "flag45", "flag46",
                                    "item_name"})
        self.assertLessEqual(len(env["rows"]), gp.MARKET_MAX_ROWS)

    def test_without_a_table_every_name_is_null_and_no_item_table_key(self) -> None:
        obs = MO.MarketObserver("ab12cd34", MO.ItemNames(None))
        page = _page([market_row(), market_row(item_id=853)])
        env = obs.envelope(page, _obs(page))
        self.assertEqual([r["item_name"] for r in env["rows"]], [None, None])
        self.assertNotIn("item_table", env)
        self.assertEqual(obs.unknown_item, 2)


class CallbackTest(unittest.TestCase):
    def test_callback_enqueues_and_prints(self) -> None:
        queued, said = [], []
        obs = MO.MarketObserver("id", MO.ItemNames(_table()), enqueue=queued.append, say=said.append)
        page = _page([market_row(), market_row(item_id=853)], total_pages=239)
        obs(2, page, _obs(page), pid=1234)
        self.assertEqual(len(queued), 1)
        self.assertEqual((obs.pages, obs.rows), (1, 2))
        self.assertEqual(said, ["[육의전] 클라3 목록 2행 / 총 239쪽"])

    def test_anomalies_are_shown_in_the_console_line(self) -> None:
        said = []
        obs = MO.MarketObserver("id", MO.ItemNames(_table()), say=said.append)
        page = _page([market_row(price_hi=1)], hdr4=1)
        obs(0, page, _obs(page))
        self.assertIn("⚠hdr4,price_hi", said[0])

    def test_callback_never_raises_into_the_sniffer_thread(self) -> None:
        def boom(_env):
            raise RuntimeError("스풀 고장")

        obs = MO.MarketObserver("id", MO.ItemNames(_table()), enqueue=boom)
        page = _page()
        obs(0, page, _obs(page))  # 던지면 스니퍼 스레드가 죽어 창 전체를 잃는다
        self.assertEqual(obs.pages, 1)


class ItemNamesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gcs = Path(self.tmp.name) / "gersang.gcs"
        self.gcs.write_bytes(b"x" * 100)
        self.now = 0.0
        st = self.gcs.stat()
        self.names = MO.ItemNames(ItemTable({1: "가"}, _source(gcs_size=st.st_size,
                                                               gcs_mtime_ns=st.st_mtime_ns)),
                                  self.gcs, clock=lambda: self.now)

    def test_recheck_waits_for_the_interval(self) -> None:
        self.gcs.write_bytes(b"y" * 200)
        self.assertFalse(self.names.recheck(), "주기 전에는 stat 도 하지 않는다")

    def test_recheck_reloads_when_the_client_is_patched(self) -> None:
        self.now = MO.TABLE_RECHECK_SEC + 1
        self.assertFalse(self.names.recheck(), "안 바뀌었으면 그대로")
        self.gcs.write_bytes(b"y" * 200)
        self.now += MO.TABLE_RECHECK_SEC + 1
        fresh = ItemTable({1: "가", 2: "나"}, _source(gcs_size=200))
        with mock.patch.object(MO, "load_item_table_result",
                               return_value=SimpleNamespace(table=fresh, error=None)):
            self.assertTrue(self.names.recheck())
        self.assertEqual(self.names.rows, 2)

    def test_reload_failure_keeps_the_old_table(self) -> None:
        self.now = MO.TABLE_RECHECK_SEC + 1
        self.gcs.write_bytes(b"y" * 200)
        with mock.patch.object(MO, "load_item_table_result",
                               return_value=SimpleNamespace(table=None, error="깨짐")):
            self.assertFalse(self.names.recheck())
        self.assertEqual(self.names.rows, 1)


if __name__ == "__main__":
    unittest.main()
