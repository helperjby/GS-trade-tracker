"""SEAssist `config` 모듈의 심 — 벤더 사본이 부르는 두 함수 + 공용 `appdata_base()` (이 프로젝트 소유, 동기화 제외).

SEAssist 의 `config.py` 는 3,900줄짜리 설정·프로파일 모듈이고 템플릿 b64·시나리오 상수를
끌고 온다. 벤더 사본(`packet_discovery_ledger`·`ledger_paths`)이 실제로 쓰는 것은
``app_dir()`` (원장 폴백 경로) 과 ``load_settings()`` (경로 계산용 키 몇 개) 뿐이라, 같은
이름·같은 의미의 함수 두 개로 대체한다.

계약(SEAssist 와 같아야 하는 것)
- ``app_dir()`` = ``%APPDATA%\\SEAssist`` — SEAssist 가 설정을 두는 폴더 그대로. 그래서 SEAssist 가
  설치된 PC 에서는 **같은 settings.json 을 읽어 같은 원장 폴더**(`packet_data_dir` / OneDrive
  검토 폴더 / 디바이스 이름)에 창을 남긴다. 레거시 마이그레이션·profiles 생성은 하지 않는다.
- ``load_settings()`` 가 돌려주는 객체의 속성 이름은 SEAssist ``Settings.to_dict()`` 의 키와 같다.
  `ledger_paths` 가 읽는 것은 ``display_name`` / ``packet_data_dir`` / ``wordinput_review_dir``,
  관측기가 읽는 것은 ``dashboard_server_url`` / ``dashboard_secret`` / ``lan_shared_secret``.
  파일이 없거나 깨졌으면 전부 빈 문자열(SEAssist 의 기본값과 같다).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

APP_DIR_NAME = "SEAssist"
SETTINGS_FILE = "settings.json"

#: 관측기가 아는 설정 키 — 전부 문자열, 기본값 빈 문자열.
SETTINGS_KEYS = (
    "display_name",
    "packet_data_dir",
    "wordinput_review_dir",
    "dashboard_server_url",
    "dashboard_secret",
    "lan_shared_secret",
)


def appdata_base() -> Path:
    """`%APPDATA%`(없으면 홈) — 이 심과 프로젝트의 `paths.app_dir` 이 같은 폴백 규칙을 쓴다."""
    return Path(os.environ.get("APPDATA") or Path.home())


def app_dir() -> Path:
    p = appdata_base() / APP_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_path() -> Path:
    return app_dir() / SETTINGS_FILE


def load_settings() -> SimpleNamespace:
    raw: dict = {}
    try:
        text = settings_path().read_text(encoding="utf-8")
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            raw = loaded
    except (OSError, ValueError):
        raw = {}
    values = {k: str(raw.get(k, "") or "") for k in SETTINGS_KEYS}
    return SimpleNamespace(**values)
