"""이 프로젝트가 소유하는 로컬 경로 — `%APPDATA%\\YukTracker` (아이템 표 캐시, 나중의 스풀).

SEAssist 심(`seassist.config.app_dir`)은 `%APPDATA%\\SEAssist` 의 설정을 **읽기만** 한다. 관측기가
직접 쓰는 파일(추출한 아이템 표 캐시, PR-Y1b 의 업로드 스풀)은 그 폴더를 더럽히지 않고 여기에 둔다.
OneDrive 가 아니라 기기 로컬이다 — 캐시는 그 PC 의 클라 빌드에 묶이고, 스풀은 업로드 뒤 지운다.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "YukTracker"
ITEM_TABLE_CACHE_NAME = "item_names.json"


def app_dir() -> Path:
    """`%APPDATA%\\YukTracker` — 없으면 만든다(실패해도 경로는 돌려준다)."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    p = Path(base) / APP_DIR_NAME
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


def item_table_cache_path() -> Path:
    return app_dir() / ITEM_TABLE_CACHE_NAME
