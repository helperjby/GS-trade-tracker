"""SEAssist 패킷 코어 동기화 — SEAssist 레포 `src/core` 의 허용 목록 모듈을 `src/yuktracker/seassist/` 로.

왜 벤더 사본인가
----------------
관측기는 SEAssist(거상 매크로)의 프레이머(`gersang_protocol`)·스니퍼 엔진(`packet_state_source`)·
수집기(`packet_discovery_ledger`)를 **그대로** 써야 한다 — 오프라인 재생(`packet_explore.py`)이
프로덕션과 같은 경로를 밟아야 발굴 결과가 그대로 이식되기 때문이다(PACKET-TOOLS §1).
두 프로젝트가 모두 최상위 `src` 패키지라 sys.path 심으로 상대를 import 하면 한쪽이 다른 쪽을
가린다(PACKET-FINDINGS §7 의 함정). 그래서 파일을 복사하되 **출처 커밋과 해시를 VENDOR.json 에
남기고**, 이 스크립트 하나로만 갱신한다 — 손으로 고친 사본은 `--check` 가 드리프트로 잡는다.

규칙
----
- 프로토콜·판정 함수의 정본은 언제나 SEAssist 레포다. 육의전 opcode·파서(PR-Y2)도 거기서
  코퍼스 테스트와 함께 들어온 뒤 이 스크립트로 가져온다. 사본을 직접 고치지 않는다.
- `config.py` 는 동기화 대상이 **아니다** — SEAssist 의 거대한 설정 모듈 대신 이 프로젝트가
  소유하는 심(`app_dir`/`load_settings` 두 함수)이다.
- 복사 대상은 relative import 만 쓰므로 패키지 이름이 달라도 그대로 동작한다.

사용
----
    python tools/sync_seassist_core.py --source <레포 경로>  # 체크아웃/worktree 를 직접 지정
    set SEASSIST_REPO=<레포 경로> && python tools/sync_seassist_core.py
    python tools/sync_seassist_core.py --check              # 사본 == 소스 인지만 확인
                                                            # (rc 1 = 드리프트, rc 2 = 소스 없음/미지정)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "src" / "yuktracker" / "seassist"
#: SEAssist 체크아웃 위치는 기기마다 다르고, 이 레포는 공개다 — 경로를 소스에 박지 않는다.
SOURCE_ENV = "SEASSIST_REPO"
SOURCE_SUBDIR = Path("src") / "core"
VENDOR_FILE = DEST / "VENDOR.json"

#: 허용 목록 — 이 순서로 검사·복사한다. 새 모듈이 필요하면 여기 한 줄 + import 경계 테스트.
MODULES = (
    "admin.py",
    "gersang_protocol.py",
    "ledger_paths.py",
    "log_history.py",
    "logger.py",
    "packet_discovery_ledger.py",
    "packet_state_source.py",
    "pcap_ffi.py",
    "tcp_flow_map.py",
    "wordinput_packet_state.py",
)
#: 프로젝트 소유 — 동기화·드리프트 검사에서 제외.
OWNED = ("__init__.py", "config.py", "VENDOR.json")


def normalize(data: bytes) -> bytes:
    """줄바꿈을 LF 로 — SEAssist 체크아웃은 Windows 라 CRLF 이고 이 레포는 `.gitattributes` 로 LF 다.
    복사·해시·비교 전부 이 정규화를 거친다(CI 의 LF 체크아웃에서 해시가 어긋난 실패가 근거)."""
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


def check(source: Path) -> int:
    """사본과 소스가 같은지. 다르면 파일별로 찍고 1."""
    src_dir = source / SOURCE_SUBDIR
    bad = 0
    for name in MODULES:
        s, d = src_dir / name, DEST / name
        if not s.is_file():
            print(f"소스 없음: {s}")
            bad += 1
            continue
        if not d.is_file():
            print(f"사본 없음: {d}")
            bad += 1
            continue
        if _sha256(s) != _sha256(d):
            print(f"드리프트: {name}")
            bad += 1
    vendor = load_vendor()
    for name, meta in (vendor.get("files") or {}).items():
        d = DEST / name
        if d.is_file() and _sha256(d) != meta.get("sha256"):
            print(f"VENDOR.json 불일치(사본이 손으로 바뀜?): {name}")
            bad += 1
    print("동기화 상태: " + ("일치" if bad == 0 else f"불일치 {bad}건"))
    return 1 if bad else 0


def sync(source: Path) -> int:
    src_dir = source / SOURCE_SUBDIR
    missing = [n for n in MODULES if not (src_dir / n).is_file()]
    if missing:
        print(f"소스에 없는 모듈: {missing} — {src_dir}")
        return 2
    DEST.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict] = {}
    changed = 0
    for name in MODULES:
        data = normalize((src_dir / name).read_bytes())
        dst = DEST / name
        if not dst.is_file() or normalize(dst.read_bytes()) != data:
            dst.write_bytes(data)
            changed += 1
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    # 출처는 커밋으로만 기록한다 — 로컬 절대 경로·브랜치명은 공개 레포에 남기지 않는다.
    vendor = {
        "source": "SEAssist",
        "source_subdir": str(SOURCE_SUBDIR).replace("\\", "/"),
        "commit": _git(source, "rev-parse", "HEAD"),
        "dirty": bool(_git(source, "status", "--porcelain", "--", str(SOURCE_SUBDIR))),
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "owned": list(OWNED),
        "files": files,
    }
    VENDOR_FILE.write_text(json.dumps(vendor, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(f"동기화 완료 — 변경 {changed}개 / {len(MODULES)}개, 커밋 {vendor['commit'][:10] or '?'}"
          f"{' (dirty)' if vendor['dirty'] else ''} → {DEST}")
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
    ap.add_argument("--check", action="store_true", help="복사하지 않고 일치 여부만 검사")
    a = ap.parse_args(argv)
    raw = a.source or os.environ.get(SOURCE_ENV)
    if not raw:
        print(f"SEAssist 레포 위치가 없다 — --source <경로> 또는 환경변수 {SOURCE_ENV} 를 설정한다.")
        return 2
    source = Path(raw).resolve()
    if not (source / SOURCE_SUBDIR).is_dir():
        print(f"SEAssist 소스 없음: {source / SOURCE_SUBDIR}")
        return 2
    return check(source) if a.check else sync(source)


if __name__ == "__main__":
    raise SystemExit(main())
