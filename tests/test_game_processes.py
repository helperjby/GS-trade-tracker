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

    @unittest.skipUnless(sys.platform == "win32", "ctypes.set_last_error 는 Windows 전용")
    def test_retries_with_large_buffer_on_insufficient_buffer(self) -> None:
        import ctypes
        from types import SimpleNamespace
        from unittest import mock
        long_path = "C:\\" + "x" * 2000 + "\\Gersang.exe"
        capacities: list[int] = []

        def query(handle, flags, buf, psize):
            cap = psize._obj.value
            capacities.append(cap)
            if cap < len(long_path) + 1:
                ctypes.set_last_error(gp._ERROR_INSUFFICIENT_BUFFER)
                return 0
            buf.value = long_path
            psize._obj.value = len(long_path)
            return 1

        fake = SimpleNamespace(OpenProcess=lambda *a: 1234, QueryFullProcessImageNameW=query, CloseHandle=lambda h: 1)
        with mock.patch.object(gp.ctypes, "WinDLL", return_value=fake):
            self.assertEqual(gp.process_image_path(42), long_path)
        self.assertEqual(capacities, [gp._IMAGE_PATH_BUF, gp._IMAGE_PATH_BUF_MAX])

    @unittest.skipUnless(sys.platform == "win32", "ctypes.set_last_error 는 Windows 전용")
    def test_other_query_failure_is_none_without_retry(self) -> None:
        import ctypes
        from types import SimpleNamespace
        from unittest import mock
        calls: list[int] = []

        def query(handle, flags, buf, psize):
            calls.append(psize._obj.value)
            ctypes.set_last_error(5)   # ERROR_ACCESS_DENIED
            return 0

        fake = SimpleNamespace(OpenProcess=lambda *a: 1234, QueryFullProcessImageNameW=query, CloseHandle=lambda h: 1)
        with mock.patch.object(gp.ctypes, "WinDLL", return_value=fake):
            self.assertIsNone(gp.process_image_path(42))
        self.assertEqual(calls, [gp._IMAGE_PATH_BUF])


if __name__ == "__main__":
    unittest.main()
