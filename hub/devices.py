"""허브 기기 관리 CLI — 초대 코드로 자기등록한 관측기 기기의 목록·제거·복구·별칭·메모. 서버와 같은 SQLite 파일에 직접 쓴다.

    python devices.py [--db PATH] list
    python devices.py [--db PATH] revoke <device_id> [--note TEXT]        # 명단에서 제거 — 정원 자리가 바로 빈다
    python devices.py [--db PATH] unrevoke <device_id>
    python devices.py [--db PATH] alias <device_id> <text>                # 관리자 별칭(빈 문자열이면 해제)
    python devices.py [--db PATH] note <device_id> <text>

DB 경로: ``--db`` > 환경변수 ``HUB_DB_PATH``(컨테이너 이미지가 ``/data/hub.db`` 로 둔다) > ``<hub>/config.json`` 의
``db_path``(server.load_config 규칙). 컨테이너에서는 ``docker compose exec yuktracker-hub python devices.py list`` —
시크릿은 필요 없고, 없는 DB 파일은 만들지 않는다(경로 오타가 빈 DB 를 남기지 않게).

업로드가 없다고 기기가 저절로 빠지는 기능은 두지 않는다(2026-09-22 사용자 결정: 아는 사람 최대 7명에게 직접 배포).
정원은 설정 ``invite_codes`` 의 슬롯 수이고 ``slot`` 열이 그 기기가 어느 코드로 들어왔는지다(초기 별칭 = 슬롯 이름).
같은 슬롯 코드로 다시 등록하면 서버가 옛 기기를 자동 제거한다(note ``재등록 교체 → <새 id>``, 2026-09-23) — 관리자의
``revoke`` 는 그 밖의 경우(분실·남용)를 위한 것이다.

서버는 요청마다 토큰을 DB 에서 찾으므로 제거는 다음 업로드부터 바로 403 ``device_revoked``. 쓰기는 한 행 UPDATE 라
WAL + busy timeout(10s) 아래서 서버의 업로드 트랜잭션과 안전하게 교차한다(HUB-PROTOCOL §3-6). 제거는 **soft** —
행은 감사용으로 남고, 같은 슬롯 코드로 재등록하면 새 기기가 슬롯을 받는다; 남용은 ``config.json`` 에서 그 슬롯의 코드 교체 + 재기동.
이미 제거된 기기를 다시 제거해도 원래 ``revoked_ts`` 는 덮지 않는다(감사 시각 보존). 토큰 해시는 출력하지 않는다.
``alias`` 는 관리자가 붙이는 이름, ``label`` 은 기기가 등록 때 스스로 적은 값 — 표시는 별칭을 앞세운다.
alias·label·note 는 터미널에 그대로 찍히므로 제어 문자는 ``?`` 로 바꾸고, 한글 등 전각 문자는 표시 폭 2 로 맞춰 정렬한다.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import unicodedata

import db as db_mod

_HUB_DIR = os.path.dirname(os.path.abspath(__file__))
_COLUMNS = ("device_id", "slot", "alias", "label", "created", "last_seen", "uploads", "revoked", "note")
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
    rows = [[d["device_id"], _clean(d.get("slot", "")), _clean(d["alias"]), _clean(d["label"]), _fmt_ts(d["created_ts"]),
             _fmt_ts(d["last_seen_ts"]), str(d["upload_count"]), _fmt_ts(d["revoked_ts"]),
             _clean(d["note"])] for d in devices]
    widths = [max([display_width(col)] + [display_width(r[i]) for r in rows]) for i, col in enumerate(_COLUMNS)]
    lines = ["  ".join(_pad(col, w) for col, w in zip(_COLUMNS, widths)).rstrip()]
    lines += ["  ".join(_pad(v, w) for v, w in zip(r, widths)).rstrip() for r in rows]
    return "\n".join(lines)


def cmd_list(db: db_mod.Database, out) -> int:
    devices = db.list_devices()
    print(format_table(devices), file=out)
    revoked = sum(1 for d in devices if d["revoked_ts"] is not None)
    # "등록" = 정원이 세는 것과 같은 정의(미제거) — 서버의 count_active_devices 와 어긋나지 않는다.
    # 정원 자체는 config 의 invite_codes 슬롯 수 / stats 의 devices_max 에 있다(여기서 config 를 읽지 않는다).
    print(f"{len(devices)}행 (등록 {len(devices) - revoked} / 제거 {revoked})", file=out)
    return 0


def cmd_revoke(db: db_mod.Database, device_id: str, note, now: float, out) -> int:
    dev = db.get_device(device_id)
    if dev is None:
        print(f"미지 기기: {device_id}", file=sys.stderr)
        return 1
    if dev["revoked_ts"] is not None:
        print(f"이미 제거됨: {device_id} ({_fmt_ts(dev['revoked_ts'])}) — 시각은 그대로 둔다", file=out)
    else:
        db.set_device_revoked(device_id, now)
        print(f"제거됨: {device_id} — 다음 업로드부터 403 device_revoked", file=out)
    if note is not None:
        db.set_device_note(device_id, _clean(note))
    return 0


def cmd_alias(db: db_mod.Database, device_id: str, alias: str, out) -> int:
    """관리자 별칭 — 빈 문자열이면 해제. 기기가 스스로 적은 label 은 건드리지 않는다."""
    alias = _clean(alias).strip()
    if not db.set_device_alias(device_id, alias):
        print(f"미지 기기: {device_id}", file=sys.stderr)
        return 1
    print(f"별칭 {'해제' if not alias else '저장'}: {device_id}{'' if not alias else ' → ' + alias}", file=out)
    return 0


def cmd_unrevoke(db: db_mod.Database, device_id: str, out) -> int:
    dev = db.get_device(device_id)
    if dev is None:
        print(f"미지 기기: {device_id}", file=sys.stderr)
        return 1
    if dev["revoked_ts"] is None:
        print(f"이미 등록 상태: {device_id}", file=out)
        return 0
    db.set_device_revoked(device_id, None)
    print(f"복구됨: {device_id} — 정원 자리 1개를 다시 차지한다", file=out)
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
    p = sub.add_parser("revoke", help="명단에서 제거(soft) — 다음 업로드부터 403, 정원 자리 반환")
    p.add_argument("device_id")
    p.add_argument("--note", help="사유 메모")
    p = sub.add_parser("unrevoke", help="제거 취소")
    p.add_argument("device_id")
    p = sub.add_parser("alias", help="관리자 별칭 지정(빈 문자열이면 해제)")
    p.add_argument("device_id")
    p.add_argument("text")
    p = sub.add_parser("note", help="메모 저장")
    p.add_argument("device_id")
    p.add_argument("text")
    args = parser.parse_args(argv)

    path = resolve_db_path(args.db, env)
    if path != ":memory:" and not os.path.exists(path):
        print(f"DB 파일이 없습니다: {path}", file=sys.stderr)
        return 1
    db = db_mod.Database(path, timeout=DB_TIMEOUT_SEC)
    try:
        if args.cmd == "list":
            return cmd_list(db, out)
        if args.cmd == "revoke":
            return cmd_revoke(db, args.device_id, args.note, clock(), out)
        if args.cmd == "unrevoke":
            return cmd_unrevoke(db, args.device_id, out)
        if args.cmd == "alias":
            return cmd_alias(db, args.device_id, args.text, out)
        return cmd_note(db, args.device_id, args.text, out)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
