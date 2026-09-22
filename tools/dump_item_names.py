"""아이템 id→이름 표 점검·덤프 — 클라 `gersang.gcs` 의 육의전 검색 리스트 (PR-Y2b, 2026-09-21).

    python tools\\dump_item_names.py --check                 # 표 추출 + 라벨 앵커 8/8 대조 (실기기 게이트)
    python tools\\dump_item_names.py --out item_names.json   # JSON 덤프 — 레포 밖에 둘 것(.gitignore)
    python tools\\dump_item_names.py --ids 853,3506          # id 풀기
    python tools\\dump_item_names.py --ids-from probe.txt    # SEAssist `packet_market_probe.py --rows` 출력의
                                                            #   `아이템=N` 전부 풀기 (`-` 는 stdin) → 해석률
    python tools\\dump_item_names.py --search 봉인           # 이름 검색(정규화 정확 → 부분)

옵션: `--client-dir DIR`(그 폴더만 본다 — gcs 가 없으면 다른 클라로 넘어가지 않는다) · `--gcs FILE` · `--cache FILE` ·
`--no-cache`(읽지도 쓰지도 않음) · `--refresh`(재스캔 후 캐시 갱신; 실패하면 캐시 폴백)
종료 코드: 0 정상 / 1 앵커 불일치·미해석 id·표 추출 실패(gcs 는 있는데 표가 없다) / 2 클라(gersang.gcs) 없음
`--check` 는 게이트라 묵은 캐시 폴백도 실패로 본다(사유는 stderr `경고:` 줄). `--ids-from` 은 `아이템=N` 토큰만
뽑고(맨 정수는 등록번호·수량·가격과 섞여 안 뽑는다 — `--ids` 를 쓸 것), 파일은 UTF-8 → cp949 순으로 읽는다
(리다이렉트된 Windows 콘솔 출력은 cp949 일 수 있다).

앵커 8쌍은 SEAssist docs/PACKET-MARKET-2026-09-21.md 의 라벨 대조 행(게임 아이템명 — 판매자명이 아니다).
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from yuktracker import item_names as IN  # noqa: E402
from yuktracker import paths  # noqa: E402

#: 라벨 앵커(패킷 `@4` id → 게임 UI 이름). 표의 `[M]봉인의돌` 은 `display_name` 으로 `봉인의돌`.
KNOWN_ANCHORS: dict[int, str] = {
    853: "봉인의돌", 2204: "청색 정기의 돌", 3506: "봉인의서", 5317: "시간의금화",
    6215: "작은바람의속성석", 6342: "흑호의발톱", 8245: "[천권] 주술 비법", 13468: "파손된 집행 견갑",
}
_ITEM_RE = re.compile(r"아이템=(\d+)")
_ORIGIN_LABEL = {"cache": "hit", "extracted": "miss", "cache-fallback": "폴백(묵은 캐시)"}


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="표 추출 + 라벨 앵커 대조")
    ap.add_argument("--out", type=Path, default=None, help="JSON 덤프 경로(레포 밖)")
    ap.add_argument("--ids", default=None, help="쉼표 구분 id")
    ap.add_argument("--ids-from", default=None, metavar="FILE",
                    help="`아이템=N` 토큰을 뽑아 전부 푼다 — `-` 는 stdin (UTF-8 → cp949)")
    ap.add_argument("--search", default=None, help="이름 검색")
    ap.add_argument("--client-dir", default=None, help="클라 폴더(gersang.gcs 가 있는 곳) — 이 폴더만 본다")
    ap.add_argument("--gcs", type=Path, default=None, help="gersang.gcs 파일 직접 지정")
    # 도움말은 리터럴 — 기본 경로 함수를 부르지 않는다(--help 가 폴더를 만들면 안 된다).
    ap.add_argument("--cache", type=Path, default=None,
                    help=f"캐시 파일(기본 %%APPDATA%%\\{paths.APP_DIR_NAME}\\{paths.ITEM_TABLE_CACHE_NAME})")
    ap.add_argument("--no-cache", action="store_true", help="캐시를 읽지도 쓰지도 않는다")
    ap.add_argument("--refresh", action="store_true", help="캐시를 무시하고 재스캔한 뒤 갱신")
    return ap


def decode_text(raw: bytes) -> str:
    """UTF-8(BOM 허용) → cp949 → UTF-8 replace 순. 리다이렉트된 Windows 콘솔 출력(cp949)도 읽는다."""
    for enc in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def ids_from_text(text: str) -> list[int]:
    """`아이템=N` 토큰만(중복 제거, 등장 순). 맨 정수는 뽑지 않는다 — 등록번호·수량·가격이 id 로 섞인다."""
    out: list[int] = []
    seen: set[int] = set()
    for tok in _ITEM_RE.findall(text):
        v = int(tok)
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def check(table: IN.ItemTable, *, scan_sec: float, origin: str) -> int:
    s = table.source
    print(f"gcs: {s.gcs_path} ({s.gcs_size:,} B) sha256 {s.gcs_sha256[:16]}… archive_ts {s.archive_ts or '?'}")
    print(f"표: {s.rows:,}행 (skipped {s.skipped}, 중복 id {s.duplicate_ids}, 해독 오류 {s.decode_errors}) · "
          f"스트림 offset {s.stream_offset:,} · 캐시 {_ORIGIN_LABEL.get(origin, origin)} · {scan_sec:.2f}s · "
          f"extracted_at {s.extracted_at}")
    ok = 0
    for item_id, want in KNOWN_ANCHORS.items():
        got = table.lookup(item_id)
        hit = got == want
        ok += hit
        print(f"  {'O' if hit else 'X'} {item_id:>6} → {got!r}" + ("" if hit else f" (기대 {want!r})"))
    print(f"앵커 {ok}/{len(KNOWN_ANCHORS)} 일치")
    return 0 if ok == len(KNOWN_ANCHORS) else 1


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    a = build_parser().parse_args(argv)
    if not any((a.check, a.out, a.ids, a.ids_from, a.search)):
        a.check = True
    t0 = time.perf_counter()
    res = IN.load_item_table_result(client_dir=a.client_dir, gcs_path=a.gcs, cache_path=a.cache,
                                    refresh=a.refresh, use_cache=not a.no_cache)
    scan_sec = time.perf_counter() - t0
    if res.error:
        print(f"경고: {res.error}", file=sys.stderr)
    if res.table is None:
        # gcs 를 못 찾았으면 2, gcs 는 있는데 표를 못 뽑았으면 1 — 사유는 위 `경고:` 줄.
        return 2 if res.gcs_path is None else 1
    table = res.table
    rc = 0
    if a.check:
        rc = max(rc, check(table, scan_sec=scan_sec, origin=res.origin))
        if res.origin == "cache-fallback":
            rc = max(rc, 2 if res.gcs_path is None else 1)
    if a.out:
        table.write_json(a.out)
        print(f"저장: {a.out} ({len(table):,}건)")
    ids: list[int] = []
    if a.ids:
        ids.extend(int(t) for t in a.ids.split(",") if t.strip())
    if a.ids_from:
        raw = sys.stdin.buffer.read() if a.ids_from == "-" else Path(a.ids_from).read_bytes()
        ids.extend(ids_from_text(decode_text(raw)))
    if ids:
        unresolved = []
        for i in ids:
            name = table.lookup(i)
            if name is None:
                unresolved.append(i)
            print(f"{i}\t{name if name is not None else '?'}")
        print(f"해석 {len(ids) - len(unresolved)}/{len(ids)}" + (f" · 미해석 {unresolved}" if unresolved else ""))
        if unresolved:
            rc = max(rc, 1)
    if a.search:
        hits = table.search(a.search)
        for i in hits:
            print(f"{i}\t{table.lookup(i)}")
        print(f"검색 {a.search!r}: {len(hits)}건")
    return rc


if __name__ == "__main__":
    sys.exit(main())
