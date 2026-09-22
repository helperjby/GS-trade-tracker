"""육의전 목록(`0x321f`) 오프라인 판독 — 수집 창을 **읽기 전용**으로 재생해 페이지를 표로 찍는다.

두 경로를 따로 돌려 **대조**한다:

1. **라이브 재생** — 프로덕션과 같은 경로(`FlowDecoder(observe_market=True)` 에 도착 순 세그먼트 →
   `take_market()` → `parse_market_page`). 락 로직·재정렬·중복 제거를 그대로 밟는다.
2. **프로브** — 프레이머와 독립. (슬롯, 포트)별 seq 순 재조립 스트림에서 헤더 패턴(`00 1f 32 00`)을
   찾고 `len(body) == 9 + 48×count` 로만 거른다.

둘이 다르면 ⚠ 와 rc 1 — 프레이머가 놓쳤거나(락 지연·조기 발화) 프로브가 헛것을 잡은 것이다.
페이지가 하나도 없으면 rc 2. 판정 근거는 `docs/PACKET-MARKET.md`.

    python tools\\market_probe.py --root <패킷 루트>                 # 0x321f 가 있는 전 창
    python tools\\market_probe.py --root <패킷 루트> --window 20260921_2023 --rows
    python tools\\market_probe.py --root <패킷 루트> --slot 1        # 특정 클라만

`<패킷 루트>` 는 `<루트>/<디바이스>/packet_discovery/window_*.jsonl` 구조의 뿌리다(관측기 시작 줄의
`원장 폴더` 의 **부모**). 창 파일은 열기만 하고 쓰지 않는다 — 실캡처는 레포 밖에 둔다.

**판매자명은 기본으로 가린다**(`mask_seller` 한 정의). 원본이 필요하면 `--raw-seller`, 로컬 진단용이고
출력을 문서·PR·채팅에 붙일 때는 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import base64
import bisect
import collections
import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from yuktracker.seassist import gersang_protocol as gp  # noqa: E402
from yuktracker.seassist import packet_market as pm  # noqa: E402

HDR = bytes.fromhex("001f3200")   # body[0:4] = magic 0x00 · opcode 0x321f LE · reserved 0x00
HLEN = gp.MARKET_HEADER_LEN
ROW = gp.MARKET_ROW_LEN
_LEDGER_SUBDIR = "packet_discovery"


def _load(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _u32(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 4], "little")


def _is_main_s2c(r: dict) -> bool:
    """8000 s2c 세그먼트만 — 부차 포트(4011)·c2s 는 육의전 경로가 아니다."""
    return (r.get("kind") == "seg" and r.get("sport", 8000) == 8000
            and r.get("dir", "s2c") == "s2c")


def _wall_clock(rows: list[dict]):
    """mono → 벽시계 HH:MM:SS 변환기 (창 헤더의 기준 시각 사용)."""
    start = next((r for r in rows if r.get("kind") == "window_start"), None)
    if start is None:
        return lambda mono: "?"
    mb, wb = float(start["mono_base"]), float(start["wall_base"])
    return lambda mono: datetime.datetime.fromtimestamp(wb + (mono - mb)).strftime("%H:%M:%S")


def rows_of(body: bytes) -> list[bytes]:
    n = gp.market_row_count(body)
    return [body[HLEN + k * ROW: HLEN + (k + 1) * ROW] for k in range(n)]


def seller_of(row: bytes) -> str:
    s = row[24:40]
    s = s[:s.index(0)] if 0 in s else s
    return s.decode("cp949", errors="replace")


def probe_frames(rows: list[dict]) -> list[tuple[float, int, bytes]]:
    """(mono, 슬롯, body) — 프레이머 독립. (슬롯, 포트)별 seq 순으로 잇고 중복 seq 는 첫 것만."""
    streams: dict[tuple[int, int], dict[int, dict]] = collections.defaultdict(dict)
    for r in rows:
        if _is_main_s2c(r):
            streams[(int(r["slot"]), int(r["port"]))].setdefault(int(r["seq"]), r)
    out: list[tuple[float, int, bytes]] = []
    for (slot, _port), byseq in streams.items():
        buf = bytearray()
        offs: list[tuple[int, float]] = []
        for seq in sorted(byseq):
            r = byseq[seq]
            offs.append((len(buf), float(r["mono"])))
            buf += base64.b64decode(r["b64"])
        starts = [o for o, _ in offs]
        i = buf.find(HDR, gp.LEN_PREFIX)
        while i != -1:
            blen = int.from_bytes(buf[i - gp.LEN_PREFIX:i], "little") - gp.LEN_PREFIX
            if blen >= HLEN and (blen - HLEN) % ROW == 0 and i + blen <= len(buf):
                body = bytes(buf[i:i + blen])
                if gp.market_row_count(body) == (blen - HLEN) // ROW:
                    out.append((offs[bisect.bisect_right(starts, i) - 1][1], slot, body))
            i = buf.find(HDR, i + 1)
    return sorted(out)


def live_frames(rows: list[dict]) -> tuple[list[tuple[float, int, bytes]], int, int]:
    """(mono, 슬롯, body) — **프로덕션 경로**. 도착 순 그대로 먹인다. + (거부, 유실) 누계."""
    decoders: dict[tuple[int, int], gp.FlowDecoder] = {}
    out: list[tuple[float, int, bytes]] = []
    rejected = dropped = 0
    for r in rows:
        if not _is_main_s2c(r):
            continue
        key = (int(r["slot"]), int(r["port"]))
        d = decoders.get(key)
        if d is None:
            d = decoders[key] = gp.FlowDecoder(observe_market=True)
        d.feed_segment(int(r["seq"]), base64.b64decode(r["b64"]), float(r["mono"]))
        for obs in d.take_market():
            out.append((obs.ts, key[0], obs.body))
    for d in decoders.values():
        rejected += d.market_rejected
        dropped += d.market_dropped
    return sorted(out), rejected, dropped


def anchors_of(rows: list[dict]) -> list[tuple[str, int, int, str]]:
    """콘솔 라벨 → (판매자, 수량, 가격, 아이템명). 두 표기를 받는다."""
    out = []
    for r in rows:
        if r.get("kind") != "label":
            continue
        note = str((r.get("detail") or {}).get("note", ""))
        ms = re.search(r"판매자\s*:\s*([^,]+)", note)
        mq = re.search(r"판매개수\s*:\s*(\d+)", note)
        mp = re.search(r"단가\s*:\s*([\d,]+)", note)
        if ms and mq and mp:
            item = note.split(",")[0].split(":", 1)[-1].strip()
            out.append((ms.group(1).strip(), int(mq.group(1)), int(mp.group(1).replace(",", "")), item))
            continue
        toks = [t.strip() for t in note.split(",")]
        if len(toks) >= 4 and toks[1].isdigit() and "".join(toks[3:]).replace(" ", "").isdigit():
            out.append((toks[2], int(toks[1]), int("".join(toks[3:]).replace(" ", "")), toks[0]))
    return out


def iter_windows(root: Path) -> list[Path]:
    out: list[Path] = []
    for dev in sorted(p for p in root.iterdir() if p.is_dir()):
        sub = dev / _LEDGER_SUBDIR
        if sub.is_dir():
            out.extend(sorted(sub.glob("window_*.jsonl")))
    return out


def _print_pages(frames, wall, name, show_rows: bool) -> None:
    print(f"  {'시각':<8} {'슬롯':>3} {'b[4]':>4} {'총페이지':>6} {'행수':>4} {'len':>5}  "
          f"{'등록 id 첫/끝':<20} {'내림차순':^8} {'@40':<8} anomaly")
    for mono, slot, body in frames:
        page = pm.parse_market_page(body)
        rs = rows_of(body)
        ids = [_u32(r, 0) for r in rs]
        desc = "-" if not rs else ("O" if ids == sorted(ids, reverse=True) else "X")
        at40 = rs[0][40:44].hex() if rs else "-"
        span = f"{ids[0]}/{ids[-1]}" if ids else "-"
        anomalies = ",".join(page.anomalies) if page is not None else "파싱 실패"
        print(f"  {wall(mono):<8} {slot + 1:>3} {body[4]:>4} "
              f"{int.from_bytes(body[5:7], 'little'):>6} {len(rs):>4} {len(body):>5}  "
              f"{span:<20} {desc:^8} {at40:<8} {anomalies}")
        if show_rows:
            for k, r in enumerate(rs):
                print(f"      row{k:<2} 등록={_u32(r, 0):<8} 아이템={_u32(r, 4):<6} 수량={_u32(r, 8):<5} "
                      f"가격={_u32(r, 16):<11} 판매자={name(seller_of(r))!r:<14} @12={_u32(r, 12)} "
                      f"@20={_u32(r, 20)} @40={r[40:44].hex()} @44={r[44]},{r[45]},{r[46]},{r[47]}")


def _check_anchors(frames, anchors, wall, name) -> tuple[int, int]:
    ok = n = 0
    if anchors:
        print("  라벨 대조(같은 판매자·같은 가격의 행 → 수량 칸):")
    for sel, qty, price, item in anchors:
        n += 1
        pat = sel.encode("cp949", errors="ignore")
        cands = [(mono, k, r) for mono, _slot, body in frames for k, r in enumerate(rows_of(body))
                 if pat and pat in r[24:40] and _u32(r, 16) == price]
        if not cands:
            print(f"    X {item!r} / {name(sel)!r} 수량 {qty} 가격 {price:,} — 판매자·가격이 맞는 행 없음")
            continue
        mono, k, r = next((c for c in cands if _u32(c[2], 8) == qty), cands[0])
        hit = _u32(r, 8) == qty
        ok += hit
        print(f"    {'O' if hit else 'X'} {wall(mono)} row{k} {item!r} 아이템 id {_u32(r, 4)} "
              f"수량@8={_u32(r, 8)}{'' if hit else ' (라벨 ' + str(qty) + ')'} "
              f"가격@16={_u32(r, 16):,} 판매자@24={name(seller_of(r))!r}")
    return ok, n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="육의전 목록(0x321f) 오프라인 판독 — 라이브 재생 vs 프로브 대조")
    ap.add_argument("--root", required=True, help="패킷 루트(<root>/<디바이스>/packet_discovery/)")
    ap.add_argument("--window", default="", help="창 파일명 부분 문자열(없으면 0x321f 가 있는 전 창)")
    ap.add_argument("--slot", type=int, default=0, help="클라 번호(1부터)만 보기")
    ap.add_argument("--rows", action="store_true", help="프레임의 행을 전부 찍는다")
    ap.add_argument("--raw-seller", action="store_true",
                    help="판매자명을 가리지 않는다(로컬 진단 전용 — 출력을 레포·문서에 붙이지 않는다)")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    root = Path(a.root).expanduser()
    if not root.is_dir():
        print(f"루트 없음: {root}")
        return 2
    name = (lambda s: s) if a.raw_seller else pm.mask_seller
    windows = pages = rows_total = mismatched = 0
    anchor_ok = anchor_n = 0
    for path in iter_windows(root):
        if a.window and a.window not in path.name:
            continue
        rec = _load(path)
        probe = probe_frames(rec)
        live, rejected, dropped = live_frames(rec)
        if a.slot:
            probe = [f for f in probe if f[1] == a.slot - 1]
            live = [f for f in live if f[1] == a.slot - 1]
        if not probe and not live:
            continue
        windows += 1
        pages += len(live or probe)
        rows_total += sum(gp.market_row_count(b) for _t, _s, b in (live or probe))
        wall = _wall_clock(rec)
        same = [(s, b) for _t, s, b in live] == [(s, b) for _t, s, b in probe]
        mismatched += not same
        mark = "" if same else "  ⚠ 불일치"
        print(f"\n=== {path.parent.parent.name}/{path.name} ===")
        print(f"  라이브 재생 {len(live)} · 프로브 {len(probe)}"
              f" · 거부 {rejected} · 유실 {dropped}{mark}")
        _print_pages(live or probe, wall, name, a.rows)
        ok, n = _check_anchors(live or probe, anchors_of(rec), wall, name)
        anchor_ok, anchor_n = anchor_ok + ok, anchor_n + n
    print(f"\n창 {windows}개 · 페이지 {pages}건 · 행 {rows_total}건 · 라벨 대조 {anchor_ok}/{anchor_n} 일치"
          + (f" · ⚠ 대조 불일치 창 {mismatched}개" if mismatched else ""))
    if not pages:
        return 2
    return 1 if mismatched else 0


if __name__ == "__main__":
    sys.exit(main())
