"""사통팔달 관측기 — 클라 간 중복 제거 · 봉투 · 하트비트 업로더 · 배선(`setup_satong`) · 설정·CLI·자가진단 줄.

네트워크·사용자 프로필을 건드리지 않는다(설정 경로·스풀 폴더·전송 함수 주입). 데이터는 합성.
"""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from yuktracker import agent as A
from yuktracker import app_config, cli, hub_client, paths, selftest, spool
from yuktracker import market_observer as MO
from yuktracker import satong_observer as SO
from yuktracker.seassist.gersang_protocol import SatongObservation
from yuktracker.seassist.packet_satong import parse_satong

from packet_frames import SENDER_A, TEXT_A, satong_body
from test_selftest import _by_name, _probes


def _obs(ts: float, **kw) -> tuple:
    body = satong_body(**kw)
    return parse_satong(body), SatongObservation(ts, 0x1F82, body)


class _Clock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class ObserverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sent: list[dict] = []
        self.said: list[str] = []
        self.mono = _Clock(1000.0)
        self.o = SO.SatongObserver("abcd1234", enqueue=self.sent.append, say=self.said.append,
                                   wall=lambda: 1_800_000_000.0, mono=self.mono)

    def test_same_shout_from_two_clients_is_one_message(self):
        self.o(0, *_obs(990.0))
        self.o(1, *_obs(990.004))                                # 같은 PC 의 둘째 클라 — 수 ms 뒤
        self.o(0, *_obs(1005.0))                                 # 15초 뒤 같은 글 = 다음 큐 순번(진짜 반복)
        self.assertEqual((self.o.messages, self.o.duplicates), (2, 1))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len([s for s in self.said if "첫 사통팔달" in s]), 1)

    def test_envelope_fields_and_epoch_time(self):
        self.o(2, *_obs(990.0, mid=5))
        (env,) = self.sent
        self.assertEqual((env["sender"], env["text"], env["opcode"], env["mid"], env["anomalies"]),
                         (SENDER_A, TEXT_A, 0x1F82, 5, []))
        self.assertAlmostEqual(env["agent_ts"], 1_800_000_000.0 - 10.0)
        self.assertRegex(env["obs_id"], r"^abcd1234:1799999990000:[0-9a-f]{12}$")
        self.assertEqual(env["tail"], "00010000000000")
        c = self.o.collector(connected=True)
        self.assertEqual((c["client_connected"], c["messages"], c["last_message_ts"]),
                         (True, 1, env["agent_ts"]))

    def test_dedup_table_is_swept(self):
        for k in range(SO._DEDUP_SWEEP_AT + 10):
            self.o(0, *_obs(1000.0 + 20 * k, text=f"합성 {k}"))
        self.assertLessEqual(len(self.o._recent), SO._DEDUP_SWEEP_AT)

    def test_enqueue_failure_never_escapes(self):
        o = SO.SatongObserver("x", enqueue=lambda env: (_ for _ in ()).throw(RuntimeError("full")))
        o(0, *_obs(1.0))                                        # 던지지 않는다


class UploaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = spool.Spool(Path(self.tmp.name) / "satong")
        self.clock = _Clock(100.0)
        self.calls: list[tuple] = []
        self.responses: list[hub_client.Response] = []
        self.status = {"client_connected": True, "last_message_ts": None, "messages": 0, "agent_ts": 1.0}
        self.said: list[str] = []

    def _post(self, hub_url, token, device_id, server_id, messages, collector):
        self.calls.append((device_id, server_id, list(messages), dict(collector)))
        return self.responses.pop(0) if self.responses else hub_client.Response(200, {"ok": True})

    def _up(self, **kw) -> SO.SatongUploader:
        return SO.SatongUploader(self.store, hub_url="https://hub", token="tok", device_id=lambda: "d-1",
                                 server_id=7, collector=lambda: self.status, say=self.said.append,
                                 post=self._post, clock=self.clock, heartbeat_sec=60.0, batch_wait=0.01, **kw)

    def test_heartbeat_when_idle_then_every_interval(self):
        up = self._up()
        up.tick()
        self.assertEqual(self.calls, [("d-1", 7, [], self.status)])
        self.clock.t += 59
        up.tick()
        self.assertEqual(len(self.calls), 1)
        self.clock.t += 2
        up.tick()
        self.assertEqual((len(self.calls), up.heartbeats), (2, 2))

    def test_pending_batch_goes_first_and_counts_as_heartbeat(self):
        up = self._up()
        up.enqueue({"obs_id": "x:1:a", "agent_ts": 1.0, "sender": "보낸이A", "text": "합성"})
        up.drain_queue(wait=False)
        up.tick()
        self.assertEqual([len(c[2]) for c in self.calls], [1])   # 메시지 배치 1회, 빈 하트비트 없음
        self.assertEqual((up.uploaded, self.store.pending()), (1, []))
        self.clock.t += 30
        up.tick()
        self.assertEqual(len(self.calls), 1)                      # 방금 보낸 배치가 하트비트를 겸했다

    def test_not_allowed_stops_the_uploader_and_keeps_the_spool(self):
        self.responses.append(hub_client.Response(403, {"ok": False, "error": "satong_not_allowed"},
                                                  "satong_not_allowed"))
        up = self._up()
        up.tick()
        self.assertTrue(up.halted)
        self.assertIn("satong_slots", up.stopped_reason)
        up.enqueue({"obs_id": "x:1:a", "agent_ts": 1.0, "sender": "보낸이A", "text": "합성"})
        up.drain_queue(wait=False)
        self.clock.t += 600
        up.tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.store.pending()), 1)

    def test_failed_heartbeat_retries_at_the_interval_and_says_so_once(self):
        self.responses += [hub_client.Response(0, error="network", text="down")] * 2
        up = self._up()
        up.tick()
        self.assertEqual((up.retry_delay, up.heartbeat_due()), (1.0, False))
        self.clock.t += 30
        up.tick()
        self.assertEqual(len(self.calls), 1)                      # 백오프는 지났지만 주기 전
        self.clock.t += 31
        up.tick()
        self.assertEqual((len(self.calls), up.retry_delay), (2, 2.0))
        self.clock.t += 61
        up.tick()
        self.assertEqual((len(self.calls), up.heartbeats), (3, 1))
        fails = [s for s in self.said if "하트비트 실패" in s]
        self.assertEqual(len(fails), 1)
        self.assertIn("60초 뒤", fails[0])
        self.assertEqual(len([s for s in self.said if "다시 들어갑니다" in s]), 1)

    def test_collector_failure_sends_disconnected_status(self):
        up = SO.SatongUploader(self.store, hub_url="h", token="t", device_id=lambda: "d-1", server_id=7,
                               collector=lambda: 1 / 0, post=self._post, clock=self.clock)
        up.tick()
        self.assertFalse(self.calls[0][3]["client_connected"])

    def _backlog(self, n: int) -> None:
        for k in range(n):
            self.store.write([{"obs_id": f"x:{k}:{k:012x}", "agent_ts": 1.0 + k, "sender": "보낸이A", "text": f"합성 {k}"}])

    def test_backlog_is_sent_in_grouped_requests(self):
        """리뷰 반영 — 파일마다 요청하면 밀린 수천 파일이 기기 속도제한을 다 쓴다. 100건까지 한 요청으로 묶는다."""
        self._backlog(250)
        up = self._up()
        up.tick()
        self.assertEqual([len(c[2]) for c in self.calls], [100, 100, 50])
        self.assertEqual([c[2][0]["text"] for c in self.calls], ["합성 0", "합성 100", "합성 200"])   # 오래된 것부터
        self.assertEqual((up.uploaded, self.store.pending()), (250, []))

    def test_rejected_group_falls_back_to_single_files_and_quarantines_only_the_bad_one(self):
        self._backlog(3)
        bad = hub_client.Response(400, {"ok": False, "error": "bad_request", "index": 1}, "bad_request")
        self.responses += [bad, hub_client.Response(200, {"ok": True}), bad, hub_client.Response(200, {"ok": True})]
        up = self._up()
        up.tick()
        self.assertEqual([len(c[2]) for c in self.calls], [3, 1, 1, 1])
        self.assertEqual((up.uploaded, up.quarantined, self.store.pending()), (2, 1, []))

    def test_group_retry_keeps_every_file(self):
        self._backlog(3)
        self.responses.append(hub_client.Response(0, error="network", text="down"))
        up = self._up()
        up.tick()
        self.assertEqual((len(self.calls), len(self.store.pending()), up.retry_delay), (1, 3, 1.0))

    def test_undeletable_file_is_not_resent_in_the_same_pass(self):
        self._backlog(1)
        up = self._up()
        with mock.patch.object(self.store, "done", lambda p: None):
            up.flush_pending()
        self.assertEqual(len(self.calls), 1)

    def test_idle_tick_does_not_rescan_the_spool_when_no_heartbeat_is_due(self):
        up = self._up()
        up.tick()                                                # 첫 하트비트
        with mock.patch.object(self.store, "pending", wraps=self.store.pending) as pending:
            self.clock.t += 2
            up.tick()
        self.assertEqual(pending.call_count, 1)                  # flush_pending 의 한 번뿐


class SetupSatongTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        self.said: list[str] = []

    def _market(self, uploader=True) -> A.Market:
        up = SimpleNamespace(hub_url="https://hub", token="tok", device_id=lambda: "d-1") if uploader else None
        return A.Market(observer=MO.MarketObserver("abcd1234", MO.ItemNames(None)), uploader=up)

    def _setup(self, opts=None, **kw) -> A.Satong:
        return A.setup_satong(opts or A.RunOptions(), self.said.append, kw.pop("market", self._market()),
                              config_path=self.config, spool_dir=self.root / "satong", **kw)

    def test_off_by_default(self):
        s = self._setup()
        self.assertEqual((s.cb, s.uploader, s.observer, s.server_id), (None, None, None, None))
        self.assertEqual(self.said, [])

    def test_cli_turns_it_on_with_the_market_hub_and_its_own_spool(self):
        s = self._setup(A.RunOptions(satong_server_id=7))
        self.assertIs(s.cb, s.observer)
        self.assertEqual((s.server_id, s.uploader.server_id, s.uploader.hub_url, s.uploader.device_id()),
                         (7, 7, "https://hub", "d-1"))
        self.assertEqual(s.uploader.spool.dir, self.root / "satong")
        self.assertEqual(s.observer.local_id, "abcd1234")
        self.assertIn("서버 7", "\n".join(self.said))

    def test_config_key_and_cli_precedence(self):
        app_config.save(app_config.Config(satong_server_id="9"), self.config)
        self.assertEqual(self._setup().server_id, 9)
        self.assertEqual(self._setup(A.RunOptions(satong_server_id=7)).server_id, 7)

    def test_without_market_upload_it_only_counts(self):
        s = self._setup(A.RunOptions(satong_server_id=7), market=self._market(uploader=False))
        self.assertIsNone(s.uploader)
        self.assertIsNotNone(s.cb)
        self.assertIn("건수만", "\n".join(self.said))

    def test_collector_status_uses_the_engine_probe(self):
        s = self._setup(A.RunOptions(satong_server_id=7))
        self.assertFalse(s.collector_status()["client_connected"])
        s.connected = lambda: True
        self.assertTrue(s.collector_status()["client_connected"])
        s.connected = lambda: 1 / 0
        self.assertFalse(s.collector_status()["client_connected"])

    def test_status_tick_reports_every_interval(self):
        s = self._setup(A.RunOptions(satong_server_id=7))
        s.connected = lambda: True
        self.assertIsNone(A._satong_tick(s, now=0.0))
        s.observer.messages = 40
        self.assertIsNone(A._satong_tick(s, now=A.SATONG_REPORT_SEC - 1))
        line = A._satong_tick(s, now=A.SATONG_REPORT_SEC)
        self.assertIn("최근 10분 40건", line)
        line = A._satong_tick(s, now=2 * A.SATONG_REPORT_SEC)
        self.assertIn("0건", line)
        self.assertIn("사통이 안 왔습니다", line)


class CloseBudgetTest(unittest.TestCase):
    def test_uploaders_share_one_join_budget(self):
        """리뷰 반영 — 둘을 3초씩 차례로 기다리면 6초가 돼 콘솔 닫기 유예(약 5초)를 넘긴다. 먼저 둘 다 멈추라고 알리고,
        남은 예산 안에서 기다린다."""
        clock = _Clock(0.0)
        log: list = []

        class _Stuck:
            def __init__(self, name):
                self.name = name

            def stop(self):
                log.append(("stop", self.name, clock.t))

            def is_alive(self):
                return True

            def join(self, timeout):
                log.append(("join", self.name, round(timeout, 3)))
                clock.t += timeout                                  # POST 에 붙들려 끝까지 기다린다

            def drain_queue(self, wait=True):
                return None

            def queue_size(self):
                return 0

        a, b = A.Market(uploader=_Stuck("market")), A.Satong(uploader=_Stuck("satong"))
        A._close_uploads(a, b, budget=3.0, clock=clock)
        self.assertEqual(log[:2], [("stop", "market", 0.0), ("stop", "satong", 0.0)])
        self.assertEqual([e for e in log if e[0] == "join"], [("join", "market", 3.0), ("join", "satong", 0.0)])
        self.assertEqual(len([e for e in log if e[0] == "stop"]), 2, "close() 가 stop 을 다시 부르지 않는다")


class ConfigCliTest(unittest.TestCase):
    def test_resolve_server_id(self):
        r = app_config.resolve_satong_server_id
        self.assertIsNone(r(None, app_config.Config()))
        self.assertEqual(r(None, app_config.Config(satong_server_id=" 7 ")), 7)
        self.assertIsNone(r(None, app_config.Config(satong_server_id="세종")))
        self.assertIsNone(r(None, app_config.Config(satong_server_id="0")))
        self.assertEqual(r(3, app_config.Config(satong_server_id="7")), 3)

    def test_config_round_trip_keeps_the_key(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            app_config.save(app_config.Config(satong_server_id="7", extra={"other": 1}), p)
            cfg = app_config.load(p)
            self.assertEqual((cfg.satong_server_id, cfg.extra), ("7", {"other": 1}))

    def test_cli_flag(self):
        ap = cli.build_parser()
        self.assertIsNone(ap.parse_args([]).satong)
        self.assertEqual(ap.parse_args(["--satong", "7"]).satong, 7)
        for bad in ("0", "-1", "세종"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit), \
                    mock.patch("sys.stderr", io.StringIO()):
                ap.parse_args(["--satong", bad])

    def test_satong_spool_is_below_the_market_spool_and_not_globbed_by_it(self):
        self.assertEqual(paths.satong_spool_dir().parent, paths.spool_dir())
        with tempfile.TemporaryDirectory() as d:
            market = spool.Spool(Path(d))
            satong = spool.Spool(Path(d) / paths.SATONG_SPOOL_DIR_NAME)
            satong.write([{"obs_id": "x"}])
            self.assertEqual(market.pending(), [])
            self.assertEqual(len(satong.pending()), 1)


class SelftestLineTest(unittest.TestCase):
    def test_no_line_when_off(self):
        checks, _ = selftest.run(_probes())
        self.assertNotIn("사통 수집", _by_name(checks))

    def test_on_and_registered_is_ok(self):
        checks, rc = selftest.run(_probes(cli_satong_server_id=7))
        c = _by_name(checks)["사통 수집"]
        self.assertEqual(c.mark, selftest.OK)
        self.assertIn("서버 7", c.detail)
        self.assertEqual(rc, selftest.RC_OK)

    def test_on_without_upload_warns(self):
        checks, _ = selftest.run(_probes(cli_satong_server_id=7, upload=False))
        self.assertEqual(_by_name(checks)["사통 수집"].mark, selftest.WARN)

    def test_bad_config_value_warns(self):
        cfg = app_config.Config(hub_url="https://hub.example", hub_device_id="d-1", hub_token="tok",
                                satong_server_id="세종")
        checks, _ = selftest.run(_probes(cfg=cfg))
        self.assertEqual(_by_name(checks)["사통 수집"].mark, selftest.WARN)


if __name__ == "__main__":
    unittest.main()
