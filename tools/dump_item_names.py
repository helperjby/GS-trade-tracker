"""아이템 id→이름 표 점검·덤프 — 클라 `gersang.gcs` 의 육의전 검색 리스트 (PR-Y2b, 2026-09-21).

    python tools\\dump_item_names.py --check                 # 표 추출 + 라벨 앵커 8/8 대조 (실기기 게이트)
    python tools\\dump_item_names.py --out item_names.json   # JSON 덤프 — 레포 밖에 둘 것(.gitignore)
    python tools\\dump_item_names.py --ids 853,3506          # id 풀기
    python tools\\dump_item_names.py --ids-from probe.txt    # SEAssist `packet_market_probe.py --rows` 출력의
                                                            #   `아이템=N` 전부 풀기 (`-` 는 stdin) → 해석률
    python tools\\dump_item_names.py --search 봉인           # 이름 검색(정규화 정확 → 부분)

옵션: `--client-dir DIR` · `--gcs FILE` · `--cache FILE` · `--no-cache`(읽지도 쓰지도 않음) · `--refresh`(재스캔 후 캐시 갱신)
종료 코드: 0 정상 / 1 앵커 불일치·미해석 id 있음 / 2 클라(gersang.gcs) 없음

앵커 8쌍은 SEAssist docs/PACKET-MARKET-2026-09-21.md 의 라벨 대조 행(게임 아이템명 — 판매자명이 아니다).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from yuktracker import item_names as IN  # noqa: E402

#: 라벨 앵커(패킷 `@4` id → 게임 UI 이름). 표의 `[M]봉인의돌` 은 `display_name` 으로 `봉인의돌`.
KNOWN_ANCHORS: dict[int, str] = {
    853: "봉인의돌", 2204: "청색 정기의 돌", 3506: "봉인의서", 5317: "시간의금화",
    6215: "작은바람의속성석", 6342: "흑호의발톱", 8245: "[천권] 주술 비법", 13468: "파손된 집행 견갑",
}
_ITEM_RE = re.compile(r"아이템=(\d+)")
_INT_RE = re.compile(r"(?<![\w.])(\d{1,7})(?![\w.])")


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
                    help="`아이템=N`(없으면 정수) 을 뽑아 전부 푼다 — `-` 는 stdin")
    ap.add_argument("--search", default=None, help="이름 검색")
    ap.add_argument("--client-dir", default=None, help="클라 폴더(gersang.gcs 가 있는 곳)")
    ap.add_argument("--gcs", type=Path, default=None, help="gersang.gcs 파일 직접 지정")
    ap.add_argument("--cache", type=Path, default=None, help=f"캐시 파일(기본 {IN.item_table_cache_path()})")
    ap.add_argument("--no-cache", action="store_true", help="캐시를 읽지도 쓰지도 않는다")
    ap.add_argument("--refresh", action="store_true", help="캐시를 무시하고 재스캔한 뒤 갱신")
    return ap


def ids_from_text(text: str) -> list[int]:
    """`아이템=N` 이 하나라도 있으면 그것만, 없으면 정수 토큰 전부(중복 제거, 등장 순)."""
    found = _ITEM_RE.findall(text) or _INT_RE.findall(text)
    out: list[int] = []
    seen: set[int] = set()
    for tok in found:
        v = int(tok)
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def check(table: IN.ItemTable, *, scan_sec: float, cache_hit: bool) -> int:
    s = table.source
    print(f"gcs: {s.gcs_path} ({s.gcs_size:,} B) head_sha256 {s.head_sha256[:16]}… archive_ts {s.archive_ts or '?'}")
    print(f"표: {s.rows:,}행 (skipped {s.skipped}, 중복 id {s.duplicate_ids}, 해독 오류 {s.decode_errors}) · "
          f"스트림 offset {s.stream_offset:,} · 캐시 {'hit' if cache_hit else 'miss'} · {scan_sec:.2f}s · "
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
    cache = a.cache
    cache_hit = False
    if a.cache and not a.no_cache and not a.refresh and IN._read_cache(Path(a.cache)) is not None:
        cache_hit = True
    elif not a.cache and not a.no_cache and not a.refresh and IN._read_cache(IN.item_table_cache_path()) is not None:
        cache_hit = True
    t0 = time.perf_counter()
    table = IN.load_item_table(client_dir=a.client_dir, gcs_path=a.gcs, cache_path=cache,
                               refresh=a.refresh, use_cache=not a.no_cache)
    scan_sec = time.perf_counter() - t0
    if table is None:
        print(f"클라 폴더({IN.GCS_NAME})를 찾지 못했다 — --client-dir / --gcs / %{IN.ENV_CLIENT_DIR}%", file=sys.stderr)
        return 2
    # 캐시 hit 인데 추출이 새로 일어났으면(불일치) miss 로 정정.
    if cache_hit and scan_sec > 0.2:
        cache_hit = False
    rc = 0
    if a.check:
        rc = max(rc, check(table, scan_sec=scan_sec, cache_hit=cache_hit))
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        table.write_json(a.out)
        print(f"저장: {a.out} ({len(table):,}건)")
    ids: list[int] = []
    if a.ids:
        ids.extend(int(t) for t in a.ids.split(",") if t.strip())
    if a.ids_from:
        text = sys.stdin.read() if a.ids_from == "-" else Path(a.ids_from).read_text(encoding="utf-8", errors="replace")
        ids.extend(ids_from_text(text))
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
