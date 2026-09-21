"""PyInstaller 엔트리 (`build.bat`) 겸 개발 실행용 — `src/` 를 경로에 넣고 `yuktracker.cli.main` 으로."""
from __future__ import annotations

import os
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from yuktracker.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
