"""스풀·업로더 계약 — 영속·순서·중복 안전 · 응답별 행동(격리·정지·대기·분할·백오프) (PR-Y1b).

허브 응답은 주입한다(`post`) — 네트워크 없이 정책만 본다. 진짜 HTTP 는 `test_hub_client.py`.
"""
from __future__ import annotations

import queue
import tempfile
import unittest
from pathlib import Path

from yuktracker import hub_client, spool


def _resp(status=200, **kw) -> hub_client.Response:
    """`hub_client._read` 가 만들어 주는 것과 같은 모양 — `error` 는 본문에서 뽑는다."""
    body = kw.pop("body", None)
    if body is None:
        body = {"ok": True, "accepted": 1, "duplicates": 0, "rows": 2} if status == 200 else {}
    kw.setdefault("error", str(body.get("error") or ""))
    return hub_client.Response(status=status, body=body, **kw)


def _env(obs_id: str = "id-1") -> dict:
    return {"obs_id": obs_id, "agent_ts": 1.0, "opcode": 0x321F, "page": None,
            "total_pages": 1, "hdr4": 0, "anomalies": [], "rows": []}


class SpoolFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spool = spool.Spool(Path(self.tmp.name) / "spool")

    def test_write_read_done_round_trip(self) -> None:
        path = self.spool.write([_env("a"), _env("b")])
        self.assertEqual([p.name for p in self.spool.pending()], [path.name])
        self.assertEqual([o["obs_id"] for o in self.spool.read(path)], ["a", "b"])
        self.spool.done(path)
        self.assertEqual(self.spool.pending(), [])

    def test_pending_is_oldest_first_and_survives_a_new_spool_object(self) -> None:
        clock = [1_700_000_000.0]
        s = spool.Spool(self.spool.dir, wall=lambda: clock[0])
        first = s.write([_env("a")])
        clock[0] += 1.0
        second = s.write([_env("b")])
        fresh = spool.Spool(self.spool.dir)  # 프로세스를 다시 띄운 셈
        self.assertEqual([p.name for p in fresh.pending()], [first.name, second.name])

    def test_broken_line_does_not_lose_the_batch(self) -> None:
        path = self.spool.write([_env("a"), _env("b")])
        path.write_text(path.read_text(encoding="utf-8") + "{잘린 줄\n", encoding="utf-8")
        self.assertEqual([o["obs_id"] for o in self.spool.read(path)], ["a", "b"])

    def test_quarantine_moves_the_file_and_records_why(self) -> None:
        path = self.spool.write([_env("a")])
        moved = self.spool.quarantine(path, "400 bad_request rows[0].price")
        self.assertFalse(path.exists())
        self.assertTrue(moved.exists())
        self.assertIn("rows[0].price",
                      (self.spool.quarantine_dir / (path.name + ".why")).read_text(encoding="utf-8"))
        self.assertEqual(self.spool.pending(), [], "격리본은 다시 올리지 않는다")

    def test_split_halves_the_batch_and_refuses_a_single(self) -> None:
        path = self.spool.write([_env("a"), _env("b"), _env("c")])
        parts = self.spool.split(path)
        self.assertEqual(len(parts), 2)
        self.assertEqual([len(self.spool.read(p)) for p in parts], [1, 2])
        self.assertFalse(path.exists())
        self.assertEqual(self.spool.split(self.spool.write([_env("only")])), [])

    def test_no_temp_file_is_left_behind(self) -> None:
        self.spool.write([_env("a")])
        self.assertEqual([p.suffix for p in self.spool.dir.iterdir()], [".jsonl"])


class UploaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spool = spool.Spool(Path(self.tmp.name) / "spool")
        self.sent: list[tuple] = []
        self.said: list[str] = []
        self.slept: list[float] = []
        self.responses: list[hub_client.Response] = []

    def _post(self, url, token, device_id, observations):
        self.sent.append((url, token, device_id, [o["obs_id"] for o in observations]))
        return self.responses.pop(0) if self.responses else _resp()

    def _uploader(self, device_id="d-1", **kw) -> spool.Uploader:
        return spool.Uploader(self.spool, hub_url="https://hub", token="tok",
                              device_id=lambda: device_id, say=self.said.append,
                              post=self._post, sleep=self.slept.append, **kw)

    def test_device_id_is_filled_at_post_time(self) -> None:
        box = {"id": "d-old"}
        up = spool.Uploader(self.spool, hub_url="https://hub", token="tok",
                            device_id=lambda: box["id"], say=self.said.append,
                            post=self._post, sleep=self.slept.append)
        path = self.spool.write([_env("a")])
        box["id"] = "d-new"  # 재등록
        up.send_one(path)
        self.assertEqual(self.sent[0][2], "d-new", "스풀 파일이 아니라 POST 시점의 기기 id")

    def test_success_deletes_the_file_and_counts(self) -> None:
        up = self._uploader()
        path = self.spool.write([_env("a"), _env("b")])
        self.assertEqual(up.send_one(path), "ok")
        self.assertFalse(path.exists())
        self.assertEqual(up.uploaded, 2)

    def test_400_is_quarantined_not_retried(self) -> None:
        up = self._uploader()
        self.responses.append(_resp(400, body={"ok": False, "error": "bad_request",
                                               "field": "rows[0].price"}))
        path = self.spool.write([_env("a")])
        self.assertEqual(up.send_one(path), "quarantine")
        self.assertEqual((up.quarantined, self.spool.pending()), (1, []))
        self.assertIn("rows[0].price", self.said[0])

    def test_401_and_403_stop_the_uploader_and_keep_the_spool(self) -> None:
        for resp, needle in ((_resp(401, body={"error": "unauthorized"}), "401"),
                             (_resp(403, body={"ok": False, "error": "device_revoked"}), "제거")):
            with self.subTest(needle=needle):
                self.said.clear()
                up = self._uploader()
                self.responses.append(resp)
                path = self.spool.write([_env("a")])
                self.assertEqual(up.send_one(path), "stop")
                self.assertTrue(up.halted)
                self.assertTrue(path.exists(), "스풀은 남는다 — 재등록 뒤 올린다")
                self.assertIn(needle, self.said[0])
                self.spool.done(path)

    def test_flush_stops_at_the_first_halt_and_leaves_the_rest(self) -> None:
        up = self._uploader()
        self.responses.extend([_resp(403, body={"ok": False, "error": "device_revoked"})])
        first, second = self.spool.write([_env("a")]), self.spool.write([_env("b")])
        up.flush_pending()
        self.assertTrue(first.exists() and second.exists())
        self.assertEqual(len(self.sent), 1, "정지 뒤에는 더 보내지 않는다")

    def test_429_waits_the_retry_after_and_keeps_the_file(self) -> None:
        up = self._uploader()
        self.responses.append(_resp(429, body={"ok": False, "error": "rate_limited"},
                                    retry_after=42.0))
        path = self.spool.write([_env("a")])
        self.assertEqual(up.send_one(path), "wait")
        self.assertEqual(self.slept, [42.0])
        self.assertTrue(path.exists())
        self.assertFalse(up.halted)

    def test_413_splits_the_batch(self) -> None:
        up = self._uploader()
        self.responses.append(_resp(413, text="Request Entity Too Large"))
        path = self.spool.write([_env("a"), _env("b")])
        self.assertEqual(up.send_one(path), "split")
        self.assertEqual([len(self.spool.read(p)) for p in self.spool.pending()], [1, 1])

    def test_413_on_a_single_observation_is_quarantined(self) -> None:
        up = self._uploader()
        self.responses.append(_resp(413, text="too large"))
        self.assertEqual(up.send_one(self.spool.write([_env("a")])), "quarantine")
        self.assertEqual(up.quarantined, 1)

    def test_network_and_5xx_back_off_and_reset_on_success(self) -> None:
        up = self._uploader()
        self.responses.extend([hub_client.Response(status=0, error="network", text="끊김"),
                               _resp(500, body={"ok": False, "error": "storage_error"}),
                               _resp()])
        path = self.spool.write([_env("a")])
        self.assertEqual([up.send_one(path), up.send_one(path)], ["retry", "retry"])
        self.assertEqual(self.slept, [spool.BACKOFF_MIN_SEC, spool.BACKOFF_MIN_SEC * 2])
        self.assertTrue(path.exists())
        self.assertEqual(up.send_one(path), "ok")
        self.responses.append(hub_client.Response(status=0, error="network"))
        up.send_one(self.spool.write([_env("b")]))
        self.assertEqual(self.slept[-1], spool.BACKOFF_MIN_SEC, "성공하면 백오프가 처음으로 돌아간다")

    def test_tls_failure_stops_instead_of_retrying_forever(self) -> None:
        up = self._uploader()
        self.responses.append(hub_client.Response(status=0, error="tls", tls=True, text="expired"))
        self.assertEqual(up.send_one(self.spool.write([_env("a")])), "stop")
        self.assertIn("인증서", up.stopped_reason)

    def test_duplicate_obs_id_is_a_normal_200(self) -> None:
        up = self._uploader()
        self.responses.append(_resp(body={"ok": True, "accepted": 0, "duplicates": 1, "rows": 0}))
        self.assertEqual(up.send_one(self.spool.write([_env("a")])), "ok")
        self.assertEqual(up.uploaded, 1)

    def test_drain_queue_batches_up_to_the_hub_limit(self) -> None:
        up = self._uploader(batch_wait=0.01)
        for i in range(hub_client.MAX_OBSERVATIONS + 5):
            up.enqueue(_env(f"id-{i}"))
        path = up.drain_queue()
        self.assertEqual(len(self.spool.read(path)), hub_client.MAX_OBSERVATIONS)
        self.assertEqual(len(self.spool.read(up.drain_queue())), 5)
        self.assertIsNone(up.drain_queue(), "빈 큐는 파일을 만들지 않는다")

    def test_enqueue_drops_when_full_instead_of_blocking_the_sniffer(self) -> None:
        up = self._uploader()
        up._q = queue.Queue(maxsize=2)
        for i in range(5):
            up.enqueue(_env(f"id-{i}"))
        self.assertEqual(up.queue_dropped, 3)

    def test_empty_file_is_dropped_without_a_request(self) -> None:
        up = self._uploader()
        path = self.spool.write([])
        self.assertEqual(up.send_one(path), "ok")
        self.assertEqual(self.sent, [])
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
