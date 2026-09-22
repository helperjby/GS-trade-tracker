"""오프라인 프로브 계약 — 라이브 재생 vs 프로브 대조, 라벨 대조, 기본 마스킹 (PR-Y2').

창 파일은 합성이다(`packet_frames.py` 빌더로 만든 세그먼트). 실캡처는 레포에 없다 —
실기기 재생은 `python tools\\market_probe.py --root <패킷 루트>` 로 따로 돈다.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from packet_frames import (SELLER_A, default_rows, locked_stream, market_frame, market_row,
                           seller_bytes)

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "market_probe.py"


class MarketProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "DEV-1" / "packet_discovery"
        self.dir.mkdir(parents=True)

    def _window(self, *segments: bytes, labels=(), name: str = "window_20260101_000000.jsonl") -> Path:
        """세그먼트를 도착 순서대로 담은 창 파일 — 원장(`note_segment`)이 쓰는 모양 그대로."""
        rows = [{"kind": "window_start", "device": "DEV-1", "slots": {"0": "클라1"},
                 "mono_base": 100.0, "wall_base": 1_767_225_600.0, "ts": "2026-01-01T00:00:00Z",
                 "iso": "2026-01-01T00:00:00", "duration_sec": 300, "max_bytes": 1 << 20}]
        seq = 1000
        for i, payload in enumerate(segments):
            rows.append({"kind": "seg", "slot": 0, "port": 50000, "seq": seq,
                         "mono": 100.5 + i, "b64": base64.b64encode(payload).decode("ascii")})
            seq += len(payload)
        for note in labels:
            rows.append({"kind": "label", "device": "DEV-1", "event": "market_manual", "slot": 0,
                         "mono": 105.0, "ts": "2026-01-01T00:00:05Z", "iso": "2026-01-01T00:00:05",
                         "label": "market_manual", "detail": {"note": note}})
        rows.append({"kind": "window_end", "device": "DEV-1", "reason": "manual", "segs": len(segments),
                     "bytes": sum(len(s) for s in segments), "dropped": 0, "health": {},
                     "ts": "2026-01-01T00:05:00Z", "iso": "2026-01-01T00:05:00"})
        path = self.dir / name
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")
        return path

    def _run(self, *args: str) -> tuple[int, str]:
        p = subprocess.run([sys.executable, "-X", "utf8", str(TOOL), "--root", self.tmp.name, *args],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=120, cwd=str(ROOT))
        return p.returncode, p.stdout + p.stderr

    def test_live_and_probe_agree_on_a_normal_window(self) -> None:
        stream = locked_stream(market_frame(default_rows(3), total_pages=239),
                               market_frame(default_rows(2), total_pages=239))
        self._window(stream[:40], stream[40:])
        rc, out = self._run()
        self.assertEqual(rc, 0, out)
        self.assertIn("라이브 재생 2 · 프로브 2 · 거부 0 · 유실 0", out)
        self.assertIn("창 1개 · 페이지 2건 · 행 5건", out)
        self.assertNotIn("⚠", out)

    def test_unlocked_stream_is_a_reported_mismatch(self) -> None:
        # 육의전 프레임만 있는 스트림은 락이 안 잡혀(프레이머 계약) 프로덕션이 한 장도 못 본다 —
        # 프로브만 잡으면 ⚠ 와 rc 1. 대조가 존재하는 이유가 바로 이 간극이다.
        self._window(market_frame(default_rows(10)) * 3)
        rc, out = self._run()
        self.assertEqual(rc, 1, out)
        self.assertIn("라이브 재생 0 · 프로브 3", out)
        self.assertIn("⚠ 불일치", out)
        self.assertIn("대조 불일치 창 1개", out)

    def test_label_reconciliation_counts_matching_rows(self) -> None:
        row = market_row(quantity=7, price=1_234_000, seller=SELLER_A)
        self._window(locked_stream(market_frame([row], total_pages=1)),
                     labels=[f"봉인의돌, 7, {SELLER_A}, 1,234,000",
                             f"물품명: 봉인의돌, 판매개수: 9, 판매자: {SELLER_A}, 단가: 1,234,000"])
        rc, out = self._run()
        self.assertEqual(rc, 0, out)
        self.assertIn("라벨 대조 1/2 일치", out)

    def test_seller_is_masked_by_default_and_raw_only_on_demand(self) -> None:
        self._window(locked_stream(market_frame([market_row(seller_raw=seller_bytes(SELLER_A))])))
        rc, out = self._run("--rows")
        self.assertEqual(rc, 0, out)
        self.assertNotIn(SELLER_A, out)
        self.assertIn("테○○○○○A", out)
        rc, out = self._run("--rows", "--raw-seller")
        self.assertEqual(rc, 0, out)
        self.assertIn(SELLER_A, out)

    def test_empty_root_is_rc_2(self) -> None:
        rc, out = self._run()
        self.assertEqual(rc, 2, out)
        self.assertIn("창 0개", out)

    def test_window_files_are_not_modified(self) -> None:
        path = self._window(locked_stream(market_frame(default_rows(2))))
        before = (path.read_bytes(), os.stat(path).st_mtime_ns)
        self.assertEqual(self._run()[0], 0)
        self.assertEqual((path.read_bytes(), os.stat(path).st_mtime_ns), before,
                         "창 파일은 읽기 전용이다")


if __name__ == "__main__":
    unittest.main()
