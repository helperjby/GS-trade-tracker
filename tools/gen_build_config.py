"""빌드 시 허브 기본 주소 주입 — `src/yuktracker/_build_config.py` 를 만든다(`.gitignore` 대상).

이 레포는 공개다. 허브의 공개 주소(Tailscale Funnel 의 tailnet 이름)를 소스에 둘 수 없으므로
**빌드할 때** 환경변수에서 받아 모듈 하나를 생성하고, exe 가 그걸 기본값으로 쓴다. 사용자는
초대 코드만 입력하면 된다.

    set YUKTRACKER_HUB_URL=https://<노드>.<tailnet>.ts.net:10000
    python tools\\gen_build_config.py          # → src/yuktracker/_build_config.py
    python tools\\gen_build_config.py --hub-url https://...   # 환경변수 대신 인자로

주소가 없으면 **기존 파일을 지우고 rc 0** 이다 — 주소 없는 빌드도 정상이고(사용자가 `--hub-url`
로 준다), 지난 빌드의 주소가 남아 따라붙지 않는다. 주소 모양이 http(s) 가 아니면 rc 1.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "src" / "yuktracker" / "_build_config.py"
ENV = "YUKTRACKER_HUB_URL"
HEADER = '''"""빌드 시 생성 — 이 파일은 레포에 커밋하지 않는다(tools/gen_build_config.py)."""
'''


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hub-url", default="", metavar="URL",
                    help=f"허브 공개 주소 (생략 시 환경변수 {ENV})")
    a = ap.parse_args(argv)
    url = (a.hub_url or os.environ.get(ENV, "")).strip().rstrip("/")
    if not url:
        if TARGET.exists():
            TARGET.unlink()
            print(f"허브 주소가 없어 {TARGET.name} 를 지웠습니다 — 사용자가 --hub-url 로 줍니다.")
        else:
            print(f"허브 주소가 없습니다({ENV} 미설정) — 주소 없이 빌드합니다.")
        return 0
    if not (url.startswith("http://") or url.startswith("https://")):
        print(f"허브 주소가 http(s) 로 시작해야 합니다: {url!r}")
        return 1
    TARGET.write_text(HEADER + f'HUB_URL = {url!r}\n', encoding="utf-8")
    print(f"허브 기본 주소를 주입했습니다 → {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
