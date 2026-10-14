"""명령줄 — 인자 파싱 · 관리자 승격 · `agent.run` 호출."""
from __future__ import annotations

import argparse
import sys

from . import __version__, selftest
from .agent import DEFAULT_CAPTURE_MIN, FLOW_WAIT_SEC, RunOptions, run

#: 소스 실행 모드에서 UAC 재기동에 쓰는 모듈(`python -m yuktracker`). exe 는 UAC 매니페스트로 뜬다.
MODULE = "yuktracker"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="yuktracker",
        description="육의전 관측기 — 거상 클라이언트의 s2c 패킷을 관측한다(관리자 권한·Npcap 필요)")
    ap.add_argument("--capture", nargs="?", const=DEFAULT_CAPTURE_MIN, type=float, default=None,
                    metavar="MIN",
                    help=f"발굴 수집 창(원시 패킷 창 파일)도 연다(분, 값 없이 주면 "
                         f"{DEFAULT_CAPTURE_MIN:g}). 없어도 육의전 관측·업로드는 돈다")
    ap.add_argument("--wait", type=float, default=None, metavar="SEC",
                    help=f"게임 흐름 대기 상한(초, 기본 {FLOW_WAIT_SEC:g} · --selftest 는 {selftest.WAIT_SEC:g})")
    ap.add_argument("--selftest", action="store_true",
                    help="관측하지 않고 자가진단만 한다(권한·Npcap·거상·아이템 표·허브·토큰·스풀)")
    ap.add_argument("--no-elevate", action="store_true",
                    help="관리자 권한 재기동을 시도하지 않는다(개발·테스트)")
    ap.add_argument("--no-pause", action="store_true",
                    help="끝난 뒤 Enter 를 기다리지 않는다(파이프·스크립트)")
    ap.add_argument("--hub-url", default="", metavar="URL",
                    help="허브 주소(기본: 설정 파일 → 환경변수 YUKTRACKER_HUB_URL → 빌드 시 주입값)")
    ap.add_argument("--invite-code", default="", metavar="CODE",
                    help="첫 실행 등록용 초대 코드(없으면 콘솔에서 묻는다, 저장하지 않는다)")
    ap.add_argument("--device-label", default="", metavar="NAME",
                    help="허브 목록에 보일 이 PC 의 이름(기본: 호스트명)")
    ap.add_argument("--client-dir", default="", metavar="DIR",
                    help="아이템 id→이름 표를 찾을 거상 클라 폴더(없으면 실행 중 gersang.exe → 기본 설치 경로)")
    ap.add_argument("--no-upload", action="store_true",
                    help="허브 업로드 없이 관측만(스풀·업로더를 띄우지 않는다)")
    ap.add_argument("--satong", type=_server_id, default=None, metavar="SERVER_ID",
                    help="사통팔달(서버 전체 외치기)도 수집한다 — 이 클라가 접속한 서버 번호(운영자 PC 전용, 기본 꺼짐; "
                         "설정 satong_server_id 보다 우선). 허브가 이 기기를 사통 수집 기기로 허용해야 올라간다")
    ap.add_argument("--version", action="version", version=f"yuktracker {__version__}")
    return ap


def _server_id(text: str) -> int:
    """`--satong` 값 — 양의 정수만(패킷에 서버 식별자가 없어 사람이 정한다; 틀리면 다른 서버 방에 올라간다)."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("서버 번호는 양의 정수입니다(예: 7)") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("서버 번호는 양의 정수입니다(예: 7)")
    return value


def _utf8_console() -> None:
    # Windows 콘솔 기본 cp949 는 —/⚠ 를 못 찍고 죽는다(SEAssist 스크립트 관례).
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _disable_quick_edit() -> None:
    """conhost 의 QuickEdit(마우스 드래그 선택)을 끈다 — 선택 중엔 콘솔 쓰기가 통째로 멈춰 표시가 밀린다(쓰기 스레드가
    있어도 화면은 멈춘다). 관측 모드만: `--selftest` 는 화면을 복사해 보내야 하니 그대로 둔다. Windows Terminal 은 선택을
    스스로 처리해 이 플래그와 무관하다. 콘솔이 아니면(파이프·서비스) 조용히 넘어간다."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-10)               # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        k32.SetConsoleMode(handle, (mode.value | 0x0080) & ~0x0040)   # ENABLE_EXTENDED_FLAGS | ~ENABLE_QUICK_EDIT_MODE
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.selftest and (args.invite_code or args.device_label or args.capture is not None):
        # 자가진단은 등록·수집을 하지 않는다 — 조용히 무시하면 "--invite-code 로 등록하라"는 안내를 따라 온
        # 사람이 같은 경고를 또 본다.
        ap.error("--selftest 는 등록·수집을 하지 않습니다 — 등록은 --selftest 없이 --invite-code 로, "
                 "--capture 는 관측 실행에서 주세요")
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
    if args.selftest:
        # 승격 경로를 그대로 지난 뒤다 — Npcap 프로브·캡처 확인에 관리자 권한이 필요하다.
        return selftest.main(hub_url=args.hub_url, client_dir=args.client_dir,
                             upload=not args.no_upload, pause=not args.no_pause,
                             satong_server_id=args.satong,
                             wait_sec=selftest.WAIT_SEC if args.wait is None else args.wait)
    opts = RunOptions(capture_min=args.capture,
                      flow_wait_sec=FLOW_WAIT_SEC if args.wait is None else args.wait,
                      pause_on_exit=not args.no_pause, hub_url=args.hub_url,
                      invite_code=args.invite_code, device_label=args.device_label,
                      client_dir=args.client_dir, upload=not args.no_upload,
                      satong_server_id=args.satong)
    _disable_quick_edit()
    return run(opts)
