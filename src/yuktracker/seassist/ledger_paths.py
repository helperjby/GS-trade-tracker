"""원장 경로 해석 — 디바이스별 검토·패킷 폴더의 단일 결정 지점 (2026-09-21, 육의전 PR-Y1).

`wordinput_runner` 에 있던 경로 함수 여섯을 그대로 옮겨 왔다. 이유는 하나다 — 패킷 원장
(`packet_discovery_ledger` / `packet_shadow_ledger`)이 저장 위치를 알려고 `wordinput_runner`
를 import 하면 cv2·numpy·google-genai 가 통째로 딸려 온다. GUI 없이 도는 경량 육의전
관측기(`src/market_agent`)는 그 의존성 없이 **같은 폴더에 같은 형식으로** 창을 남겨야
하므로, 경로 계산만 stdlib 모듈로 분리했다. `wordinput_runner` 는 이 이름들을 그대로
재수출한다 — 기존 호출자(GUI 패널·frame_dump·스크립트)와 `wordinput_runner._debug_dir`
패치 지점은 무변경이다.

폴백 순서·디바이스 폴더 규칙은 옮기기 전과 한 바이트도 다르지 않다
(`tests/test_packet_data_dir.py` 가 핀). ``_debug_dir()`` 은 옮기지 않았다 — 조철·HP바·OCR
원장과 여러 테스트가 ``wordinput_runner._debug_dir`` 을 직접 패치하므로 거기 남긴다.
"""
from __future__ import annotations

import os
import re
import socket
import sys
from pathlib import Path

__all__ = [
    "packet_dir",
]

_DEBUG_DIR_NAME = "wordinput_review"
_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _project_root() -> Path:
    # PyInstaller frozen 빌드 시 exe 옆, 개발 시 repo 루트 (src/core/<this> 의 두 단계 상위).
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[2]


def _onedrive_root() -> Path | None:
    # Windows OneDrive 가 설치되면 env var 가 자동 등록됨.
    for key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        v = os.environ.get(key)
        if v:
            p = Path(v)
            if p.is_dir():
                return p
    return None


def _device_name(s) -> str:
    # Settings.display_name 우선, 빈 문자열이면 hostname. 윈도우 금지 문자는 _ 로 치환.
    name = ""
    if s is not None:
        name = (getattr(s, "display_name", "") or "").strip()
    if not name:
        name = socket.gethostname() or "unknown"
    name = _INVALID_NAME_CHARS.sub("_", name).strip(" .") or "unknown"
    return name


def _resolve_review_base(s) -> Path:
    # 1) settings.wordinput_review_dir 명시 → 그 경로 사용. `%OneDrive%` / `~` 같은
    #    환경변수·홈 디렉토리 토큰 확장 — 디바이스별 사용자명 차이 흡수.
    if s is not None:
        custom = (getattr(s, "wordinput_review_dir", "") or "").strip()
        if custom:
            try:
                p = Path(os.path.expandvars(custom)).expanduser()
                p.mkdir(parents=True, exist_ok=True)
                return p
            except OSError:
                pass  # 잘못된 경로 → fallback
    # 2) OneDrive default
    od = _onedrive_root()
    if od is not None:
        return od / "SEAssist" / _DEBUG_DIR_NAME
    # 3) repo/exe root
    return _project_root() / _DEBUG_DIR_NAME


def _resolve_packet_base(s) -> Path:
    """패킷 원장 베이스 — `_resolve_review_base` 의 짝 (패킷 전용 뿌리).

    `settings.packet_data_dir` 이 비면 **검토 폴더와 같은 뿌리**로 폴백한다 —
    설정하지 않은 기기는 종전과 한 바이트도 다르지 않게 동작한다(무회귀).
    갈리는 것은 패킷 도구가 읽는 원장뿐이다(packet_discovery / packet_shadow /
    packet_labels). jochul_events(운영 카운터)·hpbar_shadow·ocr_events 는
    `wordinput_runner._debug_dir()` 에 남는다 — 경계는 "패킷 도구가 읽는가" 하나다.
    """
    if s is not None:
        custom = (getattr(s, "packet_data_dir", "") or "").strip()
        if custom:
            try:
                p = Path(os.path.expandvars(custom)).expanduser()
                p.mkdir(parents=True, exist_ok=True)
                return p
            except OSError:
                pass  # 잘못된 경로 → 검토 폴더로 폴백
    return _resolve_review_base(s)


def packet_dir() -> Path:
    """디바이스별 패킷 원장 폴더 — `wordinput_runner._debug_dir()` 미러 (단일 결정 지점).

    세 원장(`packet_discovery_ledger` / `packet_shadow_ledger` /
    `warfield_packet_labeler`)과 채굴 스크립트, 육의전 관측기의 기본 루트가 전부 이 함수
    하나를 딛는다. 디바이스 폴더를 붙이는 모양까지 `_debug_dir()` 과 같아
    `<base>/<디바이스>/packet_discovery/` 가 된다.
    """
    from .config import load_settings  # 순환 import 방지용 지연 로드
    try:
        s = load_settings()
    except Exception:
        s = None
    base = _resolve_packet_base(s)
    p = base / _device_name(s)
    p.mkdir(parents=True, exist_ok=True)
    return p
