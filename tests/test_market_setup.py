"""첫 실행 등록 UX 와 관측 배선 — `agent.setup_market` · `hub_setup` (PR-Y1b).

네트워크·사용자 프로필을 건드리지 않는다: 설정 경로·스풀 폴더·등록 함수를 전부 주입한다.
"""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from yuktracker import agent as A
from yuktracker import app_config, hub_client, hub_setup
from yuktracker import market_observer as MO


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        self.spool = self.root / "spool"
        self.said: list[str] = []
        self.registers: list[tuple] = []

    def _register(self, ok=True, **body):
        def call(url, code, label):
            self.registers.append((url, code, label))
            if ok:
                payload = {"ok": True, "device_id": "d-abc123", "token": "t" * 43}
                payload.update(body)
                return hub_client.Response(200, payload)
            return hub_client.Response(401, {"ok": False, "error": "bad_invite"}, "bad_invite")
        return call

    def _setup(self, opts=None, stdin_text="", **kw) -> A.Market:
        return A.setup_market(opts or A.RunOptions(), self.said.append,
                              io.StringIO(stdin_text), config_path=self.config,
                              spool_dir=self.spool, names=MO.ItemNames(None), **kw)

    def _out(self) -> str:
        return "\n".join(self.said)


class NoUploadTest(_Base):
    def test_without_a_hub_url_it_only_observes(self) -> None:
        market = self._setup(A.RunOptions(hub_url=""))
        self.assertIsNone(market.uploader)
        self.assertIsNotNone(market.cb, "업로드가 없어도 콘솔에는 페이지가 보여야 한다")
        self.assertIn("[허브] 주소가 없습니다", self._out())
        self.assertEqual(self.registers, [])

    def test_no_upload_flag_skips_registration_entirely(self) -> None:
        market = self._setup(A.RunOptions(hub_url="https://hub", upload=False,
                                          invite_code="code"))
        self.assertIsNone(market.uploader)
        self.assertEqual(self.registers, [])
        self.assertIn("--no-upload", self._out())

    def test_local_id_is_created_even_without_a_hub(self) -> None:
        self._setup(A.RunOptions())
        self.assertRegex(app_config.load(self.config).local_id, r"^[0-9a-f]{8}$")

    def test_missing_item_table_says_so_and_keeps_going(self) -> None:
        self._setup(A.RunOptions())
        self.assertIn("[아이템표] 없음", self._out())


class RegistrationTest(_Base):
    def test_first_run_registers_with_the_invite_code_and_saves_the_token(self) -> None:
        opts = A.RunOptions(hub_url="https://hub/", invite_code=" code-1 ", device_label="DEV-1")
        market = self._setup(opts, register=self._register())
        self.assertEqual(self.registers, [("https://hub", "code-1", "DEV-1")])
        cfg = app_config.load(self.config)
        self.assertEqual((cfg.hub_device_id, cfg.hub_token, cfg.hub_url),
                         ("d-abc123", "t" * 43, "https://hub"))
        self.assertNotIn("code-1", self.config.read_text(encoding="utf-8"),
                         "초대 코드는 저장하지 않는다(1회용)")
        self.assertIsNotNone(market.uploader)
        self.assertIn("[허브] 등록 완료 — 기기 d-abc123", self._out())
        self.assertIn("[허브] https://hub 기기 d-abc123", self._out())

    def test_existing_token_does_not_register_again(self) -> None:
        app_config.save(app_config.Config(hub_url="https://hub", hub_device_id="d-old",
                                          hub_token="tok", local_id="abcd1234"), self.config)
        market = self._setup(A.RunOptions())
        self.assertEqual(self.registers, [])
        self.assertIsNotNone(market.uploader)
        self.assertEqual(market.uploader.token, "tok")
        self.assertEqual(market.uploader.device_id(), "d-old")

    def test_prompt_is_used_when_no_invite_code_flag(self) -> None:
        market = self._setup(A.RunOptions(hub_url="https://hub"), stdin_text="typed-code\n",
                             register=self._register())
        self.assertEqual(self.registers[0][1], "typed-code")
        self.assertIsNotNone(market.uploader)

    def test_empty_prompt_skips_upload_but_keeps_observing(self) -> None:
        market = self._setup(A.RunOptions(hub_url="https://hub"), stdin_text="\n",
                             register=self._register())
        self.assertEqual(self.registers, [])
        self.assertIsNone(market.uploader)
        self.assertIsNotNone(market.cb)
        self.assertIn("초대 코드가 없어", self._out())

    def test_failed_registration_explains_and_keeps_observing(self) -> None:
        market = self._setup(A.RunOptions(hub_url="https://hub", invite_code="wrong"),
                             register=self._register(ok=False))
        self.assertIsNone(market.uploader)
        self.assertIn("초대 코드가 맞지 않습니다", self._out())
        self.assertEqual(app_config.load(self.config).hub_token, "", "실패는 저장하지 않는다")

    def test_response_without_token_is_treated_as_a_failure(self) -> None:
        def call(url, code, label):
            return hub_client.Response(200, {"ok": True, "device_id": "d-1"})
        market = self._setup(A.RunOptions(hub_url="https://hub", invite_code="c"), register=call)
        self.assertIsNone(market.uploader)
        self.assertEqual(app_config.load(self.config).hub_token, "")

    def test_default_label_is_the_hostname_and_is_printable_and_short(self) -> None:
        label = hub_setup.device_label()
        self.assertTrue(label.isprintable() and len(label) <= 64)
        self.assertEqual(hub_setup.device_label("  이 PC  "), "이 PC")
        self.assertEqual(hub_setup.device_label("a" * 100), "a" * 64)


class WiringTest(_Base):
    def test_callback_feeds_the_uploader_queue(self) -> None:
        market = self._setup(A.RunOptions(hub_url="https://hub", invite_code="c"),
                             register=self._register())
        market.observer._enqueue({"obs_id": "x"})
        self.assertEqual(market.uploader._q.qsize(), 1)

    def test_started_uploader_thread_posts_enqueued_observations(self) -> None:
        """배선이 만든 진짜 Uploader 를 띄우면 큐 → 스풀 → POST 까지 스스로 간다(run() 이 start 를 부른다는 전제)."""
        import threading
        from yuktracker import hub_client
        posted = []
        done = threading.Event()

        def post(hub_url, token, device_id, observations):
            posted.append((hub_url, token, device_id, observations))
            done.set()
            return hub_client.Response(200, {"ok": True, "accepted": len(observations)})

        market = self._setup(A.RunOptions(hub_url="https://hub", invite_code="c"),
                             register=self._register(), post=post)
        market.uploader._batch_wait = 0.05
        market.uploader.start()
        try:
            market.observer._enqueue({"obs_id": "x"})
            self.assertTrue(done.wait(5.0), "업로더 스레드가 POST 하지 않았다")
        finally:
            market.uploader.stop()
            market.uploader.join(timeout=5.0)
        self.assertEqual(posted[0][0], "https://hub")
        self.assertEqual([o["obs_id"] for o in posted[0][3]], ["x"])
        self.assertEqual(market.uploader.uploaded, 1)

    def test_pending_spool_from_a_previous_run_is_announced(self) -> None:
        from yuktracker import spool as S
        S.Spool(self.spool).write([{"obs_id": "old"}])
        self._setup(A.RunOptions(hub_url="https://hub", invite_code="c"),
                    register=self._register())
        self.assertIn("지난 실행의 스풀 1건부터 올립니다", self._out())


if __name__ == "__main__":
    unittest.main()
