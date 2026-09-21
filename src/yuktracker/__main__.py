"""`python -m yuktracker` 엔트리 — PyInstaller 도 이 모듈을 가리킨다(`build.bat`)."""
from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
