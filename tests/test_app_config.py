"""설정 계약 — 원자적 쓰기 · 손상 파일 내성 · local_id 1회 생성 · 허브 주소 우선순위 (PR-Y1b)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from yuktracker import app_config


class AppConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sub" / "config.json"

    def test_missing_file_is_an_empty_config(self) -> None:
        cfg = app_config.load(self.path)
        self.assertEqual((cfg.hub_url, cfg.hub_device_id, cfg.hub_token, cfg.local_id), ("", "", "", ""))

    def test_round_trip_keeps_unknown_keys(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"hub_url": " https://h/ ", "hub_token": "t",
                                         "미래키": {"a": 1}}), encoding="utf-8")
        cfg = app_config.load(self.path)
        self.assertEqual((cfg.hub_url, cfg.hub_token), ("https://h/", "t"))
        cfg.hub_device_id = "d-1"
        app_config.save(cfg, self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["미래키"], {"a": 1}, "모르는 키를 지우지 않는다(구버전 exe 가 새 키를 밟지 않게)")
        self.assertEqual(raw["hub_device_id"], "d-1")

    def test_corrupt_or_wrong_shape_file_does_not_raise(self) -> None:
        self.path.parent.mkdir(parents=True)
        for text in ("{깨진", "[1,2]", '"문자열"'):
            self.path.write_text(text, encoding="utf-8")
            cfg = app_config.load(self.path)
            self.assertEqual(cfg.hub_token, "", text)

    def test_save_is_atomic_and_leaves_no_temp(self) -> None:
        app_config.save(app_config.Config(hub_token="t"), self.path)
        self.assertEqual(app_config.load(self.path).hub_token, "t")
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["config.json"])

    def test_local_id_is_created_once_and_persists(self) -> None:
        cfg = app_config.load(self.path)
        first = app_config.ensure_local_id(cfg, self.path)
        self.assertRegex(first, r"^[0-9a-f]{8}$")
        again = app_config.ensure_local_id(app_config.load(self.path), self.path)
        self.assertEqual(again, first, "재시작해도 같아야 obs_id 가 흔들리지 않는다")

    def test_hub_url_priority(self) -> None:
        cfg = app_config.Config(hub_url="https://from-config")
        env = {app_config.ENV_HUB_URL: "https://from-env"}
        self.assertEqual(app_config.resolve_hub_url("https://from-cli/", cfg, env), "https://from-cli")
        self.assertEqual(app_config.resolve_hub_url("", cfg, env), "https://from-config")
        self.assertEqual(app_config.resolve_hub_url("", app_config.Config(), env), "https://from-env")
        self.assertEqual(app_config.resolve_hub_url("", app_config.Config(), {}),
                         app_config.BUILD_HUB_URL.rstrip("/"))


if __name__ == "__main__":
    unittest.main()
