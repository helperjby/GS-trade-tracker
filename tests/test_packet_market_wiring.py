"""육의전 목록 엔진 배선 계약 — `market_cb`·헬스 7종·드레인 순서 (PR-Y2' 이식).

관측 전용(H-2609-08 B): 전투 상태·러너·입력에 닿지 않는다. 실데이터 없음 — 픽스처는
`packet_frames.py` 의 합성 빌더, 패킷은 `packet_engine_harness.py` 의 합성 L2/L3/L4.
"""
from __future__ import annotations

import inspect
import unittest
from unittest import mock

from yuktracker.seassist import packet_state_source as pss

from packet_engine_harness import _EngineHarness, _flow, wrap
from packet_frames import TICK, default_rows, market_body, market_frame, market_row, wire


class EngineMarketTest(_EngineHarness):
    def test_pages_are_delivered_per_slot_with_flow_pid_and_counted(self):
        seen = []
        self.engine._market_cb = lambda slot, page, obs, **kw: seen.append(
            (slot, page.count, obs.body, kw))
        payload = (wire(TICK) * 8 + market_frame(default_rows(3), total_pages=239)
                   + market_frame([], total_pages=0))
        self._capture([wrap(payload, dst_port=50000, seq=1), wrap(payload, dst_port=50001, seq=1)],
                      [[_flow(100, 50000), _flow(101, 50001)]], {0: 100, 1: 101})
        self.assertEqual([(s, c, kw) for s, c, _b, kw in seen],
                         [(0, 3, {"pid": 100}), (0, 0, {"pid": 100}),
                          (1, 3, {"pid": 101}), (1, 0, {"pid": 101})])
        self.assertEqual(seen[0][2], market_body(default_rows(3), total_pages=239))
        h = self.engine.health_snapshot()
        self.assertEqual(
            (h["market_frames"], h["market_rows"], h["market_rejected"], h["market_dropped"],
             h["market_parse_failures"], h["market_anomalies"], h["market_callback_errors"]),
            (4, 6, 0, 0, 0, 0, 0))
        # 관측 전용 — 전투 상태·이벤트 무접촉.
        self.assertEqual(self.event_log, [])
        self.assertEqual(self.engine._states, {})
        self.assertIsNone(self.engine.state_for(0))
        self.assertEqual(self._warns("육의전"), [])

    def test_without_callback_nothing_is_queued_but_frames_still_count(self):
        payload = wire(TICK) * 8 + market_frame(default_rows(3))
        self._capture([wrap(payload, dst_port=50000, seq=1)], [[_flow(100, 50000)]], {0: 100})
        h = self.engine.health_snapshot()
        self.assertEqual((h["market_frames"], h["market_rows"], h["market_rejected"],
                          h["market_dropped"]), (0, 0, 0, 0))
        self.assertEqual(h["fed_segments"], 1)

    def test_throwing_callback_is_counted_and_capture_continues(self):
        self.engine._market_cb = mock.Mock(side_effect=RuntimeError("observer failed"))
        payload = wire(TICK) * 8 + market_frame(default_rows(1)) + market_frame(default_rows(2))
        self._capture([wrap(payload, dst_port=50000, seq=1)], [[_flow(100, 50000)]], {0: 100})
        self.assertEqual(self.engine._market_cb.call_count, 2)
        h = self.engine.health_snapshot()
        self.assertEqual((h["market_frames"], h["market_callback_errors"]), (2, 2))

    def test_rejected_frames_warn_once_and_are_not_delivered(self):
        seen = []
        self.engine._market_cb = lambda *a, **kw: seen.append(a)
        bad = wire(market_body(default_rows(2), count=3))      # 헤더 행 수 ≠ 실제 — 길이 정합 실패
        payload = wire(TICK) * 8 + bad + bad + market_frame(default_rows(1))
        self._capture([wrap(payload, dst_port=50000, seq=1)], [[_flow(100, 50000)]], {0: 100})
        h = self.engine.health_snapshot()
        self.assertEqual((h["market_rejected"], h["market_frames"]), (2, 1))
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(self._warns("육의전 번호")), 1, "드리프트 경고는 세션당 1회")

    def test_anomalous_page_is_delivered_and_counted(self):
        seen = []
        self.engine._market_cb = lambda slot, page, obs, **kw: seen.append(page.anomalies)
        payload = wire(TICK) * 8 + market_frame([market_row(quantity_hi=1)], hdr4=5)
        self._capture([wrap(payload, dst_port=50000, seq=1)], [[_flow(100, 50000)]], {0: 100})
        self.assertEqual(seen, [("hdr4", "quantity_hi")])
        h = self.engine.health_snapshot()
        self.assertEqual((h["market_frames"], h["market_anomalies"], h["market_parse_failures"]),
                         (1, 1, 0))

    def test_health_line_carries_market_counters(self):
        fl = pss._Flow(0, 100, 50000)
        fl.decoder.feed_segment(1, wire(TICK) * 8, 1.0)
        fl.decoder.framer.observe_market = True
        fl.decoder.feed_segment(1 + 9 * 8, market_frame(default_rows(1)), 2.0)
        self.engine._health["market_frames"] = 7
        self.engine._health["market_rejected"] = 1
        logger = mock.Mock()
        with mock.patch.object(pss, "get_logger", return_value=logger):
            self.engine._log_health({("x", 1): fl})
        fmt, *args = logger.info.call_args.args
        line = fmt % tuple(args)
        self.assertIn("market=7 mrej=1", line)
        self.assertIn("321f=1", line)

    def test_flag_and_drain_are_pinned_in_source(self):
        src = inspect.getsource(pss.PacketStateSource._on_packet)
        self.assertIn("fl.decoder.framer.observe_market = self._market_cb is not None", src)
        self.assertLess(src.index("take_wordinput()"), src.index("take_market()"),
                        "육의전 드레인은 wordinput 블록 뒤 — 기존 블록 순서 무변경")
        self.assertIn("market_cb", inspect.signature(pss.PacketStateSource.__init__).parameters)
        for key in ("market_frames", "market_rows", "market_rejected", "market_dropped",
                    "market_parse_failures", "market_anomalies", "market_callback_errors"):
            self.assertIn(key, self.engine.health_snapshot())


if __name__ == "__main__":
    unittest.main()
