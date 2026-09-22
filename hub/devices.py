"""허브 기기 관리 CLI — 초대 코드로 자기등록한 관측기 기기의 목록·취소·복구·메모. 서버와 같은 SQLite 파일에 직접 쓴다.

    python devices.py [--db PATH] list
    python devices.py [--db PATH] revoke <device_id> [--note TEXT]
    python devices.py [--db PATH] revoke --stale <days> [--note TEXT]     # N일 넘게 안 올린 기기 일괄 취소
    python devices.py [--db PATH] unrevoke <device_id>
    python devices.py [--db PATH] note <device_id> <text>

DB 경로: ``--db`` > 환경변수 ``HUB_DB_PATH``(컨테이너 이미지가 ``/data/hub.db`` 로 둔다) > ``<hub>/config.json`` 의
``db_path``(server.load_config 규칙). 컨테이너에서는 ``docker compose exec yuktracker-hub python devices.py list`` —
시크릿은 필요 없고, 없는 DB 파일은 만들지 않는다(경로 오타가 빈 DB 를 남기지 않게).

서버는 요청마다 토큰을 DB 에서 찾으므로 취소는 다음 업로드부터 바로 403 ``device_revoked``. 쓰기는 한 행 UPDATE 라
WAL + busy timeout(10s) 아래서 서버의 업로드 트랜잭션과 안전하게 교차한다(HUB-PROTOCOL §3-6). 취소는 **soft** —
같은 초대 코드로 재등록하면 새 기기가 된다; 남용은 ``config.json`` 의 ``invite_code`` 교체 + 재기동.
이미 취소된 기기를 다시 취소해도 원래 ``revoked_ts`` 는 덮지 않는다(감사 시각 보존). 토큰 해시는 출력하지 않는다.
label·note 는 터미널에 그대로 찍히므로 제어 문자는 ``?`` 로 바꾸고, 한글 등 전각 문자는 표시 폭 2 로 맞춰 정렬한다.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import unicodedata

import db as db_mod

_HUB_DIR = os.path.dirname(os.path.abspath(__file__))
_COLUMNS = ("device_id", "label", "created", "last_seen", "uploads", "revoked", "note")
DB_TIMEOUT_SEC = 10.0


def resolve_db_path(explicit: str | None, env=None) -> str:
    env = os.environ if env is None else env
    if explicit:
        return explicit
    if env.get("HUB_DB_PATH"):
        return env["HUB_DB_PATH"]
    import server  # 늦은 import — aiohttp 는 config 폴백에서만 필요
    return server.load_config(os.path.join(_HUB_DIR, "config.json"), env=env)["db_path"]


def _clean(text) -> str:
    return "".join(ch if ch.isprintable() else "?" for ch in str(text or ""))


def display_width(text: str) -> int:
    """터미널 표시 폭 — 전각(W)·Fullwidth(F) 는 2, 나머지 1(한글 label·note 가 열을 밀지 않게)."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def _fmt_ts(ts) -> str:
    if ts is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def format_table(devices: list) -> str:
    rows = [[d["device_id"], _clean(d["label"]), _fmt_ts(d["created_ts"]), _fmt_ts(d["last_seen_ts"]),
             str(d["upload_count"]), _fmt_ts(d["revoked_ts"]), _clean(d["note"])] for d in devices]
    widths = [max([display_width(col)] + [display_width(r[i]) for r in rows]) for i, col in enumerate(_COLUMNS)]
    lines = ["  ".join(_pad(col, w) for col, w in zip(_COLUMNS, widths)).rstrip()]
    lines += ["  ".join(_pad(v, w) for v, w in zip(r, widths)).rstrip() for r in rows]
    return "\n".join(lines)


def cmd_list(db: db_mod.Database, out) -> int:
    devices = db.list_devices()
    print(format_table(devices), file=out)
    revoked = sum(1 for d in devices if d["revoked_ts"] is not None)
    print(f"{len(devices)}대 (활성 {len(devices) - revoked} / 취소 {revoked})", file=out)
    return 0


def cmd_revoke(db: db_mod.Database, device_id: str, note, now: float, out) -> int:
    dev = db.get_device(device_id)
    if dev is None:
        print(f"미지 기기: {device_id}", file=sys.stderr)
        return 1
    if dev["revoked_ts"] is not None:
        print(f"이미 취소됨: {device_id} ({_fmt_ts(dev['revoked_ts'])}) — 시각은 그대로 둔다", file=out)
    else:
        db.set_device_revoked(device_id, now)
        print(f"취소됨: {device_id} — 다음 업로드부터 403 device_revoked", file=out)
    if note is not None:
        db.set_device_note(device_id, _clean(note))
    return 0


def cmd_revoke_stale(db: db_mod.Database, days: int, note, now: float, out) -> int:
    n = db.revoke_stale_devices(now, days * 86400, _clean(note) if note is not None else f"stale>{days}d")
    print(f"일괄 취소: {n}대 ({days}일 넘게 업로드 없음)", file=out)
    return 0


def cmd_unrevoke(db: db_mod.Database, device_id: str, out) -> int:
    dev = db.get_device(device_id)
    if dev is None:
        print(f"미지 기기: {device_id}", file=sys.stderr)
        return 1
    if dev["revoked_ts"] is None:
        print(f"이미 활성: {device_id}", file=out)
        return 0
    db.set_device_revoked(device_id, None)
    print(f"복구됨: {device_id}", file=out)
    return 0


def cmd_note(db: db_mod.Database, device_id: str, note: str, out) -> int:
    if not db.set_device_note(device_id, _clean(note)):
        print(f"미지 기기: {device_id}", file=sys.stderr)
        return 1
    print(f"메모 저장: {device_id}", file=out)
    return 0


def main(argv=None, out=None, env=None, clock=time.time) -> int:
    out = sys.stdout if out is None else out
    parser = argparse.ArgumentParser(description="육의전 시세 허브 — 기기 관리")
    parser.add_argument("--db", help="SQLite 경로(기본: HUB_DB_PATH → config.json 의 db_path)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="기기 목록")
    p = sub.add_parser("revoke", help="기기 취소(soft) — 다음 업로드부터 403")
    p.add_argument("device_id", nargs="?")
    p.add_argument("--stale", type=int, metavar="DAYS", help="N일 넘게 업로드 없는 기기 전부 취소(device_id 대신)")
    p.add_argument("--note", help="사유 메모")
    p = sub.add_parser("unrevoke", help="취소 해제")
    p.add_argument("device_id")
    p = sub.add_parser("note", help="메모 저장")
    p.add_argument("device_id")
    p.add_argument("text")
    args = parser.parse_args(argv)
    if args.cmd == "revoke":
        if (args.device_id is None) == (args.stale is None):
            parser.error("revoke 는 <device_id> 또는 --stale DAYS 중 하나")
        if args.stale is not None and args.stale < 1:
            parser.error("--stale 은 1 이상")

    path = resolve_db_path(args.db, env)
    if path != ":memory:" and not os.path.exists(path):
        print(f"DB 파일이 없습니다: {path}", file=sys.stderr)
        return 1
    db = db_mod.Database(path, timeout=DB_TIMEOUT_SEC)
    try:
        if args.cmd == "list":
            return cmd_list(db, out)
        if args.cmd == "revoke":
            if args.stale is not None:
                return cmd_revoke_stale(db, args.stale, args.note, clock(), out)
            return cmd_revoke(db, args.device_id, args.note, clock(), out)
        if args.cmd == "unrevoke":
            return cmd_unrevoke(db, args.device_id, out)
        return cmd_note(db, args.device_id, args.text, out)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
