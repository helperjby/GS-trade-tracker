"""관측기 계약 — 엔진 배선 · 수집 창 수명 · 콘솔 라벨 · 종료 코드 (실 스니퍼 없이).

엔진은 스텁이고 recorder 는 **진짜** `PacketDiscoveryRecorder`(원장 베이스를 tmp 로) — 창 파일의
형식이 SEAssist GUI `[패킷 수집]` 과 같아야 하므로 실제 writer 를 태운다.
"""
from __future__ import annotations

import io
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from yuktracker import agent as A
from yuktracker.seassist import packet_discovery_ledger as L


class _EngineStub:
    """PacketStateSource 의 관측기가 쓰는 표면만."""

    instances: list["_EngineStub"] = []

    def __init__(self, pids_provider, *, status_cb=None, event_cb=None, segment_cb=None,
                 enter_anchor=True, start_ok=True, start_msg="패킷 스니퍼 시작",
                 capturing=True):
        self.pids_provider = pids_provider
        self.status_cb = status_cb
        self.event_cb = event_cb
        self.segment_cb = segment_cb
        self.enter_anchor = enter_anchor
        self._start_ok = start_ok
        self._start_msg = start_msg
        self.capturing = capturing
        self.started = False
        self.stopped = False
        _EngineStub.instances.append(self)

    def start(self):
        self.started = True
        # provider 를 한 번은 불러 본다 — 실 엔진의 DISCOVER 와 같은 시점.
        self.pids_provider()
        return self._start_ok, self._start_msg

    def stop(self):
        self.stopped = True

    def is_capturing(self):
        return self.started and self.capturing

    def health_snapshot(self):
        return {"packets": 3, "fed_segments": 2, "tracked_flows": 1, "battle_events": 0,
                "opens": 1}


def _factory(**kw):
    def make(pids_provider, **engine_kw):
        return _EngineStub(pids_provider, **engine_kw, **kw)
    return make


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        _EngineStub.instances.clear()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(mock.patch.object(L, "_ledger_base", lambda: self.root))
        self.enterContext(mock.patch.object(A.ledger_paths, "packet_dir", lambda: self.root))
        self.recorder = L.PacketDiscoveryRecorder()
        self.addCleanup(lambda: self.recorder.end_window("cleanup"))
        self.indexer = A.PidIndexer(lambda: [4321, 1234])
        self.out = io.StringIO()

    def _run(self, opts, stdin_text: str, factory=None, sleep=None, clock=None):
        return A.run(opts, engine_factory=factory or _factory(), recorder=self.recorder,
                     indexer=self.indexer, stdin=io.StringIO(stdin_text), out=self.out,
                     clock=clock or time.monotonic, sleep=sleep or (lambda s: time.sleep(0.02)))

    def _rows(self) -> list[dict]:
        d = self.root / "packet_discovery"
        files = sorted(d.glob("*.jsonl")) if d.is_dir() else []
        self.assertEqual(len(files), 1, f"창당 파일 1개: {files}")
        return [json.loads(ln) for ln in files[0].read_text(encoding="utf-8").splitlines()
                if ln.strip()]


class PidIndexerTest(unittest.TestCase):
    def test_indices_are_sticky_and_never_reused(self) -> None:
        seen = [[300, 100], [100], [100, 500], [300, 100, 500]]
        ix = A.PidIndexer(lambda: seen.pop(0))
        self.assertEqual(ix.provide(), {0: 100, 1: 300})      # 정렬 순으로 첫 배정
        self.assertEqual(ix.provide(), {0: 100})              # 300 사라져도 100 은 0 유지
        self.assertEqual(ix.provide(), {0: 100, 2: 500})      # 새 pid 는 새 번호(1 재사용 없음)
        self.assertEqual(ix.provide(), {0: 100, 1: 300, 2: 500})  # 돌아온 300 은 옛 번호
        self.assertEqual(ix.known, {0: 100, 1: 300, 2: 500})

    def test_enumeration_failure_is_empty(self) -> None:
        def boom():
            raise RuntimeError("enum")
        self.assertEqual(A.PidIndexer(boom).provide(), {})

    def test_ignores_non_positive(self) -> None:
        self.assertEqual(A.PidIndexer(lambda: [0, -1, 7]).provide(), {0: 7})


class CaptureModeTest(_Base):
    def test_capture_window_labels_and_manual_quit(self) -> None:
        opts = A.RunOptions(capture_min=5.0, pause_on_exit=False, attach_log=False)
        rc = self._run(opts, "육의전 열기\n\n검색 소나무\nq\n")
        self.assertEqual(rc, A.RC_OK)
        eng = _EngineStub.instances[-1]
        self.assertTrue(eng.started and eng.stopped)
        # 원시 세그먼트 탭 배선 — bound method 는 접근마다 새 객체라 self/func 로 비교한다.
        self.assertIs(eng.segment_cb.__self__, self.recorder)
        self.assertIs(eng.segment_cb.__func__, L.PacketDiscoveryRecorder.note_segment)
        self.assertEqual(eng.pids_provider(), {0: 1234, 1: 4321})

        rows = self._rows()
        kinds = [r["kind"] for r in rows]
        self.assertEqual(kinds[0], "window_start")
        self.assertEqual(kinds[-1], "window_end")
        self.assertEqual(rows[0]["slots"], {"0": "클라1", "1": "클라2"})
        self.assertEqual(rows[0]["duration_sec"], 300.0)
        labels = [r for r in rows if r["kind"] == "label"]
        self.assertEqual([r["event"] for r in labels], [A.LABEL_EVENT] * 2)
        self.assertEqual([r["detail"]["note"] for r in labels], ["육의전 열기", "검색 소나무"])
        self.assertEqual(rows[-1]["reason"], "manual")
        self.assertEqual(rows[-1]["health"]["packets"], 3)
        text = self.out.getvalue()
        self.assertIn("라벨 #2 기록", text)
        self.assertIn("종료 헬스", text)

    def test_auto_stop_ends_run(self) -> None:
        # 1분 미만은 recorder 가 1초로 올린다 — 실제 시간 만료로 자동 종료 → 창 마감 → 종료.
        opts = A.RunOptions(capture_min=0.001, pause_on_exit=False, attach_log=False)
        rc = self._run(opts, "")  # 콘솔 입력 없음
        self.assertEqual(rc, A.RC_OK)
        rows = self._rows()
        self.assertEqual(rows[-1]["kind"], "window_end")
        self.assertEqual(rows[-1]["reason"], "시간 만료")
        self.assertIn("자동 종료 — 시간 만료", self.out.getvalue())

    def test_flow_wait_timeout(self) -> None:
        clock = [0.0]

        def fake_clock():
            return clock[0]

        def fake_sleep(s):
            clock[0] += max(s, 1.0)

        opts = A.RunOptions(capture_min=5.0, flow_wait_sec=3.0, pause_on_exit=False,
                            attach_log=False)
        rc = self._run(opts, "", factory=_factory(capturing=False), sleep=fake_sleep,
                       clock=fake_clock)
        self.assertEqual(rc, A.RC_NO_FLOW)
        self.assertFalse(self.recorder.window_open)
        self.assertFalse((self.root / "packet_discovery").exists())
        self.assertTrue(_EngineStub.instances[-1].stopped)
        self.assertIn("대기 초과", self.out.getvalue())

    def test_engine_start_failure(self) -> None:
        opts = A.RunOptions(capture_min=5.0, pause_on_exit=False, attach_log=False)
        rc = self._run(opts, "", factory=_factory(start_ok=False, start_msg="Npcap 미설치"))
        self.assertEqual(rc, A.RC_ENGINE)
        self.assertFalse(self.recorder.window_open)
        self.assertIn("Npcap 미설치", self.out.getvalue())

    def test_label_without_window_is_not_recorded(self) -> None:
        opts = A.RunOptions(capture_min=None, pause_on_exit=False, attach_log=False)
        rc = self._run(opts, "라벨\nq\n")
        self.assertEqual(rc, A.RC_OK)
        self.assertFalse((self.root / "packet_discovery").exists())
        self.assertIn("수집 창이 없어", self.out.getvalue())


class ObserveModeTest(_Base):
    def test_observe_mode_runs_until_quit(self) -> None:
        opts = A.RunOptions(capture_min=None, pause_on_exit=False, attach_log=False)
        rc = self._run(opts, "q\n")
        self.assertEqual(rc, A.RC_OK)
        eng = _EngineStub.instances[-1]
        self.assertTrue(eng.started and eng.stopped)
        self.assertFalse(self.recorder.window_open)
        self.assertIn("관측 대기", self.out.getvalue())


class PauseOnExitTest(_Base):
    def test_pause_waits_for_enter(self) -> None:
        opts = A.RunOptions(capture_min=None, pause_on_exit=True, attach_log=False)
        with mock.patch.object(A, "FINAL_WAIT_SEC", 2.0):
            rc = self._run(opts, "q\n\n")  # q → 종료, 빈 줄 → Enter
        self.assertEqual(rc, A.RC_OK)
        self.assertIn("Enter 를 누르면", self.out.getvalue())


class CliTest(unittest.TestCase):
    def test_parser_defaults(self) -> None:
        from yuktracker.cli import build_parser
        p = build_parser()
        a = p.parse_args([])
        self.assertIsNone(a.capture)
        a = p.parse_args(["--capture"])
        self.assertEqual(a.capture, A.DEFAULT_CAPTURE_MIN)
        a = p.parse_args(["--capture", "2", "--no-elevate", "--no-pause", "--wait", "9"])
        self.assertEqual((a.capture, a.no_elevate, a.no_pause, a.wait), (2.0, True, True, 9.0))

    def test_main_relaunches_with_module(self) -> None:
        from yuktracker import cli
        from yuktracker.seassist import admin
        with mock.patch.object(admin, "is_admin", return_value=False), \
                mock.patch.object(admin, "relaunch_as_admin", return_value=True) as rl, \
                mock.patch.object(cli, "run") as run:
            self.assertEqual(cli.main(["--capture"]), 0)
        rl.assert_called_once_with(module=cli.MODULE)
        run.assert_not_called()

    def test_main_no_elevate_runs(self) -> None:
        from yuktracker import cli
        with mock.patch.object(cli, "run", return_value=0) as run:
            self.assertEqual(cli.main(["--no-elevate", "--no-pause", "--capture", "3"]), 0)
        opts = run.call_args.args[0]
        self.assertEqual((opts.capture_min, opts.pause_on_exit), (3.0, False))

    def test_relaunch_args_use_this_module(self) -> None:
        from yuktracker.seassist import admin
        with mock.patch.object(admin.sys, "frozen", False, create=True), \
                mock.patch.object(admin.sys, "argv", ["x", "--capture", "5"]):
            _exe, params, _cwd = admin._build_relaunch_args("yuktracker")
        self.assertIn("-m yuktracker --capture 5", params)


if __name__ == "__main__":
    unittest.main()
