"""허브 클라이언트 계약 — 요청 모양·응답 해석·행동 매핑 (PR-Y1b).

진짜 HTTP 를 쓴다(스레드 `http.server`) — `urllib` 이 `Content-Length` 를 붙이는지, 4xx 본문을
읽는지, 평문 413 을 견디는지는 스텁으로는 확인되지 않는다. 계약 정본은 `docs/HUB-PROTOCOL.md`.
"""
from __future__ import annotations

import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from yuktracker import hub_client


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    script: list = []
    seen: list = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 규약
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = None
        _Handler.seen.append({"path": self.path, "method": "POST",
                              "auth": self.headers.get("Authorization"),
                              "ctype": self.headers.get("Content-Type"),
                              "len": self.headers.get("Content-Length"), "body": body})
        self._reply()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 규약
        _Handler.seen.append({"path": self.path, "method": "GET",
                              "auth": self.headers.get("Authorization"),
                              "ctype": self.headers.get("Content-Type"),
                              "len": self.headers.get("Content-Length"), "body": None})
        self._reply()

    def _reply(self) -> None:
        status, headers, payload = (_Handler.script.pop(0) if _Handler.script
                                    else (200, {}, b'{"ok": true}'))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args) -> None:  # 테스트 출력을 더럽히지 않는다
        pass


def _json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class _ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.script, _Handler.seen = [], []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)


class RegisterTest(_ServerTest):
    def test_success_carries_device_id_and_token(self) -> None:
        _Handler.script.append((200, {"Content-Type": "application/json"},
                                _json({"ok": True, "v": 1, "device_id": "d-3f9a1c7e2b",
                                       "token": "t" * 43, "server_time": 1.0})))
        resp = hub_client.register(self.url + "/", "invite-code", "DEV-1")
        self.assertTrue(resp.ok)
        self.assertEqual(resp.body["device_id"], "d-3f9a1c7e2b")
        sent = _Handler.seen[0]
        self.assertEqual(sent["path"], "/api/market/register")
        self.assertEqual(sent["body"], {"v": 1, "invite_code": "invite-code", "label": "DEV-1"})
        self.assertIsNone(sent["auth"], "등록은 무인증 — 시크릿을 보내지 않는다")
        self.assertEqual(sent["ctype"], "application/json")
        self.assertTrue(sent["len"], "Content-Length 가 없으면 허브가 411 을 낸다")

    def test_bad_invite_and_full_and_closed_are_explained(self) -> None:
        cases = [(401, {"ok": False, "error": "bad_invite"}, "초대 코드"),
                 (403, {"ok": False, "error": "registration_full"}, "정원"),
                 (403, {"ok": False, "error": "registration_closed"}, "등록을 받지 않습니다"),
                 (400, {"ok": False, "error": "bad_request", "field": "label"}, "label")]
        for status, body, needle in cases:
            with self.subTest(needle=needle):
                _Handler.script.append((status, {}, _json(body)))
                resp = hub_client.register(self.url, "x", "DEV-1")
                self.assertFalse(resp.ok)
                self.assertEqual(resp.status, status)
                self.assertIn(needle, hub_client.describe_register(resp))

    def test_rate_limit_reports_the_wait(self) -> None:
        _Handler.script.append((429, {"Retry-After": "120"},
                                _json({"ok": False, "error": "rate_limited", "retry_after": 120})))
        resp = hub_client.register(self.url, "x")
        self.assertEqual((resp.status, resp.retry_after), (429, 120.0))
        self.assertIn("120초", hub_client.describe_register(resp))

    def test_label_is_omitted_when_empty(self) -> None:
        hub_client.register(self.url, "x", "")
        self.assertNotIn("label", _Handler.seen[0]["body"])


class UploadTest(_ServerTest):
    def _obs(self, obs_id="a"):
        return {"obs_id": obs_id, "agent_ts": 1.0, "opcode": 0x321F, "rows": []}

    def test_request_shape_and_bearer_token(self) -> None:
        _Handler.script.append((200, {}, _json({"ok": True, "accepted": 1, "duplicates": 0,
                                                "rows": 3, "server_time": 2.0})))
        resp = hub_client.upload(self.url, "tok-1", "d-1", [self._obs()])
        self.assertEqual(hub_client.classify_upload(resp)[0], "ok")
        sent = _Handler.seen[0]
        self.assertEqual(sent["path"], "/api/market/observations")
        self.assertEqual(sent["auth"], "Bearer tok-1")
        self.assertEqual(sent["body"]["device_id"], "d-1")
        self.assertEqual([o["obs_id"] for o in sent["body"]["observations"]], ["a"])

    def test_plain_text_413_does_not_crash_the_parser(self) -> None:
        _Handler.script.append((413, {"Content-Type": "text/plain"}, b"Request Entity Too Large"))
        resp = hub_client.upload(self.url, "t", "d-1", [self._obs()])
        self.assertEqual((resp.status, resp.body), (413, {}))
        self.assertIn("Too Large", resp.text)
        self.assertEqual(hub_client.classify_upload(resp)[0], "split")

    def test_unauthorized_body_without_ok_key(self) -> None:
        _Handler.script.append((401, {}, _json({"error": "unauthorized"})))
        resp = hub_client.upload(self.url, "t", "d-1", [self._obs()])
        self.assertFalse(resp.ok)
        self.assertEqual(hub_client.classify_upload(resp)[0], "stop")

    def test_device_mismatch_tells_the_expected_id(self) -> None:
        _Handler.script.append((403, {}, _json({"ok": False, "error": "device_mismatch",
                                                "device_id": "d-real"})))
        action, why = hub_client.classify_upload(
            hub_client.upload(self.url, "t", "d-wrong", [self._obs()]))
        self.assertEqual(action, "stop")
        self.assertIn("d-real", why)

    def test_unreachable_host_is_a_network_failure_not_an_exception(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # 아무도 듣지 않는 포트
        resp = hub_client.upload(f"http://127.0.0.1:{port}", "t", "d-1", [self._obs()])
        self.assertEqual((resp.status, resp.error), (0, "network"))
        self.assertEqual(hub_client.classify_upload(resp)[0], "retry")


class PingTest(_ServerTest):
    """`GET /` 와 `GET /api/market/ping` — 자가진단이 허브 도달과 토큰 유효를 나눠 보는 근거(HUB-PROTOCOL §3-7)."""

    def test_ping_request_shape_and_success_line(self) -> None:
        _Handler.script.append((200, {}, _json({"ok": True, "v": 1, "device_id": "d-1",
                                                "label": "친구1", "server_time": 2.0})))
        resp = hub_client.ping(self.url + "/", "tok-1")
        sent = _Handler.seen[0]
        self.assertEqual((sent["path"], sent["method"]), ("/api/market/ping", "GET"))
        self.assertEqual(sent["auth"], "Bearer tok-1")
        self.assertIsNone(sent["ctype"])            # 본문 없는 요청 — Content-Type 을 붙이지 않는다
        self.assertTrue(resp.ok)
        line = hub_client.describe_ping(resp)
        self.assertIn("d-1", line)
        self.assertIn("친구1", line)

    def test_status_reports_reachability_only(self) -> None:
        _Handler.script.append((200, {}, _json({"service": "yuktracker-hub", "v": 1,
                                                "server_time": 2.0})))
        resp = hub_client.status(self.url)
        self.assertEqual(_Handler.seen[0]["path"], "/")
        self.assertTrue(resp.ok)
        self.assertIn("응답", hub_client.describe_status(resp))

    def test_old_hub_without_the_route_is_named_as_such(self) -> None:
        """허브가 아직 이 판이 아니면 404(직접 접속) 또는 403 not_public(공개 리스너) 이다 — 둘 다 같은 안내."""
        for status, payload in ((404, b"Not Found"),
                                (403, _json({"ok": False, "error": "not_public"}))):
            with self.subTest(status=status):
                _Handler.script.append((status, {}, payload))
                line = hub_client.describe_ping(hub_client.ping(self.url, "t"))
                self.assertIn("옛 판", line)

    def test_rejected_tokens_are_explained(self) -> None:
        cases = [
            ((401, _json({"error": "unauthorized"})), "--invite-code"),
            ((403, _json({"ok": False, "error": "device_revoked"})), "제거"),
            ((429, _json({"ok": False, "error": "rate_limited", "retry_after": 7})), "7초"),
            ((500, _json({"ok": False, "error": "storage_error"})), "500"),
        ]
        for (status, payload), expect in cases:
            with self.subTest(status=status):
                _Handler.script.append((status, {}, payload))
                resp = hub_client.ping(self.url, "t")
                self.assertFalse(resp.ok)
                self.assertIn(expect, hub_client.describe_ping(resp))

    def test_unreachable_hub_is_a_network_failure(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        resp = hub_client.status(f"http://127.0.0.1:{port}")
        self.assertEqual((resp.status, resp.error), (0, "network"))
        self.assertIn("닿지 못했습니다", hub_client.describe_status(resp))


class ClassifyTest(unittest.TestCase):
    """행동 매핑은 정책 그 자체다 — 표로 못 박는다(HUB-PROTOCOL §3-1)."""

    def test_table(self) -> None:
        cases = [
            (hub_client.Response(200, {"ok": True, "accepted": 2}), "ok"),
            (hub_client.Response(400, {"ok": False, "error": "bad_request"}, "bad_request"), "quarantine"),
            (hub_client.Response(400, {"ok": False, "error": "agent_ts_out_of_range"},
                                 "agent_ts_out_of_range"), "quarantine"),
            (hub_client.Response(401, {"error": "unauthorized"}), "stop"),
            (hub_client.Response(403, {"ok": False, "error": "device_revoked"}, "device_revoked"), "stop"),
            (hub_client.Response(429, {"ok": False}, "rate_limited", retry_after=5), "wait"),
            (hub_client.Response(413, text="too large"), "split"),
            (hub_client.Response(500, {"ok": False, "error": "storage_error"}, "storage_error"), "retry"),
            (hub_client.Response(0, error="network"), "retry"),
            (hub_client.Response(0, error="tls", tls=True), "stop"),
            (hub_client.Response(418, {}), "quarantine"),
        ]
        for resp, want in cases:
            with self.subTest(status=resp.status, error=resp.error):
                self.assertEqual(hub_client.classify_upload(resp)[0], want)

    def test_chunks_respects_the_batch_limit(self) -> None:
        items = list(range(250))
        self.assertEqual([len(c) for c in hub_client.chunks(items)], [100, 100, 50])
        self.assertEqual([len(c) for c in hub_client.chunks(items, 1000)], [100, 100, 50])
        self.assertEqual([len(c) for c in hub_client.chunks(items, 0)], [1] * 250)


if __name__ == "__main__":
    unittest.main()
