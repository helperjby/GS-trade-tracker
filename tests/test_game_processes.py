from __future__ import annotations

import sys
import unittest

from yuktracker import game_processes as gp


class GamePidsTest(unittest.TestCase):
    def test_filters_by_exe_name_case_insensitive(self) -> None:
        snap = [(4, "System"), (100, "Gersang.exe"), (101, "gersang-traders.exe"),
                (102, "GERSANG.EXE"), (103, r"C:\Games\gersang.exe"), (0, "gersang.exe")]
        self.assertEqual(gp.game_pids(lambda: snap), [100, 102, 103])

    def test_dedup_and_sorted(self) -> None:
        snap = [(300, "gersang.exe"), (200, "gersang.exe"), (300, "gersang.exe")]
        self.assertEqual(gp.game_pids(lambda: snap), [200, 300])

    def test_enumeration_failure_is_empty(self) -> None:
        def boom():
            raise OSError("snapshot")
        self.assertEqual(gp.game_pids(boom), [])

    def test_mirror_of_seassist_constant(self) -> None:
        # SEAssist window_manager.GAME_PROC_NAMES 미러 — 바뀌면 여기와 같이 바꾼다.
        self.assertEqual(gp.GAME_PROC_NAMES, frozenset({"gersang.exe"}))

    @unittest.skipUnless(sys.platform == "win32", "Toolhelp32 는 Windows 전용")
    def test_real_snapshot_contains_self(self) -> None:
        import os
        pids = {pid for pid, _exe in gp.snapshot_processes()}
        self.assertIn(os.getpid(), pids)


class ProcessImagePathTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "QueryFullProcessImageNameW 는 Windows 전용")
    def test_self_path_on_windows(self) -> None:
        import os
        from pathlib import Path
        p = gp.process_image_path(os.getpid())
        self.assertIsNotNone(p)
        self.assertEqual(Path(p).name.lower(), Path(sys.executable).name.lower())
        self.assertTrue(Path(p).is_file())

    def test_invalid_pid_is_none(self) -> None:
        self.assertIsNone(gp.process_image_path(0))
        self.assertIsNone(gp.process_image_path(-1))

    @unittest.skipUnless(sys.platform == "win32", "Windows 전용")
    def test_missing_process_is_none(self) -> None:
        # 존재할 가능성이 사실상 없는 큰 PID — OpenProcess 실패 → None(예외 없음).
        self.assertIsNone(gp.process_image_path(0x7FFFFFF0))

    def test_off_windows_is_none(self) -> None:
        from unittest import mock
        with mock.patch.object(gp.sys, "platform", "linux"):
            self.assertIsNone(gp.process_image_path(1234))


if __name__ == "__main__":
    unittest.main()
