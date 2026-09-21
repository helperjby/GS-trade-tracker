"""`src/` 를 경로에 — 설치 없이 `python -m pytest` 로 돈다."""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
