"""`paths` — 경로 계산은 순수(폴더를 만들지 않는다), `%APPDATA%` 폴백은 심 `config.appdata_base` 와 한 규칙."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuktracker import paths
from yuktracker.seassist import config


class PathsTest(unittest.TestCase):
    def test_app_dir_is_pure_and_shares_appdata_rule(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"APPDATA": str(Path(d) / "roaming")}):
                p = paths.app_dir()
                self.assertEqual(p, Path(d) / "roaming" / "YukTracker")
                self.assertEqual(paths.item_table_cache_path(), p / "item_names.json")
                self.assertFalse(p.parent.exists(), "경로 계산이 폴더를 만들면 안 된다")
                self.assertEqual(config.appdata_base(), Path(d) / "roaming")

    def test_falls_back_to_home_without_appdata(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "APPDATA"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(paths.app_dir(), Path.home() / "YukTracker")
            self.assertEqual(config.appdata_base(), Path.home())


if __name__ == "__main__":
    unittest.main()
