"""`src/` 를 경로에 — 설치 없이 `python -m pytest` 로 돈다."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _no_ambient_hub_url(monkeypatch):
    """빌드 PC 의 허브 주소가 테스트에 새지 않게 — `%YUKTRACKER_HUB_URL%` 과 빌드 주입(`_build_config.py`) 둘 다.

    `build.bat` 은 DEPLOY §2 대로 환경변수를 켠 채 테스트 단계를 돌리고, 이전 빌드가 남긴 `_build_config.py` 가
    있으면 `app_config.BUILD_HUB_URL` 도 차 있다. "허브 주소가 없다"를 전제한 테스트(자가진단 SKIP·관측만·등록
    프롬프트 없음)가 그 PC 에서만 깨진다 — 2026-09-23 첫 실빌드에서 4건. 주소가 필요한 테스트는 명시적으로 준다.
    """
    from yuktracker import app_config

    monkeypatch.delenv(app_config.ENV_HUB_URL, raising=False)
    monkeypatch.setattr(app_config, "BUILD_HUB_URL", "")
