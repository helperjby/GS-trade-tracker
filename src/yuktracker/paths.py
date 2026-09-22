"""이 프로젝트가 소유하는 로컬 경로 — `%APPDATA%\\YukTracker` (아이템 표 캐시, 나중의 스풀).

SEAssist 심(`seassist.config.app_dir`)은 `%APPDATA%\\SEAssist` 의 설정을 **읽기만** 한다. 관측기가
직접 쓰는 파일(추출한 아이템 표 캐시, PR-Y1b 의 업로드 스풀)은 그 폴더를 더럽히지 않고 여기에 둔다.
OneDrive 가 아니라 기기 로컬이다 — 캐시는 그 PC 의 클라 빌드에 묶이고, 스풀은 업로드 뒤 지운다.

경로 계산은 **순수**다(폴더를 만들지 않는다) — 실제로 쓰는 쪽(`ItemTable.write_json`)이 만든다. 도움말
문자열·`--help`·테스트 subprocess 가 사용자 프로필에 폴더를 남기지 않는다. `%APPDATA%` 폴백 규칙은
심의 `appdata_base()` 하나를 같이 쓴다.
"""
from __future__ import annotations

from pathlib import Path

from .seassist.config import appdata_base

APP_DIR_NAME = "YukTracker"
ITEM_TABLE_CACHE_NAME = "item_names.json"
CONFIG_NAME = "config.json"
SPOOL_DIR_NAME = "spool"
QUARANTINE_DIR_NAME = "quarantine"


def app_dir() -> Path:
    """`%APPDATA%\\YukTracker` — 경로만(만들지 않는다)."""
    return appdata_base() / APP_DIR_NAME


def item_table_cache_path() -> Path:
    return app_dir() / ITEM_TABLE_CACHE_NAME


def config_path() -> Path:
    """`%APPDATA%\\YukTracker\\config.json` — 허브 주소·기기 토큰(`app_config`)."""
    return app_dir() / CONFIG_NAME


def spool_dir() -> Path:
    """업로드 대기 배치(`obs_*.jsonl`) — 200 응답 뒤에 지운다."""
    return app_dir() / SPOOL_DIR_NAME


def quarantine_dir() -> Path:
    """허브가 400 으로 거부한 배치 — 재시도해도 같은 400 이라 옆으로 치운다(진단용)."""
    return spool_dir() / QUARANTINE_DIR_NAME
