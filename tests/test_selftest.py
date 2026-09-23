"""자가진단(`--selftest`) — 프로브 조합 → 줄 표시·종료 코드 (PR-Y5).

판정부 `selftest.run` 은 순수 함수이고 실측은 전부 `Probes` 주입이라, 이 테스트는 Npcap·네트워크·
사용자 프로필(`%APPDATA%`)에 닿지 않는다. 보는 것은 **지인이 읽을 줄이 상황마다 달라지는가**와
**종료 코드가 실패/경고를 구분하는가**다.
"""
from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from yuktracker import app_config, hub_client, selftest


class _Names:
    def __init__(self, rows: int = 4001, path: str = "C:/게임/gersang.gcs") -> None:
        self.rows = rows
        self.gcs_path = path


def _probes(**over) -> selftest.Probes:
    """전부 통과하는 기본 조합 — 테스트마다 어긋나게 할 항목만 바꾼다."""
    cfg = over.pop("cfg", None) or app_config.Config(
        hub_url="https://hub.example", hub_device_id="d-1", hub_token="tok", local_id="abcd1234")
    base = dict(
        is_admin=lambda: True,
        capture=lambda: selftest.CaptureProbe(npcap_ok=True, npcap_msg="Npcap 사용 가능",
                                              capturing=True, packets=12),
        game_pids=lambda: [1234],
        item_names=lambda: _Names(),
        config=lambda: cfg,
        hub_status=lambda url: hub_client.Response(200, {"service": "yuktracker-hub", "v": 1}),
        hub_ping=lambda url, token: hub_client.Response(
            200, {"ok": True, "v": 1, "device_id": "d-1", "label": "친구1"}),
        spool_counts=lambda: (0, 0),
        env={})
    base.update(over)
    return selftest.Probes(**base)


def _by_name(checks) -> dict:
    return {c.name: c for c in checks}


class HappyPathTest(unittest.TestCase):
    def test_all_green_is_rc_zero(self) -> None:
        checks, rc = selftest.run(_probes())
        self.assertEqual(rc, selftest.RC_OK)
        self.assertEqual({c.mark for c in checks}, {selftest.OK})
        marks = _by_name(checks)
        self.assertIn("친구1", marks["기기 토큰"].detail)      # 허브가 아는 별칭을 보여 준다
        self.assertIn("4001", marks["아이템 표"].detail)
        self.assertIn("12", marks["패킷 흐름"].detail)

    def test_render_shows_every_check_and_the_summary(self) -> None:
        checks, rc = selftest.run(_probes())
        text = "\n".join(selftest.render(checks, rc))
        for name in ("관리자 권한", "Npcap", "패킷 흐름", "아이템 표", "허브 설정",
                     "허브 도달", "기기 토큰", "업로드 대기"):
            self.assertIn(name, text)
        self.assertIn("종료 코드 0", text)
        self.assertIn("전부 정상", text)


class FailureTest(unittest.TestCase):
    def test_no_admin_and_no_npcap_are_failures(self) -> None:
        checks, rc = selftest.run(_probes(
            is_admin=lambda: False,
            capture=lambda: selftest.CaptureProbe(npcap_ok=False, npcap_msg="Npcap 미설치",
                                                  npcap_reason="npcap_not_installed")))
        marks = _by_name(checks)
        self.assertEqual(rc, selftest.RC_FAIL)
        self.assertEqual(marks["관리자 권한"].mark, selftest.FAIL)
        self.assertIn("관리자 권한으로 실행", marks["관리자 권한"].detail)
        self.assertEqual(marks["Npcap"].mark, selftest.FAIL)
        self.assertIn("npcap.com", marks["Npcap"].detail)
        self.assertNotIn("화면 감지", marks["Npcap"].detail)
        # Npcap 이 없으면 흐름은 확인 자체가 불가능하다 — 실패가 아니라 미확인
        self.assertEqual(marks["패킷 흐름"].mark, selftest.SKIP)

    def test_not_elevated_does_not_tell_the_user_to_reinstall_npcap(self) -> None:
        """벤더의 사유 문구("화면 감지를 계속 사용합니다")도, 엉뚱한 재설치 안내도 내지 않는다."""
        checks, _rc = selftest.run(_probes(
            is_admin=lambda: False,
            capture=lambda: selftest.CaptureProbe(
                npcap_ok=False, npcap_msg="관리자 권한이 아닙니다 — 화면 감지를 계속 사용합니다",
                npcap_reason="not_elevated")))
        detail = _by_name(checks)["Npcap"].detail
        self.assertIn("관리자 권한으로 다시 실행", detail)
        self.assertNotIn("npcap.com", detail)
        self.assertNotIn("화면 감지", detail)

    def test_game_off_is_a_warning_and_flow_is_not_judged(self) -> None:
        checks, rc = selftest.run(_probes(
            game_pids=lambda: [],
            capture=lambda: selftest.CaptureProbe(npcap_ok=True, npcap_msg="ok", capturing=False)))
        marks = _by_name(checks)
        self.assertEqual(rc, selftest.RC_WARN)
        self.assertEqual(marks["거상 실행"].mark, selftest.WARN)
        self.assertEqual(marks["패킷 흐름"].mark, selftest.SKIP)

    def test_game_on_but_no_flow_is_a_failure(self) -> None:
        checks, rc = selftest.run(_probes(
            capture=lambda: selftest.CaptureProbe(npcap_ok=True, npcap_msg="ok", capturing=False)))
        marks = _by_name(checks)
        self.assertEqual(rc, selftest.RC_FAIL)
        self.assertEqual(marks["패킷 흐름"].mark, selftest.FAIL)
        self.assertIn("어댑터", marks["패킷 흐름"].detail)

    def test_unreachable_hub_fails_and_token_is_not_judged(self) -> None:
        checks, rc = selftest.run(_probes(
            hub_status=lambda url: hub_client.Response(0, error="network", text="연결 거부")))
        marks = _by_name(checks)
        self.assertEqual(rc, selftest.RC_FAIL)
        self.assertEqual(marks["허브 도달"].mark, selftest.FAIL)
        self.assertEqual(marks["기기 토큰"].mark, selftest.SKIP)

    def test_rejected_token_and_id_mismatch_are_failures(self) -> None:
        rejected = selftest.run(_probes(
            hub_ping=lambda url, token: hub_client.Response(401, {"error": "unauthorized"})))
        self.assertEqual(rejected[1], selftest.RC_FAIL)
        self.assertIn("--invite-code", _by_name(rejected[0])["기기 토큰"].detail)
        mismatch = selftest.run(_probes(
            hub_ping=lambda url, token: hub_client.Response(
                200, {"ok": True, "device_id": "d-other"})))
        self.assertEqual(mismatch[1], selftest.RC_FAIL)
        detail = _by_name(mismatch[0])["기기 토큰"].detail
        self.assertIn("d-1", detail)
        self.assertIn("d-other", detail)

    def test_old_hub_without_ping_is_named(self) -> None:
        checks, rc = selftest.run(_probes(
            hub_ping=lambda url, token: hub_client.Response(404, text="Not Found")))
        self.assertEqual(rc, selftest.RC_FAIL)
        self.assertIn("옛 판", _by_name(checks)["기기 토큰"].detail)

    def test_a_probe_that_raises_does_not_kill_the_run(self) -> None:
        def boom():
            raise RuntimeError("프로브 실패")

        checks, rc = selftest.run(_probes(is_admin=boom, capture=boom, item_names=boom,
                                          game_pids=boom, spool_counts=boom))
        self.assertEqual(rc, selftest.RC_FAIL)
        self.assertEqual(len(checks), 9)
        self.assertIn("읽지 못했습니다", _by_name(checks)["업로드 대기"].detail)
        self.assertEqual(_by_name(checks)["관리자 권한"].mark, selftest.FAIL)
        self.assertEqual(_by_name(checks)["Npcap"].mark, selftest.FAIL)


class ConfigTest(unittest.TestCase):
    def test_not_registered_yet_is_a_warning_with_the_next_step(self) -> None:
        cfg = app_config.Config(hub_url="https://hub.example", local_id="abcd1234")
        checks, rc = selftest.run(_probes(cfg=cfg))
        marks = _by_name(checks)
        self.assertEqual(rc, selftest.RC_WARN)
        self.assertEqual(marks["기기 토큰"].mark, selftest.WARN)
        self.assertIn("--invite-code", marks["기기 토큰"].detail)
        self.assertIn("미등록", marks["허브 설정"].detail)

    def test_no_hub_url_disables_the_network_checks(self) -> None:
        checks, rc = selftest.run(_probes(cfg=app_config.Config(local_id="abcd1234")))
        marks = _by_name(checks)
        self.assertEqual(rc, selftest.RC_WARN)
        self.assertEqual(marks["허브 설정"].mark, selftest.WARN)
        self.assertEqual(marks["허브 도달"].mark, selftest.SKIP)
        self.assertEqual(marks["기기 토큰"].mark, selftest.SKIP)

    def test_url_origin_is_named(self) -> None:
        empty = app_config.Config(local_id="abcd1234")
        cli = selftest.run(_probes(cfg=empty, cli_hub_url="https://cli.example"))
        self.assertIn("--hub-url", _by_name(cli[0])["허브 설정"].detail)
        env = selftest.run(_probes(cfg=empty, env={app_config.ENV_HUB_URL: "https://env.example"}))
        self.assertIn("환경변수", _by_name(env[0])["허브 설정"].detail)
        self.assertIn("설정 파일", _by_name(selftest.run(_probes())[0])["허브 설정"].detail)

    def test_empty_env_isolates_from_the_real_environment(self) -> None:
        """`env={}` 는 '환경변수 없음'이다 — 빌드 PC 에 YUKTRACKER_HUB_URL 이 있어도 테스트가 흔들리지 않는다."""
        empty = app_config.Config(local_id="abcd1234")
        with mock.patch.dict(os.environ, {app_config.ENV_HUB_URL: "https://real.example"}):
            isolated = selftest.run(_probes(cfg=empty, env={}))
            self.assertEqual(_by_name(isolated[0])["허브 도달"].mark, selftest.SKIP)
            live = selftest.run(_probes(cfg=empty, env=None))
            self.assertIn("환경변수", _by_name(live[0])["허브 설정"].detail)

    def test_a_200_that_is_not_the_hub_fails_reachability(self) -> None:
        """오타 주소의 다른 서비스·접속 포털도 200 을 준다 — 허브 서명이 없으면 '도달' 이 아니다."""
        checks, rc = selftest.run(_probes(
            hub_status=lambda url: hub_client.Response(200, {}, text="<html>captive</html>")))
        marks = _by_name(checks)
        self.assertEqual(rc, selftest.RC_FAIL)
        self.assertEqual(marks["허브 도달"].mark, selftest.FAIL)
        self.assertIn("허브가 아닙니다", marks["허브 도달"].detail)
        self.assertEqual(marks["기기 토큰"].mark, selftest.SKIP)   # 옛 판 안내로 빠지지 않는다

    def test_engine_start_failure_does_not_tell_the_user_to_install_npcap(self) -> None:
        checks, _rc = selftest.run(_probes(
            capture=lambda: selftest.CaptureProbe(npcap_ok=False, npcap_msg="스레드를 띄우지 못했습니다",
                                                  npcap_reason="")))
        detail = _by_name(checks)["Npcap"].detail
        self.assertIn("스레드를 띄우지", detail)
        self.assertNotIn("npcap.com", detail)

    def test_not_registered_hint_says_to_drop_selftest(self) -> None:
        cfg = app_config.Config(hub_url="https://hub.example", local_id="abcd1234")
        detail = _by_name(selftest.run(_probes(cfg=cfg))[0])["기기 토큰"].detail
        self.assertIn("--selftest 없이", detail)

    def test_no_upload_option_skips_hub_checks(self) -> None:
        checks, rc = selftest.run(_probes(upload=False))
        marks = _by_name(checks)
        self.assertEqual(rc, selftest.RC_WARN)
        self.assertIn("--no-upload", marks["허브 설정"].detail)
        self.assertEqual(marks["허브 도달"].mark, selftest.SKIP)

    def test_pending_spool_is_reported_as_a_warning(self) -> None:
        checks, rc = selftest.run(_probes(spool_counts=lambda: (3, 1)))
        detail = _by_name(checks)["업로드 대기"].detail
        self.assertEqual(rc, selftest.RC_WARN)
        self.assertIn("대기 3", detail)
        self.assertIn("격리 1", detail)


class MainTest(unittest.TestCase):
    def test_main_prints_and_returns_the_code_without_touching_the_console(self) -> None:
        out = io.StringIO()
        rc = selftest.main(probes=_probes(), out=out, pause=False)
        self.assertEqual(rc, selftest.RC_OK)
        self.assertIn("자가진단", out.getvalue())
        self.assertNotIn("Enter", out.getvalue())

    def test_pause_waits_for_a_line(self) -> None:
        out, stdin = io.StringIO(), io.StringIO("\n")
        selftest.main(probes=_probes(), out=out, pause=True, stdin=stdin)
        self.assertIn("Enter", out.getvalue())

    def test_pause_survives_a_closed_stdin(self) -> None:
        out = io.StringIO()
        closed = io.StringIO()
        closed.close()
        self.assertEqual(selftest.main(probes=_probes(), out=out, pause=True, stdin=closed),
                         selftest.RC_OK)


class _FakeEngine:
    def __init__(self, provider, *, capturing=False):
        self.provider = provider
        self.capturing = capturing
        self.stopped = False

    def start(self):
        return True, "패킷 스니퍼 시작"

    def is_capturing(self):
        return self.capturing

    def health_snapshot(self):
        return {"packets": 0}

    def stop(self):
        self.stopped = True


class ProbeCaptureTest(unittest.TestCase):
    """실 프로브의 대기 규칙 — Npcap·엔진은 가짜, `pcap_ffi.npcap_available` 만 패치한다."""

    def _run(self, pids, *, wait_sec=15.0):
        from yuktracker.seassist import pcap_ffi
        clock = [0.0]
        slept = []

        def sleep(s):
            slept.append(s)
            clock[0] += s

        engines = []

        def factory(provider):
            engines.append(_FakeEngine(provider))
            return engines[-1]

        with mock.patch.object(pcap_ffi, "npcap_available", return_value=(True, "ok")):
            probe = selftest.probe_capture(wait_sec, factory=factory, pids=pids,
                                           sleep=sleep, clock=lambda: clock[0])
        return probe, slept, engines[0]

    def test_no_game_process_does_not_wait_for_a_flow(self) -> None:
        probe, slept, engine = self._run(lambda: {})
        self.assertEqual(slept, [])            # 15초를 태우지 않는다 — 판정은 어차피 '거상이 꺼져 있다'
        self.assertFalse(probe.capturing)
        self.assertTrue(engine.stopped)

    def test_game_process_present_waits_up_to_the_limit(self) -> None:
        probe, slept, engine = self._run(lambda: {0: 1234}, wait_sec=2.0)
        self.assertGreater(len(slept), 0)
        self.assertLessEqual(sum(slept), 2.0 + selftest._POLL_SEC)
        self.assertTrue(engine.stopped)

    def test_engine_start_failure_carries_no_npcap_reason(self) -> None:
        from yuktracker.seassist import pcap_ffi

        class _Dead(_FakeEngine):
            def start(self):
                return False, "스니퍼 스레드 시작 실패"

        with mock.patch.object(pcap_ffi, "npcap_available", return_value=(True, "ok")):
            probe = selftest.probe_capture(1.0, factory=lambda provider: _Dead(provider),
                                           pids=lambda: {0: 1}, sleep=lambda s: None)
        self.assertFalse(probe.npcap_ok)
        self.assertEqual(probe.npcap_reason, "")
        self.assertIn("시작 실패", probe.npcap_msg)


if __name__ == "__main__":
    unittest.main()
