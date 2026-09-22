"""육의전 목록(0x321f) 프레이머 계약 — opt-in 전량 대기, 조기 발화 무손실, 전투 판정 무접촉 (PR-Y2' 이식).

8 틱으로 락한 `FlowDecoder(observe_market=True)` 에 합성 프레임(`packet_frames.py`)을 세그먼트로
먹인다. 실데이터 없음.
"""
from __future__ import annotations

import unittest

from yuktracker.seassist import gersang_protocol as gp

from packet_frames import (OP, TICK, build_frame, default_rows, enter_frame, exit_frame,
                           idle_frames, jochul_body, market_body, market_frame, market_row, wire)


class MarketFramerTest(unittest.TestCase):
    def decoder(self, **kw) -> gp.FlowDecoder:
        d = gp.FlowDecoder(observe_market=True, **kw)
        self.prefix = wire(TICK) * 8
        self.assertEqual(d.feed_segment(100, self.prefix, 1.0), [])
        self.assertTrue(d.locked)
        return d

    def test_every_segment_split_waits_for_complete_body(self):
        for n in (0, 1, 10):
            rows = default_rows(n)
            body, data = market_body(rows, total_pages=239), market_frame(rows, total_pages=239)
            for split in range(1, len(data)):
                with self.subTest(rows=n, split=split):
                    d = self.decoder()
                    seq = 100 + len(self.prefix)
                    self.assertEqual(d.feed_segment(seq, data[:split], 2.0), [])
                    self.assertEqual(d.take_market(), [])
                    self.assertEqual(d.feed_segment(seq + split, data[split:], 3.0), [])
                    got = d.take_market()
                    self.assertEqual([(o.opcode, o.body) for o in got], [(OP, body)])
                    self.assertEqual(got[0].ts, 3.0)
                    self.assertEqual(d.take_market(), [])
                    self.assertEqual(d.market_rejected, 0)
                    self.assertEqual(d.stats.framer.frames, 9)

    def test_max_observed_body_survives_the_early_fire_window(self):
        # 489B(10행)는 OP_STATUS 의 256B 상한 밖 — 자체 상한이 없으면 need=8 뒤가 SKIP 으로 버려진다.
        data = market_frame(default_rows(10), total_pages=239)
        self.assertEqual(len(data), 2 + 489)
        d = self.decoder()
        seq = 100 + len(self.prefix)
        self.assertEqual(d.feed_segment(seq, data[:8], 2.0), [])
        self.assertEqual(d.take_market(), [])
        self.assertEqual(d.feed_segment(seq + 8, data[8:], 2.5), [])
        got = d.take_market()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].body, data[2:])
        self.assertEqual(d.take_market(), [])
        self.assertEqual((d.market_rejected, d.market_dropped), (0, 0))

    def test_opt_in_off_is_byte_identical_for_everything_else(self):
        stream = (market_frame(default_rows(3)) + enter_frame() + market_frame([])
                  + exit_frame() + market_frame(default_rows(10)))
        off = gp.FlowDecoder()
        on = gp.FlowDecoder(observe_market=True)
        ev_off = off.feed_segment(1, wire(TICK) * 8 + stream, 1.0)
        ev_on = on.feed_segment(1, wire(TICK) * 8 + stream, 1.0)
        self.assertEqual([e.kind for e in ev_off], [gp.ENTER, gp.EXIT])
        self.assertEqual([(e.kind, e.opcode, e.body_len) for e in ev_off],
                         [(e.kind, e.opcode, e.body_len) for e in ev_on])
        self.assertEqual(off.take_market(), [])
        self.assertEqual((off.market_rejected, off.market_dropped), (0, 0))
        self.assertEqual(len(on.take_market()), 3)
        self.assertEqual(off.stats.framer.frames, on.stats.framer.frames)
        self.assertEqual(off.stats.framer.opcode_counts, on.stats.framer.opcode_counts)
        self.assertFalse(off.framer.observe_market)

    def test_coexists_with_battle_status_and_jochul_channels(self):
        jochul = wire(jochul_body())  # 합성 13B 조철 본문(0x30df)
        d = self.decoder()
        data = market_frame(default_rows(2)) + enter_frame() + jochul + exit_frame() + market_frame([])
        events = d.feed_segment(100 + len(self.prefix), data, 2.0)
        self.assertEqual([e.kind for e in events], [gp.ENTER, gp.EXIT])
        self.assertEqual([len(o.body) for o in d.take_market()], [9 + 96, 9])
        self.assertEqual(len(d.take_jochul()), 1)
        self.assertEqual(d.jochul_rejected, 0)

    def test_queue_is_bounded_and_reset_counts_pending_as_dropped(self):
        d = self.decoder()
        d.feed_segment(100 + len(self.prefix), market_frame([market_row()]) * 100, 2.0)
        got = d.take_market()
        self.assertEqual(len(got), gp._MARKET_QUEUE_MAX)
        self.assertEqual(len(got) + d.market_dropped, 100)
        self.assertEqual(d.market_rejected, 0)
        d = self.decoder()
        d.feed_segment(100 + len(self.prefix), market_frame([market_row()]) * 3, 2.0)
        d.reset()
        self.assertEqual(d.take_market(), [])
        self.assertEqual(d.market_dropped, 3)

    def test_reordered_overlap_emits_one_observation_stamped_when_assemblable(self):
        d = self.decoder()
        seq = 100 + len(self.prefix)
        data = market_frame(default_rows(2))
        d.feed_segment(seq, data[:8], 2.0)
        d.feed_segment(seq + 40, data[40:], 2.1)      # 홀 — 보류
        self.assertEqual(d.take_market(), [])
        d.feed_segment(seq + 5, data[5:45], 2.2)      # 홀 메움(겹침 포함)
        got = d.take_market()
        self.assertEqual([o.body for o in got], [data[2:]])
        self.assertEqual(got[0].ts, 2.2, "재정렬 흡수분은 조립 가능해진 시점의 ts")
        d.feed_segment(seq, data, 2.3)                # 재전송
        self.assertEqual(d.take_market(), [])
        self.assertEqual(d.stats.framer.frames, 9)

    def test_retransmissions_do_not_duplicate_pages(self):
        d = self.decoder()
        seq = 100 + len(self.prefix)
        for rows in (default_rows(1), default_rows(2)):
            data = market_frame(rows)
            self.assertEqual(d.feed_segment(seq, data, 2.0), [])
            self.assertEqual(d.feed_segment(seq, data, 2.1), [])
            seq += len(data)
        self.assertEqual([len(o.body) for o in d.take_market()], [57, 105])
        self.assertEqual(d.stats.dup_segments, 2)

    def test_oversize_frame_is_early_fired_and_counted_rejected(self):
        # 65행(3,129B) > 상한 3,081B: 종전대로 조기 발화(본문 SKIP) — 관측은 없고 거부 1, 프레임은 센다.
        data = market_frame(default_rows(gp.MARKET_MAX_ROWS + 1))
        self.assertGreater(len(data) - 2, gp._MARKET_FULL_WAIT_MAX_BODY)
        d = self.decoder()
        seq = 100 + len(self.prefix)
        d.feed_segment(seq, data[:100], 2.0)
        d.feed_segment(seq + 100, data[100:] + wire(TICK), 2.1)
        self.assertEqual(d.take_market(), [])
        self.assertEqual(d.market_rejected, 1)
        self.assertEqual(d.stats.framer.frames, 10, "거대 프레임도 프레임으로 세고 다음 틱도 이어 파싱")
        self.assertTrue(d.locked)

    def test_shape_mismatch_is_rejected_but_hdr4_is_not_a_framer_criterion(self):
        d = self.decoder()
        seq = 100 + len(self.prefix)
        bad = market_body(default_rows(2), count=3)     # 헤더 행 수 3, 실제 2행 — 길이 정합 실패
        odd = market_body([market_row()], hdr4=7)
        data = wire(bad) + wire(odd)
        d.feed_segment(seq, data, 2.0)
        got = d.take_market()
        self.assertEqual([o.body for o in got], [odd])
        self.assertEqual(d.market_rejected, 1)
        self.assertEqual(d.market_dropped, 0)

    def test_market_frames_are_not_a_lock_anchor(self):
        frames = market_frame(default_rows(10)) * 3 + wire(TICK)
        self.assertIsNone(gp.find_boundary(frames, 0, gp.DEFAULT_LOCK_K))
        d = gp.FlowDecoder(observe_market=True)
        self.assertEqual(d.feed_segment(1, frames, 1.0), [])
        self.assertFalse(d.locked)
        self.assertEqual(d.take_market(), [])
        self.assertEqual(d.market_rejected, 0)

    def test_opcode_histogram_keeps_market_beyond_the_cap(self):
        d = self.decoder()
        filler = b"".join(build_frame(0x1000 + i, subtype=0, body_len=9) for i in range(gp._OPCODE_HIST_CAP))
        d.feed_segment(100 + len(self.prefix), filler + market_frame([market_row()]), 2.0)
        counts = dict(d.stats.framer.opcode_counts)
        self.assertGreaterEqual(len(counts), gp._OPCODE_HIST_CAP)
        self.assertEqual(counts.get(OP), 1)
        self.assertGreater(d.stats.framer.opcodes_dropped, 0)

    def test_stream_framer_honours_market_ts_override(self):
        f = gp.StreamFramer(observe_market=True)
        data = b"".join(idle_frames(gp.DEFAULT_LOCK_K)) + market_frame([market_row()])
        self.assertEqual(f.feed(data, 5.0, market_ts=7.5), [])
        got = f.take_market()
        self.assertEqual([o.ts for o in got], [7.5])
        f.feed(market_frame([market_row()]), 6.0)
        self.assertEqual([o.ts for o in f.take_market()], [6.0])
        self.assertEqual(f.market_rejected, 0)


if __name__ == "__main__":
    unittest.main()
