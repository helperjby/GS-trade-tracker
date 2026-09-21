"""명령줄 — 인자 파싱 · 관리자 승격 · `agent.run` 호출."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .agent import DEFAULT_CAPTURE_MIN, FLOW_WAIT_SEC, RunOptions, run

#: 소스 실행 모드에서 UAC 재기동에 쓰는 모듈(`python -m yuktracker`). exe 는 UAC 매니페스트로 뜬다.
MODULE = "yuktracker"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="yuktracker",
        description="육의전 관측기 — 거상 클라이언트의 s2c 패킷을 관측한다(관리자 권한·Npcap 필요)")
    ap.add_argument("--capture", nargs="?", const=DEFAULT_CAPTURE_MIN, type=float, default=None,
                    metavar="MIN",
                    help=f"발굴 수집 창을 연다(분, 값 없이 주면 {DEFAULT_CAPTURE_MIN:g}). "
                         "없으면 관측 대기 모드")
    ap.add_argument("--wait", type=float, default=FLOW_WAIT_SEC, metavar="SEC",
                    help=f"게임 흐름 대기 상한(초, 기본 {FLOW_WAIT_SEC:g})")
    ap.add_argument("--no-elevate", action="store_true",
                    help="관리자 권한 재기동을 시도하지 않는다(개발·테스트)")
    ap.add_argument("--no-pause", action="store_true",
                    help="끝난 뒤 Enter 를 기다리지 않는다(파이프·스크립트)")
    ap.add_argument("--version", action="version", version=f"yuktracker {__version__}")
    return ap


def _utf8_console() -> None:
    # Windows 콘솔 기본 cp949 는 —/⚠ 를 못 찍고 죽는다(SEAssist 스크립트 관례).
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    if sys.platform != "win32":
        print("이 프로그램은 Windows 전용입니다(Npcap).", file=sys.stderr)
        return 1
    if not args.no_elevate:
        from .seassist.admin import is_admin, relaunch_as_admin
        if not is_admin():
            if getattr(sys, "frozen", False):
                print("관리자 권한이 필요합니다 — exe 를 '관리자 권한으로 실행' 하세요.")
                return 2
            print("관리자 권한이 필요합니다(Npcap 캡처) — UAC 창에서 승인하면 새 콘솔로 다시 뜹니다.")
            if relaunch_as_admin(module=MODULE):
                return 0
            print("권한 상승이 거부되었거나 실패했습니다. 관리자 명령 프롬프트에서 다시 실행하세요.")
            return 2
    opts = RunOptions(capture_min=args.capture, flow_wait_sec=args.wait,
                      pause_on_exit=not args.no_pause)
    return run(opts)
