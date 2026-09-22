"""육의전 목록(0x321f) 파서 계약 — 검증된 배치만 수용, 미상 필드는 anomaly (PR-Y2' 이식).

픽스처는 전부 **합성**이다(`packet_frames.py` 빌더 — 실판매자명·캡처 파일은 레포에 없다).
근거 문서는 `docs/PACKET-MARKET.md`.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from yuktracker.seassist import gersang_protocol as gp
from yuktracker.seassist import packet_market as pm

from packet_frames import (OP, SELLER_A, UNKNOWN40, default_rows, market_body, market_row,
                           seller_bytes)


class ParserAcceptTest(unittest.TestCase):
    def test_header_only_empty_search_is_a_valid_page(self):
        body = market_body([], total_pages=0)
        self.assertEqual(len(body), 9)
        page = pm.parse_market_page(body)
        self.assertIsNotNone(page)
        self.assertEqual((page.opcode, page.total_pages, page.count, page.rows), (OP, 0, 0, ()))
        self.assertEqual(page.anomalies, ())
        self.assertIsNone(page.unknown40)
        self.assertTrue(page.listing_desc)

    def test_fields_read_back(self):
        body = market_body([market_row()], total_pages=247)
        page = pm.parse_market_page(body)
        self.assertEqual((page.total_pages, page.count, page.hdr4), (247, 1, 0))
        r = page.rows[0]
        self.assertEqual((r.listing_id, r.item_id, r.quantity, r.price), (3769170, 6215, 329, 780_000))
        self.assertEqual((r.quantity_hi, r.price_hi), (0, 0))
        self.assertEqual((r.quantity_u64, r.price_u64), (329, 780_000))
        self.assertEqual(r.seller, SELLER_A)
        self.assertTrue(r.seller_nul)
        self.assertEqual(r.unknown40, 0x09279020)
        self.assertEqual(r.unknown40_hex, "20902709")
        self.assertEqual((r.raw44, r.flag45, r.flag46, r.raw47), (0, 2, 3, 0))
        self.assertEqual(page.anomalies, ())
        self.assertEqual(page.listing_ids, (3769170,))

    def test_accepts_observed_and_maximum_row_counts(self):
        for n in (1, 5, 10, gp.MARKET_MAX_ROWS):
            with self.subTest(rows=n):
                page = pm.parse_market_page(market_body(default_rows(n), total_pages=239))
                self.assertIsNotNone(page)
                self.assertEqual(page.count, n)
                self.assertEqual(len(page.rows), n)
                self.assertTrue(page.listing_desc)
                self.assertTrue(gp.is_market(OP, market_body(default_rows(n))))

    def test_seller_residue_after_nul_is_ignored_and_no_nul_reads_all_16(self):
        residue = seller_bytes(SELLER_A, residue=b"\xf8\x31")
        page = pm.parse_market_page(market_body([market_row(seller_raw=residue)]))
        self.assertEqual(page.rows[0].seller, SELLER_A)
        self.assertEqual(page.rows[0].seller_raw, residue)
        self.assertEqual(page.anomalies, ())
        full = "가나다라마바사아".encode("cp949")  # 16B, NUL 없음
        page = pm.parse_market_page(market_body([market_row(seller_raw=full)]))
        self.assertEqual(page.rows[0].seller, "가나다라마바사아")
        self.assertFalse(page.rows[0].seller_nul)
        self.assertIn("seller_no_nul", page.anomalies)

    def test_undecodable_seller_bytes_become_replacement_char_not_error(self):
        raw = b"\xff\xfe" + b"\x00" * 14
        page = pm.parse_market_page(market_body([market_row(seller_raw=raw)]))
        self.assertIn("�", page.rows[0].seller)
        self.assertTrue(page.rows[0].seller_nul)

    def test_search_response_grouping_is_not_an_anomaly(self):
        # 검색 응답: 아이템 묶음별 내림차순 — 전체로는 내림차순이 아닐 수 있다(표시용 플래그만).
        rows = [market_row(listing_id=10, item_id=3506), market_row(listing_id=9, item_id=3506),
                market_row(listing_id=20, item_id=853)]
        page = pm.parse_market_page(market_body(rows, total_pages=2))
        self.assertFalse(page.listing_desc)
        self.assertEqual(page.anomalies, ())


class ParserRejectTest(unittest.TestCase):
    def test_every_truncation_and_one_extra_byte_are_rejected(self):
        body = market_body(default_rows(2))
        for cut in range(len(body)):
            with self.subTest(cut=cut):
                self.assertIsNone(pm.parse_market_page(body[:cut]))
                self.assertFalse(gp.is_market(OP, body[:cut]))
        self.assertIsNone(pm.parse_market_page(body + b"\x00"))
        self.assertFalse(gp.is_market(OP, body + b"\x00"))

    def test_count_mismatch_and_over_limit_are_rejected(self):
        two_rows_count_three = market_body(default_rows(2), count=3)
        self.assertIsNone(pm.parse_market_page(two_rows_count_three))
        over = market_body(default_rows(gp.MARKET_MAX_ROWS + 1))
        self.assertEqual(len(over), 9 + 48 * 65)
        self.assertIsNone(pm.parse_market_page(over))
        self.assertFalse(gp.is_market(OP, over))

    def test_header_damage_is_rejected_but_hdr4_is_not_a_criterion(self):
        body = bytearray(market_body([market_row()]))
        for pos, val in ((0, 0x01), (1, 0x20), (2, 0x33), (3, 0x01)):
            damaged = bytes(body[:pos]) + bytes((val,)) + bytes(body[pos + 1:])
            with self.subTest(pos=pos):
                self.assertIsNone(pm.parse_market_page(damaged))
                self.assertFalse(gp.is_market(OP, damaged))
        # 다른 번호는 같은 모양이어도 육의전이 아니다.
        self.assertIsNone(pm.parse_market_page(market_body([market_row()], opcode=0x3220)))
        self.assertFalse(gp.is_market(0x3220, market_body([market_row()], opcode=0x3220)))
        page = pm.parse_market_page(market_body([market_row()], hdr4=5))
        self.assertIsNotNone(page)
        self.assertEqual(page.hdr4, 5)
        self.assertIn("hdr4", page.anomalies)

    def test_is_market_agrees_with_parser_over_adversarial_inputs(self):
        base = market_body(default_rows(3), total_pages=5)
        cases = [base, base[:-1], base + b"\x00", market_body([], total_pages=0),
                 market_body(default_rows(3), count=2), bytes(9), b"", base[:9],
                 market_body(default_rows(11)), market_body([market_row()], hdr4=9)]
        for i in range(len(base)):
            flipped = bytearray(base)
            flipped[i] ^= 0xFF
            cases.append(bytes(flipped))
        for body in cases:
            self.assertEqual(gp.is_market(OP, body), pm.parse_market_page(body) is not None,
                             body[:12].hex())


class AnomalyTest(unittest.TestCase):
    def test_hi_halves_signal_u64_promotion(self):
        page = pm.parse_market_page(market_body([market_row(quantity_hi=1, price_hi=2)]))
        r = page.rows[0]
        self.assertEqual(r.quantity_u64, 329 | (1 << 32))
        self.assertEqual(r.price_u64, 780_000 | (2 << 32))
        self.assertEqual(page.anomalies, ("quantity_hi", "price_hi"))

    def test_unknown_field_drift_is_flagged_not_rejected(self):
        rows = [market_row(unknown40=UNKNOWN40), market_row(listing_id=1, unknown40=b"\x58\x00\x00\x00")]
        page = pm.parse_market_page(market_body(rows))
        self.assertIn("unknown40_varies", page.anomalies)
        for kw, name in (({"flag45": 3}, "flag45"), ({"flag46": 8}, "flag46"),
                         ({"raw44": 1}, "raw44"), ({"raw47": 7}, "raw47")):
            with self.subTest(name=name):
                page = pm.parse_market_page(market_body([market_row(**kw)]))
                self.assertEqual(page.anomalies, (name,))
        page = pm.parse_market_page(market_body(default_rows(11), total_pages=3))
        self.assertEqual(page.anomalies, ("count_gt_page",))
        page = pm.parse_market_page(market_body(default_rows(2), total_pages=0))
        self.assertEqual(page.anomalies, ("total_pages_zero",))

    def test_anomaly_order_is_deterministic(self):
        rows = [market_row(quantity_hi=1, flag45=0, raw47=1, unknown40=b"\x01\x00\x00\x00"),
                market_row(listing_id=2, unknown40=b"\x02\x00\x00\x00")]
        page = pm.parse_market_page(market_body(rows, hdr4=1))
        self.assertEqual(page.anomalies,
                         ("hdr4", "quantity_hi", "unknown40_varies", "raw47", "flag45"))


class SerializationTest(unittest.TestCase):
    def test_mask_seller_keeps_only_first_and_last(self):
        for name, want in (("", ""), ("a", "a"), ("ab", "a○"), ("abc", "a○c"),
                           ("[탐]테스트", "[○○○○트"), (SELLER_A, "테○○○○○A")):
            with self.subTest(name=name):
                self.assertEqual(pm.mask_seller(name), want)

    def test_row_to_dict_excludes_raw_bytes_and_masks_on_request(self):
        row = pm.parse_market_page(market_body([market_row(quantity_hi=0)])).rows[0]
        d = pm.row_to_dict(row)
        self.assertEqual(d, {
            "listing_id": 3769170, "item_id": 6215, "quantity": 329, "quantity_hi": 0,
            "price": 780_000, "price_hi": 0, "seller": SELLER_A, "unknown40": "20902709",
            "flag45": 2, "flag46": 3})
        self.assertNotIn("seller_raw", d)
        self.assertEqual(pm.row_to_dict(row, mask=True)["seller"], pm.mask_seller(SELLER_A))
        # JSON 직렬화 가능(원장·업로드) — bytes 없음.
        import json
        json.dumps(d, ensure_ascii=False)


class ContractTest(unittest.TestCase):
    def test_constants_pinned(self):
        self.assertEqual(gp.OP_MARKET_LIST, 0x321F)
        self.assertEqual(gp.MARKET_OPCODES, frozenset({0x321F}))
        self.assertEqual((gp.MARKET_HEADER_LEN, gp.MARKET_ROW_LEN, gp.MARKET_MAX_ROWS), (9, 48, 64))
        self.assertEqual(gp._MARKET_FULL_WAIT_MAX_BODY, 9 + 48 * 64)
        self.assertEqual(pm.OBSERVED_PAGE_ROWS, 10)
        self.assertEqual(pm.SELLER_LEN, 16)
        for name in ("MarketObservation", "is_market", "market_row_count", "OP_MARKET_LIST",
                     "MARKET_OPCODES", "MARKET_HEADER_LEN", "MARKET_ROW_LEN", "MARKET_MAX_ROWS"):
            self.assertIn(name, gp.__all__)
        self.assertEqual(gp.__all__[0], "FlowDecoder")

    def test_parser_module_is_stdlib_plus_protocol_only(self):
        # 관측기의 import 경계(tests/test_vendor.py ALLOWED_STDLIB) 안에 남는다 — struct 없음.
        tree = ast.parse(Path(pm.__file__).read_text(encoding="utf-8"))
        absolute: set[str] = set()
        relative: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                absolute.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                (relative if node.level else absolute).add(node.module or "")
        self.assertEqual(absolute, {"__future__", "dataclasses"}, absolute)
        self.assertEqual(relative, {"gersang_protocol"}, relative)

    def test_market_row_count_reads_header_u16(self):
        self.assertEqual(gp.market_row_count(market_body(default_rows(7))), 7)
        self.assertEqual(gp.market_row_count(market_body([], count=300)), 300)


if __name__ == "__main__":
    unittest.main()
