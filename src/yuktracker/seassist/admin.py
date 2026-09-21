from __future__ import annotations

import ctypes
import os
import sys


def is_admin() -> bool:
    """현재 프로세스가 관리자 권한으로 실행 중인지 검사."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _quote(arg: str) -> str:
    if not arg:
        return '""'
    if any(c in arg for c in ' \t"'):
        return '"' + arg.replace('"', r"\"") + '"'
    return arg


#: 소스 실행 모드의 기본 재기동 모듈 — GUI. 육의전 관측기(`src.market_agent`)처럼 다른
#: 엔트리가 승격을 요청할 때는 `module=` 로 자기 모듈을 넘긴다(2026-09-21).
DEFAULT_MODULE = "src.main"


def _build_relaunch_args(module: str = DEFAULT_MODULE) -> tuple[str, str, str]:
    """Return (executable, parameters, working_directory) for ShellExecuteW."""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = " ".join(_quote(a) for a in sys.argv[1:])
        cwd = os.path.dirname(exe) or os.getcwd()
        return exe, params, cwd
    # 소스 실행 모드: 동일한 파이썬 인터프리터 + -m <module>
    exe = sys.executable
    parts: list[str] = []
    # 바이트코드 캐시 위치를 승격 프로세스에도 전달 — run.py/run_admin.bat 이 실행 폴더 밖
    # (%LOCALAPPDATA%\SEAssist\pycache)으로 돌려 둔 것을 재실행이 되돌리지 않도록.
    if sys.pycache_prefix:
        parts += ["-X", _quote(f"pycache_prefix={sys.pycache_prefix}")]
    parts += ["-m", module or DEFAULT_MODULE]
    parts += [_quote(a) for a in sys.argv[1:]]
    params = " ".join(parts)
    cwd = os.getcwd()
    return exe, params, cwd


def relaunch_as_admin(module: str = DEFAULT_MODULE) -> bool:
    """UAC 다이얼로그를 띄워 관리자 권한 프로세스로 다시 시작.

    True 반환: 상승된 프로세스가 정상 시작됨 — 호출자는 즉시 종료해야 한다.
    False 반환: UAC 거부 또는 실패 — 현재 프로세스를 유지하면 됨.
    ``module`` 은 소스 실행 모드에서 `-m` 으로 다시 띄울 모듈(frozen 이면 무시).
    """
    exe, params, cwd = _build_relaunch_args(module)
    try:
        SW_SHOWNORMAL = 1
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, cwd, SW_SHOWNORMAL
        )
        # ShellExecuteW: 32 초과면 성공
        return int(rc) > 32
    except Exception:
        return False
