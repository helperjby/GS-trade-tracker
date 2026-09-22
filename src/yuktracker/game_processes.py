"""실행 중인 거상 클라이언트 PID 열거 — ctypes Toolhelp32 스냅샷 (pywin32 불필요).

SEAssist 는 창 목록(`window_manager.enum_visible_windows`, pywin32)에서 실행 파일명이
`GAME_PROC_NAMES` 인 것을 게임으로 본다. 관측기는 슬롯이 없으니 "지금 떠 있는 거상 프로세스
전부"만 알면 되고, 창이 최소화·숨김이어도 상관없으므로 프로세스 스냅샷이 더 단순하다.
실행 파일명 집합은 SEAssist `window_manager.GAME_PROC_NAMES` 와 같아야 한다 — 그쪽이 정본,
여기는 미러(한 줄이라 동기화 스크립트 대상으로 만들지 않았다).
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Callable, Iterable

#: SEAssist `src/core/window_manager.py` 의 GAME_PROC_NAMES 미러(소문자 basename).
GAME_PROC_NAMES: frozenset[str] = frozenset({"gersang.exe"})

_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
_MAX_PATH = 260
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_IMAGE_PATH_BUF = 1024
#: 긴 경로(LongPathsEnabled·깊은 사용자 폴더) 재시도 크기 — Win32 UNICODE_STRING 상한.
_IMAGE_PATH_BUF_MAX = 32767
_ERROR_INSUFFICIENT_BUFFER = 122


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * _MAX_PATH),
    ]


def snapshot_processes() -> list[tuple[int, str]]:
    """(pid, 실행 파일명) 전부 — 실패하면 빈 목록(예외 없음). Windows 전용."""
    if sys.platform != "win32":
        return []
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
    k32.Process32NextW.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.CloseHandle.restype = wintypes.BOOL

    handle = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not handle or handle == _INVALID_HANDLE_VALUE:
        return []
    out: list[tuple[int, str]] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = k32.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), str(entry.szExeFile)))
            ok = k32.Process32NextW(handle, ctypes.byref(entry))
    except Exception:
        pass
    finally:
        k32.CloseHandle(handle)
    return out


def process_image_path(pid: int) -> str | None:
    """실행 중 프로세스의 실행 파일 전체 경로 — `QueryFullProcessImageNameW`. 실패·비Windows 는 None.

    `Module32FirstW` 가 아니라 이것인 이유: 64bit 파이썬에서 32bit 게임 프로세스의 모듈 스냅샷은
    `ERROR_PARTIAL_COPY` 로 실패하지만, `PROCESS_QUERY_LIMITED_INFORMATION` 핸들의 이미지 이름 조회는
    비트 수·승격과 무관하게 된다(실측 2026-09-21: 비승격 64bit 파이썬 → 32bit Gersang.exe 3개, 안티치트
    가동 중). 클라 폴더(`gersang.gcs`)를 찾는 데 쓴다(`item_names.discover_client_dirs`).

    버퍼는 1024 wchar 로 시작해 `ERROR_INSUFFICIENT_BUFFER` 면 32767 로 한 번 더 — 긴 경로에 설치된 클라를
    조용히 놓치지 않는다. 그 외 실패는 None.
    """
    if sys.platform != "win32" or int(pid) <= 0:
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            for capacity in (_IMAGE_PATH_BUF, _IMAGE_PATH_BUF_MAX):
                buf = ctypes.create_unicode_buffer(capacity)
                size = wintypes.DWORD(capacity)
                if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    return buf.value or None
                if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
                    return None
            return None
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return None


def game_pids(snapshot: Callable[[], Iterable[tuple[int, str]]] = snapshot_processes) -> list[int]:
    """거상 클라이언트 PID — 오름차순, 중복 없음. 이름 비교는 소문자 basename."""
    pids: set[int] = set()
    try:
        for pid, exe in snapshot():
            name = str(exe or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
            if int(pid) > 0 and name in GAME_PROC_NAMES:
                pids.add(int(pid))
    except Exception:
        return []
    return sorted(pids)
