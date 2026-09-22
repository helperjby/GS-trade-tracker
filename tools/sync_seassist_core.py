"""패킷 코어의 **출처 기록·차이 보고** — `src/yuktracker/seassist/` 는 이 레포 소유다.

왜 소유인가 (2026-09-22 결정)
-----------------------------
이 폴더는 원래 SEAssist 레포 `src/core` 의 벤더 사본이었고, 이 스크립트가 복사·해시 고정을 맡았다.
그런데 육의전 프레이머 확장(`observe_market` 전량 대기)과 엔진의 `market_cb` 는 `StreamFramer.feed()`
루프 **안**에 들어가야 하고(서브클래스는 루프 전체 복제가 된다), SEAssist 레포에는 더 이상 변경을 내지
않기로 했다. 그래서 **전량 소유 전환**: 복사 경로를 없애고, 파일은 이 레포가 직접 고친다.

남는 것은 추적성이다 — `VENDOR.json` 이 파일마다 **출처 커밋과 그 시점의 해시**를 들고 있고,
지금 내용이 출처와 달라진 파일은 `diverged` 에 사유와 함께 선언한다. 선언이 실제와 어긋나면
`--check` 와 `tests/test_vendor.py` 가 잡는다. 상류가 궁금하면 `--upstream` 으로 체크아웃과 비교한다.

사용
----
    python tools/sync_seassist_core.py --check                 # 출처 대비 차이 == diverged 선언 ? (rc 1 = 불일치)
    python tools/sync_seassist_core.py --upstream              # 상류 체크아웃과의 차이 보고(정보용, rc 0)
    python tools/sync_seassist_core.py --upstream --source <레포 경로>
                                                               # 소스 없음/미지정 = rc 2 (빌드는 건너뛴다)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "src" / "yuktracker" / "seassist"
#: SEAssist 체크아웃 위치는 기기마다 다르고, 이 레포는 공개다 — 경로를 소스에 박지 않는다.
SOURCE_ENV = "SEASSIST_REPO"
SOURCE_SUBDIR = Path("src") / "core"
VENDOR_FILE = DEST / "VENDOR.json"

#: 동기화(복사) 대상 — **없다**. 전량 소유 전환(2026-09-22) 이후 이 스크립트는 파일을 쓰지 않는다.
MODULES: tuple[str, ...] = ()
#: 프로젝트 소유 = 이 폴더 전부. 출처가 있는 파일은 `VENDOR.json` 의 `origin.files` 가 기록한다.
OWNED = (
    "__init__.py",
    "admin.py",
    "config.py",
    "gersang_protocol.py",
    "ledger_paths.py",
    "log_history.py",
    "logger.py",
    "packet_discovery_ledger.py",
    "packet_market.py",
    "packet_state_source.py",
    "pcap_ffi.py",
    "tcp_flow_map.py",
    "wordinput_packet_state.py",
    "VENDOR.json",
)


def normalize(data: bytes) -> bytes:
    """줄바꿈을 LF 로 — SEAssist 체크아웃은 Windows 라 CRLF 이고 이 레포는 `.gitattributes` 로 LF 다.
    해시·비교 전부 이 정규화를 거친다(CI 의 LF 체크아웃에서 해시가 어긋난 실패가 근거)."""
    return data.replace(b"\r\n", b"\n")


def _sha256(p: Path) -> str:
    return hashlib.sha256(normalize(p.read_bytes())).hexdigest()


def _git(source: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(source), *args], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def load_vendor() -> dict:
    if not VENDOR_FILE.is_file():
        return {}
    return json.loads(VENDOR_FILE.read_text(encoding="utf-8"))


def origin_files(vendor: dict | None = None) -> dict[str, dict]:
    """`VENDOR.json` 의 출처 기록 — `{파일: {"sha256", "bytes"}}`."""
    v = load_vendor() if vendor is None else vendor
    return dict((v.get("origin") or {}).get("files") or {})


def diverged_now(vendor: dict | None = None) -> list[str]:
    """출처 기록과 **지금 내용이 다른** 파일(정렬). 사본이 없으면 그것도 차이로 본다."""
    out = []
    for name, meta in sorted(origin_files(vendor).items()):
        p = DEST / name
        if not p.is_file() or _sha256(p) != meta.get("sha256"):
            out.append(name)
    return out


def check() -> int:
    """실제 차이 == `diverged` 선언 인지. 소스 체크아웃이 필요 없다."""
    vendor = load_vendor()
    origin = origin_files(vendor)
    if not origin:
        print("VENDOR.json 에 출처 기록(origin.files)이 없다.")
        return 1
    declared = {str(d.get("file")) for d in (vendor.get("diverged") or [])}
    actual = set(diverged_now(vendor))
    for name in sorted(actual - declared):
        print(f"선언 없는 변경: {name} — VENDOR.json 의 diverged 에 사유와 함께 적는다")
    for name in sorted(declared - actual):
        print(f"선언만 있고 차이 없음: {name} — diverged 에서 지운다")
    missing = [n for n in sorted(origin) if not (DEST / n).is_file()]
    for name in missing:
        print(f"파일 없음: {name}")
    bad = len(actual ^ declared) + len(missing)
    print(f"출처 선언: " + ("일치" if bad == 0 else f"불일치 {bad}건")
          + f" (기록 {len(origin)}개 / 변경 {len(actual)}개)")
    return 1 if bad else 0


def upstream(source: Path) -> int:
    """상류 체크아웃과의 차이 보고 — **정보용**(이 레포가 소유하므로 차이는 정상이다)."""
    src_dir = source / SOURCE_SUBDIR
    vendor = load_vendor()
    commit = (vendor.get("origin") or {}).get("commit") or ""
    print(f"출처 커밋 {commit[:10] or '?'} · 상류 HEAD {_git(source, 'rev-parse', 'HEAD')[:10] or '?'}"
          f" — {src_dir}")
    for name in sorted(origin_files(vendor)):
        s, d = src_dir / name, DEST / name
        if not s.is_file():
            print(f"  상류 없음   {name}")
        elif not d.is_file():
            print(f"  여기 없음   {name}")
        elif _sha256(s) == _sha256(d):
            print(f"  같음        {name}")
        else:
            print(f"  다름        {name}")
    for name in sorted(set(OWNED) - set(origin_files(vendor)) - {"VENDOR.json"}):
        print(f"  이 레포 것  {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", type=Path, default=None,
                    help=f"SEAssist 레포 루트 (생략 시 환경변수 {SOURCE_ENV})")
    ap.add_argument("--check", action="store_true",
                    help="출처 대비 차이가 VENDOR.json 의 diverged 선언과 같은지 (기본)")
    ap.add_argument("--upstream", action="store_true",
                    help="상류 체크아웃과 파일별로 비교해 보고(정보용)")
    a = ap.parse_args(argv)
    if not a.upstream:
        return check()
    raw = a.source or os.environ.get(SOURCE_ENV)
    if not raw:
        print(f"SEAssist 레포 위치가 없다 — --source <경로> 또는 환경변수 {SOURCE_ENV} 를 설정한다.")
        return 2
    source = Path(raw).resolve()
    if not (source / SOURCE_SUBDIR).is_dir():
        print(f"SEAssist 소스 없음: {source / SOURCE_SUBDIR}")
        return 2
    return upstream(source)


if __name__ == "__main__":
    raise SystemExit(main())
