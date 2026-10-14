"""사통팔달(0x1f82) — 파서 · 프레이머 opt-in 전량 대기 · 엔진 배선 (H-2610-01 B, 2026-10-14).

관측 전용: 전투 상태·러너에 닿지 않는다. 실데이터 없음 — 보낸이·내용은 `packet_frames.py` 의 합성값.
"""
from __future__ import annotations

import unittest
from unittest import mock

from yuktracker.seassist import gersang_protocol as gp
from yuktracker.seassist import packet_satong as ps

from packet_engine_harness import _EngineHarness, _flow, wrap
from packet_frames import (OP_SATONG, SENDER_A, TEXT_A, TICK, enter_frame, exit_frame, satong_body,
                           satong_frame, wire)


class ParseSatongTest(unittest.TestCase):
    def test_normal_message(self):
        m = ps.parse_satong(satong_body())
        self.assertEqual((m.sender, m.text, m.sender_nul, m.text_nul, m.decode_ok, m.mid),
                         (SENDER_A, TEXT_A, True, True, True, 0))
        self.assertEqual(m.tail_hex, "00010000000000")
        self.assertEqual(m.anomalies, ())

    def test_full_40_byte_text_has_no_nul_and_is_not_an_anomaly(self):
        text = "가" * 20                                        # cp949 2B × 20 = 40B
        m = ps.parse_satong(satong_body(text=text))
        self.assertEqual((m.text, m.text_nul, m.anomalies), (text, False, ()))

    def test_residue_after_nul_is_ignored(self):
        raw = "보낸이".encode("cp949") + b"\x00" + b"\x7f" * 5
        m = ps.parse_satong(satong_body(sender_raw=raw, mid=77))
        self.assertEqual((m.sender, m.mid), ("보낸이", 77))

    def test_empty_text_is_accepted(self):
        m = ps.parse_satong(satong_body(text=""))
        self.assertEqual(m.text, "")

    def test_drift_is_flagged_not_rejected(self):
        m = ps.parse_satong(satong_body(tail=bytes(7), text_raw=b"\xff\xfe abc"))
        self.assertEqual(m.anomalies, ("cp949", "tail67"))
        self.assertIn("�", m.text)
        m = ps.parse_satong(satong_body(sender_raw=b"A" * 20))
        self.assertEqual((m.sender, m.anomalies), ("A" * 20, ("sender_no_nul",)))

    def test_structure_mismatch_is_none(self):
        self.assertIsNone(ps.parse_satong(satong_body(sender="")))
        self.assertIsNone(ps.parse_satong(satong_body(sender_raw=b"   ")))
        self.assertIsNone(ps.parse_satong(satong_body(length=72)))
        self.assertIsNone(ps.parse_satong(satong_body(length=74)))
        self.assertIsNone(ps.parse_satong(satong_body(opcode=0x1F83)))
        body = bytearray(satong_body())
        body[0] = 1
        self.assertIsNone(ps.parse_satong(bytes(body)))
        body = bytearray(satong_body())
        body[3] = 1                                             # opcode u32 상위 바이트
        self.assertIsNone(ps.parse_satong(bytes(body)))
        self.assertIsNone(ps.parse_satong(b""))

    def test_is_satong_requires_exact_shape(self):
        self.assertTrue(gp.is_satong(OP_SATONG, satong_body()))
        self.assertFalse(gp.is_satong(0x321F, satong_body()))
        self.assertFalse(gp.is_satong(OP_SATONG, satong_body(length=72)))


class SatongFramerTest(unittest.TestCase):
    def decoder(self, **kw) -> gp.FlowDecoder:
        d = gp.FlowDecoder(observe_satong=True, **kw)
        self.prefix = wire(TICK) * 8
        self.assertEqual(d.feed_segment(100, self.prefix, 1.0), [])
        self.assertTrue(d.locked)
        return d

    def test_every_segment_split_waits_for_complete_body(self):
        body, data = satong_body(), satong_frame()
        for split in range(1, len(data)):
            with self.subTest(split=split):
                d = self.decoder()
                seq = 100 + len(self.prefix)
                d.feed_segment(seq, data[:split], 2.0)
                self.assertEqual(d.take_satong(), [])
                d.feed_segment(seq + split, data[split:], 3.0)
                got = d.take_satong()
                self.assertEqual([(o.opcode, o.body, o.ts) for o in got], [(OP_SATONG, body, 3.0)])
                self.assertEqual((d.satong_rejected, d.satong_dropped), (0, 0))
                self.assertEqual(d.stats.framer.frames, 9)

    def test_off_by_default_queues_nothing_and_counts_no_rejects(self):
        d = gp.FlowDecoder()
        d.feed_segment(100, wire(TICK) * 8 + satong_frame() + wire(satong_body(length=60)), 1.0)
        self.assertEqual((d.take_satong(), d.satong_rejected), ([], 0))
        self.assertEqual(dict(d.stats.framer.opcode_counts)[OP_SATONG], 2)

    def test_wrong_shape_is_rejected_and_stream_stays_locked(self):
        d = self.decoder()
        bad_len = wire(satong_body(length=60))
        bad_hdr = bytearray(satong_frame())
        bad_hdr[gp.LEN_PREFIX + 3] = 0x01                       # opcode u32 상위 바이트
        d.feed_segment(100 + len(self.prefix), bad_len + bytes(bad_hdr) + satong_frame(text="뒤"), 2.0)
        got = d.take_satong()
        self.assertEqual(len(got), 1)
        self.assertEqual(ps.parse_satong(got[0].body).text, "뒤")
        self.assertEqual(d.satong_rejected, 2)
        self.assertTrue(d.locked)

    def test_battle_events_are_untouched(self):
        d = self.decoder()
        events = d.feed_segment(100 + len(self.prefix), enter_frame() + satong_frame() + exit_frame(), 2.0)
        self.assertEqual([e.kind for e in events], [gp.ENTER, gp.EXIT])
        self.assertEqual(len(d.take_satong()), 1)

    def test_queue_is_bounded(self):
        d = self.decoder()
        burst = satong_frame() * (gp._SATONG_QUEUE_MAX + 3)
        d.feed_segment(100 + len(self.prefix), burst, 2.0)
        self.assertEqual(len(d.take_satong()), gp._SATONG_QUEUE_MAX)
        self.assertEqual(d.satong_dropped, 3)

    def test_reordered_segment_uses_ready_time(self):
        d = self.decoder()
        data = satong_frame()
        seq = 100 + len(self.prefix)
        d.feed_segment(seq + 10, data[10:], 2.0)               # 뒤쪽 먼저(보류)
        d.feed_segment(seq, data[:10], 5.0)                     # 홀이 메워진 시점
        (obs,) = d.take_satong()
        self.assertEqual(obs.ts, 5.0)

    def test_histogram_cap_never_hides_satong(self):
        d = self.decoder()
        noise = b"".join(wire(bytes((gp.MAGIC,)) + (0x5000 + k).to_bytes(2, "little") + b"\x00" * 6)
                         for k in range(gp._OPCODE_HIST_CAP + 5))
        d.feed_segment(100 + len(self.prefix), noise + satong_frame(), 2.0)
        self.assertEqual(dict(d.stats.framer.opcode_counts).get(OP_SATONG), 1)


class EngineSatongTest(_EngineHarness):
    def test_messages_are_delivered_per_slot_with_flow_pid_and_counted(self):
        seen = []
        self.engine._satong_cb = lambda slot, msg, obs, **kw: seen.append((slot, msg.sender, msg.text, kw))
        payload = wire(TICK) * 8 + satong_frame() + satong_frame(text="둘째")
        self._capture([wrap(payload, dst_port=50000, seq=1), wrap(payload, dst_port=50001, seq=1)],
                      [[_flow(100, 50000), _flow(101, 50001)]], {0: 100, 1: 101})
        self.assertEqual(seen, [(0, SENDER_A, TEXT_A, {"pid": 100}), (0, SENDER_A, "둘째", {"pid": 100}),
                                (1, SENDER_A, TEXT_A, {"pid": 101}), (1, SENDER_A, "둘째", {"pid": 101})])
        h = self.engine.health_snapshot()
        self.assertEqual((h["satong_frames"], h["satong_rejected"], h["satong_dropped"], h["satong_parse_failures"],
                          h["satong_anomalies"], h["satong_callback_errors"]), (4, 0, 0, 0, 0, 0))
        self.assertEqual(self.event_log, [])
        self.assertEqual(self.engine._states, {})
        self.assertEqual(self._warns("사통"), [])

    def test_without_callback_nothing_is_parsed(self):
        payload = wire(TICK) * 8 + satong_frame()
        self._capture([wrap(payload, dst_port=50000, seq=1)], [[_flow(100, 50000)]], {0: 100})
        h = self.engine.health_snapshot()
        self.assertEqual((h["satong_frames"], h["satong_rejected"]), (0, 0))

    def test_rejects_warn_once_parse_failures_and_callback_errors_are_counted(self):
        self.engine._satong_cb = mock.Mock(side_effect=RuntimeError("observer failed"))
        payload = (wire(TICK) * 8 + wire(satong_body(length=60)) + wire(satong_body(length=60))
                   + satong_frame(sender="") + satong_frame(tail=bytes(7)))
        self._capture([wrap(payload, dst_port=50000, seq=1)], [[_flow(100, 50000)]], {0: 100})
        h = self.engine.health_snapshot()
        self.assertEqual((h["satong_rejected"], h["satong_parse_failures"], h["satong_frames"],
                          h["satong_anomalies"], h["satong_callback_errors"]), (2, 1, 1, 1, 1))
        self.assertEqual(len(self._warns("사통팔달 번호")), 1)


if __name__ == "__main__":
    unittest.main()
