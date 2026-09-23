"""관측기 계약 — 엔진 배선 · 수집 창 수명 · 콘솔 라벨 · 종료 코드 (실 스니퍼 없이).

엔진은 스텁이고 recorder 는 **진짜** `PacketDiscoveryRecorder`(원장 베이스를 tmp 로) — 창 파일의
형식이 SEAssist GUI `[패킷 수집]` 과 같아야 하므로 실제 writer 를 태운다.
"""
from __future__ import annotations

import io
import json
import threading
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
                 market_cb=None, enter_anchor=True, start_ok=True, start_msg="패킷 스니퍼 시작",
                 capturing=True):
        self.pids_provider = pids_provider
        self.status_cb = status_cb
        self.event_cb = event_cb
        self.segment_cb = segment_cb
        self.market_cb = market_cb
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
                "opens": 1, "market_frames": 2, "market_rows": 12}


def _no_market(opts, say, stdin):
    """허브·아이템 표 배선을 끄고 도는 `market_setup` — 이 파일의 테스트는 수집 창 계약을 본다.
    (사용자 프로필의 설정·클라 탐색을 건드리지 않는다. 배선 자체는 test_market_setup.py.)"""
    return A.Market()


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

    def _run(self, opts, stdin_text: str, factory=None, sleep=None, clock=None,
             market_setup=_no_market, stdin_obj=None):
        return A.run(opts, engine_factory=factory or _factory(), recorder=self.recorder,
                     indexer=self.indexer, stdin=stdin_obj if stdin_obj is not None else io.StringIO(stdin_text),
                     out=self.out,
                     clock=clock or time.monotonic, sleep=sleep or (lambda s: time.sleep(0.02)),
                     market_setup=market_setup)

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

    def test_hangul_q_key_quits_without_label(self) -> None:
        # 한글 IME 상태에서 q 키 = ㅂ — 실기기(2026-09-21)에서 'ㅂ' 라벨이 두 번 남고 종료되지 않았다.
        opts = A.RunOptions(capture_min=5.0, pause_on_exit=False, attach_log=False)
        rc = self._run(opts, "육의전 열기\nㅂ\n")
        self.assertEqual(rc, A.RC_OK)
        rows = self._rows()
        self.assertEqual([r["detail"]["note"] for r in rows if r["kind"] == "label"], ["육의전 열기"])
        self.assertEqual(rows[-1]["reason"], "manual")
        self.assertNotIn("라벨 #2", self.out.getvalue())

    def test_console_close_hook_ends_window_with_its_own_reason(self) -> None:
        """콘솔 X — 설치 함수를 가로채 콜백을 잡고, 창이 열린 뒤 핸들러 스레드처럼 직접 부른다.

        실제 프로세스는 핸들러가 돌아오면 죽지만 여기서는 이어서 q 로 끝내, `finally` 의 manual 마감이
        이미 닫힌 창을 다시 닫지 않는 것(window_end 1행·reason=console_close)까지 본다.
        """
        captured: dict = {}

        def fake_install(on_close):
            captured["on_close"] = on_close
            captured["removed"] = False

            def remove():
                captured["removed"] = True
            return remove

        release = threading.Event()

        class _Stdin:
            def __iter__(self):
                release.wait(5.0)
                yield "q\n"

        def fire():
            # 창이 열리고 안내 줄까지 찍힌 뒤(state["window"] = True 이후)에 닫기 이벤트를 흉내 낸다.
            deadline = time.monotonic() + 5.0
            while "콘솔에 한 줄 치고" not in self.out.getvalue() and time.monotonic() < deadline:
                time.sleep(0.01)
            captured["on_close"](A.CONSOLE_CLOSE_REASON)
            release.set()

        opts = A.RunOptions(capture_min=5.0, pause_on_exit=False, attach_log=False)
        threading.Thread(target=fire, daemon=True).start()
        with mock.patch.object(A, "_install_console_close_hook", fake_install):
            rc = A.run(opts, engine_factory=_factory(), recorder=self.recorder, indexer=self.indexer,
                       stdin=_Stdin(), out=self.out, sleep=lambda s: time.sleep(0.02))
        self.assertEqual(rc, A.RC_OK)
        self.assertTrue(captured["removed"])
        rows = self._rows()
        ends = [r for r in rows if r["kind"] == "window_end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["reason"], A.CONSOLE_CLOSE_REASON)
        self.assertTrue(_EngineStub.instances[-1].stopped)
        self.assertIn("콘솔 종료 신호", self.out.getvalue())


class ConsoleCtrlEventTest(unittest.TestCase):
    """`SetConsoleCtrlHandler` 판정부 — 닫기 계열만 동기 마감, Ctrl+C/Break 는 Python 에 넘긴다."""

    def test_close_events_call_on_close_synchronously(self) -> None:
        calls: list[str] = []
        for ev in (A.CTRL_CLOSE_EVENT, A.CTRL_LOGOFF_EVENT, A.CTRL_SHUTDOWN_EVENT):
            self.assertTrue(A._console_ctrl_event(ev, calls.append))
        self.assertEqual(calls, [A.CONSOLE_CLOSE_REASON] * 3)

    def test_ctrl_c_and_break_are_left_to_python(self) -> None:
        calls: list[str] = []
        for ev in (A.CTRL_C_EVENT, A.CTRL_BREAK_EVENT):
            self.assertFalse(A._console_ctrl_event(ev, calls.append))
        self.assertEqual(calls, [])

    def test_on_close_exception_is_swallowed(self) -> None:
        def boom(reason):
            raise RuntimeError("x")
        self.assertTrue(A._console_ctrl_event(A.CTRL_CLOSE_EVENT, boom))

    def test_install_is_noop_off_windows(self) -> None:
        with mock.patch.object(A.sys, "platform", "linux"):
            remove = A._install_console_close_hook(lambda r: None)
        remove()  # 예외 없음

    @unittest.skipUnless(A.sys.platform == "win32", "Windows 콘솔 API")
    def test_install_and_remove_on_windows(self) -> None:
        # 실제 SetConsoleCtrlHandler 등록/해제가 예외 없이 도는지만 — 이벤트 발생은 실기기 검증.
        remove = A._install_console_close_hook(lambda r: None)
        remove()


class ObserveModeTest(_Base):
    def test_observe_mode_runs_until_quit(self) -> None:
        opts = A.RunOptions(capture_min=None, pause_on_exit=False, attach_log=False)
        rc = self._run(opts, "q\n")
        self.assertEqual(rc, A.RC_OK)
        eng = _EngineStub.instances[-1]
        self.assertTrue(eng.started and eng.stopped)
        self.assertFalse(self.recorder.window_open)
        self.assertIn("모드: 관측", self.out.getvalue())
        # 수집 창이 없어도 육의전 헬스는 종료 줄에 남는다(관측은 창과 무관하게 돈다).
        self.assertIn("육의전 2쪽 12행", self.out.getvalue())

    def test_market_callback_and_uploader_come_from_market_setup(self) -> None:
        """엔진에 넘어가는 `market_cb` 는 배선이 만든 관측자다 — 끊기면 육의전이 통째로 죽는다."""
        seen = {}

        class _Up:
            def __init__(self):
                self.started = False
                self.stopped = False
                self.order = []
                self.uploaded = 3
                self.quarantined = self.queue_dropped = 0
                self.spool = SimpleNamespace(pending=lambda: [1, 2])
                self.stopped_reason = ""
                self.halted = False

            def start(self):
                self.started = True
                eng = _EngineStub.instances[-1]
                self.order.append(("start", eng.started, eng.stopped))   # 엔진이 뜬 뒤·멈추기 전

            def stop(self):
                self.stopped = True
                self.order.append(("stop", _EngineStub.instances[-1].stopped))   # 엔진을 멈춘 뒤

            def is_alive(self):
                return False

            def queue_size(self):
                return 0

        def setup(opts, say, stdin):
            obs = SimpleNamespace(pages=2, rows=12, unknown_item=1)
            seen["market"] = A.Market(cb="MARKET_CB", uploader=_Up(), observer=obs)
            return seen["market"]

        opts = A.RunOptions(capture_min=None, pause_on_exit=False, attach_log=False)
        self.assertEqual(self._run(opts, "q\n", market_setup=setup), A.RC_OK)
        self.assertEqual(_EngineStub.instances[-1].market_cb, "MARKET_CB")
        self.assertTrue(seen["market"].uploader.started, "업로더 스레드를 띄운다 — 안 띄우면 관측이 큐에서 썩는다(G7 1차)")
        self.assertTrue(seen["market"].uploader.stopped, "종료 때 업로더를 멈춘다")
        self.assertEqual(seen["market"].uploader.order, [("start", True, False), ("stop", True)],
                         "start 는 엔진 기동 뒤·루프 전, stop 은 엔진 정지 뒤")
        self.assertIn("[육의전] 관측 2쪽 12행 / 이름 미해석 1행 / 업로드 3건 · 대기 2배치",
                      self.out.getvalue())


class UploaderLifecycleTest(_Base):
    """run() 이 **진짜** Uploader 를 띄우고 닫는다 — 스텁이 아니라 지난 실행의 스풀이 실제로 POST 되는지로 본다."""

    def _market(self, post, *, batch_wait=0.05):
        from yuktracker import spool as S
        store = S.Spool(self.root / "spool")
        up = S.Uploader(store, hub_url="https://hub", token="tok", device_id=lambda: "d-1",
                        say=lambda m: self.out.write(m + "\n"), post=post, batch_wait=batch_wait)
        obs = SimpleNamespace(pages=0, rows=0, unknown_item=0)
        return store, A.Market(cb=lambda *a, **k: None, uploader=up, observer=obs)

    def test_run_starts_the_real_uploader_and_pending_spool_is_posted(self) -> None:
        posted = threading.Event()
        seen = []

        def post(hub_url, token, device_id, observations):
            seen.append([o["obs_id"] for o in observations])
            posted.set()
            from yuktracker import hub_client
            return hub_client.Response(200, {"ok": True, "accepted": len(observations)})

        store, market = self._market(post)
        store.write([{"obs_id": "prev"}])           # 지난 실행이 남긴 스풀
        stdin = _SlowQuit(posted)
        rc = self._run(A.RunOptions(capture_min=None, pause_on_exit=False, attach_log=False), "",
                       market_setup=lambda o, s, i: market, stdin_obj=stdin)
        self.assertEqual(rc, A.RC_OK)
        self.assertEqual(seen, [["prev"]])
        self.assertFalse(market.uploader.is_alive(), "close() 가 스레드를 끝낸다")
        self.assertIn("업로드 1건 · 대기 0배치", self.out.getvalue())

    def test_engine_failure_never_starts_the_uploader(self) -> None:
        store, market = self._market(lambda *a: (_ for _ in ()).throw(AssertionError("POST 금지")))
        store.write([{"obs_id": "prev"}])
        rc = self._run(A.RunOptions(capture_min=None, pause_on_exit=False, attach_log=False), "",
                       factory=_factory(start_ok=False, start_msg="Npcap 없음"),
                       market_setup=lambda o, s, i: market)
        self.assertEqual(rc, A.RC_ENGINE)
        self.assertFalse(market.uploader.is_alive())
        self.assertEqual(len(store.pending()), 1, "엔진이 못 뜨면 업로더도 안 뜬다 — 스풀은 그대로")

    def test_close_drains_the_queue_to_spool_when_the_thread_is_stuck(self) -> None:
        """POST 가 붙들린 채 join 이 시간 초과해도 큐는 메인 스레드가 스풀에 내린다 — 관측을 잃지 않는다."""
        release = threading.Event()

        def slow_post(*a):
            release.wait(10.0)
            from yuktracker import hub_client
            return hub_client.Response(0, error="network")

        store, market = self._market(slow_post, batch_wait=0.01)
        store.write([{"obs_id": "stuck"}])
        market.start(lambda m: None)
        try:
            time.sleep(0.2)                                  # 스레드가 slow_post 안에 들어갈 시간
            market.uploader.enqueue({"obs_id": "late"})
            with mock.patch.object(market.uploader, "join", lambda timeout=None: None):   # join 시간 초과 흉내
                market.close()
            self.assertEqual(market.unsaved, 0)
            names = sorted(p.name for p in store.pending())
            self.assertEqual(len(names), 2, "stuck 파일 + late 가 새 스풀 파일로")
        finally:
            release.set()
            market.uploader.join(timeout=5.0)


class _SlowQuit:
    """stdin 흉내 — 이벤트가 켜질 때까지 기다렸다가 'q' 한 줄을 준 뒤 EOF."""

    def __init__(self, event: threading.Event) -> None:
        self._event = event

    def __iter__(self):                                   # _Console 은 `for line in stdin` 으로 읽는다
        self._event.wait(10.0)
        yield "q\n"


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

    def test_selftest_rejects_register_and_capture_options(self) -> None:
        """자가진단은 등록·수집을 하지 않는다 — 조용히 무시하면 안내를 따라온 사람이 같은 경고를 또 본다."""
        from yuktracker import cli
        for argv in (["--selftest", "--invite-code", "abc"], ["--selftest", "--capture"],
                     ["--selftest", "--device-label", "x"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr", new=io.StringIO()) as err:
                with self.assertRaises(SystemExit) as cm:
                    cli.main(argv)
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("--selftest 없이", err.getvalue())

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


class StatusTickTest(unittest.TestCase):
    """관측 모드 상태 줄 — 지인 PC 의 콘솔이 "되고 있는지"를 말해 주는 유일한 수단(PR-Y5)."""

    def test_first_capture_announces_once(self) -> None:
        state = {}
        first = A._status_tick(state, capturing=True, pages=0, now=0.0)
        self.assertIn("육의전을 한 번 열면", first)
        self.assertNotIn("캡처 시작", first)   # 그 문구는 엔진 상태 줄이 이미 찍는다 — 되풀이하지 않는다
        self.assertIsNone(A._status_tick(state, capturing=True, pages=0, now=1.0))

    def test_waiting_for_flow_reminds_on_the_interval(self) -> None:
        state = {}
        # 시작 직후의 미개통은 조용하다 — 게임이 켜져 있으면 곧 잡힌다
        self.assertIsNone(A._status_tick(state, capturing=False, pages=0, now=0.0))
        self.assertIsNone(A._status_tick(state, capturing=False, pages=0,
                                         now=A.FLOW_REMIND_SEC - 1))
        line = A._status_tick(state, capturing=False, pages=0, now=A.FLOW_REMIND_SEC)
        self.assertIn("대기 중", line)
        self.assertIsNone(A._status_tick(state, capturing=False, pages=0,
                                         now=A.FLOW_REMIND_SEC + 1))
        self.assertIsNotNone(A._status_tick(state, capturing=False, pages=0,
                                            now=2 * A.FLOW_REMIND_SEC))

    def test_flow_loss_is_announced_and_recovery_too(self) -> None:
        state = {}
        A._status_tick(state, capturing=True, pages=0, now=0.0)
        lost = A._status_tick(state, capturing=False, pages=0, now=5.0)
        self.assertIn("끊겼습니다", lost)
        back = A._status_tick(state, capturing=True, pages=0, now=6.0)
        self.assertIn("다시 잡았습니다", back)

    def test_idle_reminder_stops_after_the_first_page(self) -> None:
        state = {}
        A._status_tick(state, capturing=True, pages=0, now=0.0)
        self.assertIsNone(A._status_tick(state, capturing=True, pages=0,
                                         now=A.IDLE_REMIND_SEC - 1))
        line = A._status_tick(state, capturing=True, pages=0, now=A.IDLE_REMIND_SEC)
        self.assertIn("육의전 창을 열어", line)
        # 한 번이라도 목록을 보면 조용해진다(목록 줄이 대신 찍힌다)
        self.assertIsNone(A._status_tick(state, capturing=True, pages=1,
                                         now=10 * A.IDLE_REMIND_SEC))

    def test_engine_without_the_method_is_treated_as_not_capturing(self) -> None:
        self.assertFalse(A._capturing(object()))

    def test_capturing_means_a_tracked_flow_not_just_an_open_handle(self) -> None:
        """거상을 끄면 핸들은 열린 채 남고 `tracked_flows` 만 0 이 된다 — 그때 '끊겼습니다' 가 떠야 한다."""
        engine = SimpleNamespace(is_capturing=lambda: True,
                                 health_snapshot=lambda: {"tracked_flows": 1})
        self.assertTrue(A._capturing(engine))
        engine.health_snapshot = lambda: {"tracked_flows": 0}
        self.assertFalse(A._capturing(engine))
        engine.is_capturing = lambda: False
        engine.health_snapshot = lambda: {"tracked_flows": 1}
        self.assertFalse(A._capturing(engine))


class ObserveModeStatusTest(_Base):
    """배선 — 관측 모드(수집 창 없음)에서도 흐름 대기 줄이 실제로 콘솔에 찍힌다."""

    def test_waiting_line_reaches_the_console(self) -> None:
        clock = [0.0]

        def fake_clock():
            return clock[0]

        def fake_sleep(s):
            # 틱을 10초씩 건너뛰고, 안내가 두 번 날 만큼 지나면 Ctrl+C 로 빠져나온다
            clock[0] += max(s, 10.0)
            if clock[0] > 3 * A.FLOW_REMIND_SEC:
                raise KeyboardInterrupt

        opts = A.RunOptions(capture_min=None, pause_on_exit=False, attach_log=False)
        rc = self._run(opts, "", factory=_factory(capturing=False), sleep=fake_sleep,
                       clock=fake_clock)
        self.assertEqual(rc, A.RC_OK)
        self.assertIn("대기 중", self.out.getvalue())

    def test_already_capturing_at_loop_start_is_not_announced_again(self) -> None:
        """엔진이 이미 흐름을 잡은 채 루프에 들어오면(수집 모드가 그렇다) 틱이 시작을 다시 알리지 않는다."""
        clock = [0.0]

        def fake_clock():
            return clock[0]

        def fake_sleep(s):
            clock[0] += max(s, 1.0)
            if clock[0] > 5.0:
                raise KeyboardInterrupt

        opts = A.RunOptions(capture_min=None, pause_on_exit=False, attach_log=False)
        rc = self._run(opts, "", factory=_factory(capturing=True), sleep=fake_sleep,
                       clock=fake_clock)
        self.assertEqual(rc, A.RC_OK)
        self.assertNotIn("육의전을 한 번 열면", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()
