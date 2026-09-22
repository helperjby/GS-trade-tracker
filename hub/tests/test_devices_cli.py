"""devices.py CLI — list/revoke/unrevoke/note · --stale 일괄 취소 · 이미 취소/활성 안내 · 미지 기기 exit 1 · 없는 DB 는
만들지 않음 · 경로 해석 · 토큰 해시 미출력 · 전각 폭 정렬 · 서버가 연 DB 에 대한 취소가 즉시 먹는다(별도 연결·WAL)."""
from __future__ import annotations

import asyncio
import io
import json
import os

import pytest

import db as db_mod
import devices as cli
from helpers import body, device_auth, make_cfg, obs, post, register, start_client

T0 = 1_700_000_000.0
DAY = 86400.0


def _seed(path):
    d = db_mod.Database(path)
    a = d.create_device("PC-A", "ab" * 32, T0, "203.0.113.7")
    b = d.create_device("PC-B", "cd" * 32, T0 + 100, None)
    d.close()
    return a, b


def _list(path):
    d = db_mod.Database(path)
    try:
        return {x["device_id"]: x for x in d.list_devices()}
    finally:
        d.close()


def test_cli_list_revoke_unrevoke_note_roundtrip(tmp_path):
    path = str(tmp_path / "hub.db")
    a, b = _seed(path)
    out = io.StringIO()
    assert cli.main(["--db", path, "list"], out=out) == 0
    text = out.getvalue()
    assert a in text and b in text and "PC-A" in text and "PC-B" in text
    assert "ab" * 32 not in text and "cd" * 32 not in text
    assert "2대 (활성 2 / 취소 0)" in text
    assert text.splitlines()[0].split() == list(cli._COLUMNS)

    out = io.StringIO()
    assert cli.main(["--db", path, "revoke", a, "--note", "spam\x1b[0m"], out=out, clock=lambda: T0 + 500) == 0
    assert "취소됨" in out.getvalue()
    da = _list(path)[a]
    assert da["revoked_ts"] == T0 + 500 and da["note"] == "spam?[0m"    # 제어 문자는 ? 로

    out = io.StringIO()   # 두 번째 취소 — 원래 시각 보존, 메모만 갱신
    assert cli.main(["--db", path, "revoke", a, "--note", "again"], out=out, clock=lambda: T0 + 900) == 0
    assert "이미 취소됨" in out.getvalue()
    da = _list(path)[a]
    assert da["revoked_ts"] == T0 + 500 and da["note"] == "again"

    out = io.StringIO()
    assert cli.main(["--db", path, "note", b, "친구 PC"], out=out) == 0
    assert _list(path)[b]["note"] == "친구 PC"

    out = io.StringIO()
    assert cli.main(["--db", path, "list"], out=out) == 0
    assert "2대 (활성 1 / 취소 1)" in out.getvalue() and "again" in out.getvalue()

    out = io.StringIO()
    assert cli.main(["--db", path, "unrevoke", b], out=out) == 0          # 활성인 기기 — 아무것도 안 바꾸고 안내
    assert "이미 활성" in out.getvalue()
    out = io.StringIO()
    assert cli.main(["--db", path, "unrevoke", a], out=out) == 0
    assert "복구됨" in out.getvalue()
    assert _list(path)[a]["revoked_ts"] is None and _list(path)[a]["note"] == "again"   # 메모는 남는다


def test_cli_revoke_stale_bulk(tmp_path):
    path = str(tmp_path / "hub.db")
    d = db_mod.Database(path)
    now = T0 + 60 * DAY
    fresh = d.create_device("fresh", "h1", now - 10 * DAY, None)
    d.touch_device(fresh, now - 1 * DAY)
    never = d.create_device("never", "h2", now - 40 * DAY, None)              # 등록만, 40일
    idle = d.create_device("idle", "h3", now - 50 * DAY, None)
    d.touch_device(idle, now - 31 * DAY)                                       # 31일 전 마지막
    noted = d.create_device("noted", "h4", now - 50 * DAY, None)
    d.set_device_note(noted, "keep")
    already = d.create_device("already", "h5", now - 50 * DAY, None)
    d.set_device_revoked(already, now - 5 * DAY)
    d.close()
    out = io.StringIO()
    assert cli.main(["--db", path, "revoke", "--stale", "30"], out=out, clock=lambda: now) == 0
    assert "일괄 취소: 3대" in out.getvalue()
    rows = _list(path)
    assert rows[fresh]["revoked_ts"] is None
    assert rows[never]["revoked_ts"] == now and rows[never]["note"] == "stale>30d"
    assert rows[idle]["revoked_ts"] == now
    assert rows[noted]["revoked_ts"] == now and rows[noted]["note"] == "keep"      # 있는 메모는 덮지 않는다
    assert rows[already]["revoked_ts"] == now - 5 * DAY                            # 이미 취소된 행은 그대로
    # 인자 검사: device_id 와 --stale 은 하나만, --stale 은 1 이상
    for argv in (["revoke"], ["revoke", fresh, "--stale", "3"], ["revoke", "--stale", "0"]):
        with pytest.raises(SystemExit) as ei:
            cli.main(["--db", path, *argv], out=io.StringIO())
        assert ei.value.code == 2


def test_cli_table_aligns_east_asian_width(tmp_path):
    path = str(tmp_path / "hub.db")
    d = db_mod.Database(path)
    d.create_device("PC-A", "h1", T0, None)
    d.create_device("데스크탑", "h2", T0 + 1, None)
    d.set_device_note(d.create_device("x", "h3", T0 + 2, None), "친구 PC 메모")
    d.close()
    assert cli.display_width("데스크탑") == 8 and cli.display_width("PC-A") == 4
    out = io.StringIO()
    assert cli.main(["--db", path, "list"], out=out) == 0
    lines = out.getvalue().splitlines()[:4]
    created = cli._fmt_ts(T0)[:7]                       # "YYYY-MM" — created 열이 시작하는 자리
    starts = {cli.display_width(line[:line.index(created if i else "created")]) for i, line in enumerate(lines)}
    assert len(starts) == 1, lines                      # 헤더·행 전부 같은 표시 폭에서 created 열 시작


def test_cli_unknown_device_exit_1_and_missing_db_not_created(tmp_path, capsys):
    path = str(tmp_path / "hub.db")
    _seed(path)
    for argv in (["revoke", "d-0000000000"], ["unrevoke", "d-0000000000"], ["note", "d-0000000000", "x"]):
        assert cli.main(["--db", path, *argv], out=io.StringIO()) == 1
    assert "미지 기기" in capsys.readouterr().err
    missing = tmp_path / "typo" / "nope.db"
    assert cli.main(["--db", str(missing), "list"], out=io.StringIO()) == 1
    assert not missing.exists() and not missing.parent.exists()
    assert "DB 파일이 없습니다" in capsys.readouterr().err


def test_cli_db_path_resolution(tmp_path, monkeypatch):
    assert cli.resolve_db_path("x.db", env={"HUB_DB_PATH": "y.db"}) == "x.db"           # --db 가 최우선
    assert cli.resolve_db_path(None, env={"HUB_DB_PATH": "y.db"}) == "y.db"             # 컨테이너 ENV
    assert cli.resolve_db_path("", env={"HUB_DB_PATH": "y.db"}) == "y.db"
    # config.json 폴백 — server.load_config 규칙(상대 경로 = config 폴더 기준)
    monkeypatch.setattr(cli, "_HUB_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"secret": "s3cret-long-enough-0123", "db_path": "data/hub.db"}),
                                          encoding="utf-8")
    assert cli.resolve_db_path(None, env={}) == os.path.normpath(str(tmp_path / "data" / "hub.db"))


def test_cli_revoke_takes_effect_on_running_server(tmp_path):
    """서버 연결이 열린 채 CLI(별도 sqlite 연결)가 revoke — 다음 업로드부터 403, unrevoke 로 복구."""
    async def scn():
        cfg = make_cfg(tmp_path)
        app, client = await start_client(cfg)
        try:
            st, reg = await register(client, label="PC-1")
            dev_id, tok = reg["device_id"], reg["token"]
            assert (await post(client, body(obs(obs_id="c1"), device_id=dev_id), headers=device_auth(tok)))[0] == 200
            assert cli.main(["--db", cfg["db_path"], "revoke", dev_id, "--note", "테스트"], out=io.StringIO()) == 0
            st, data = await post(client, body(obs(obs_id="c2"), device_id=dev_id), headers=device_auth(tok))
            assert (st, data["error"]) == (403, "device_revoked")
            assert cli.main(["--db", cfg["db_path"], "unrevoke", dev_id], out=io.StringIO()) == 0
            st, data = await post(client, body(obs(obs_id="c2"), device_id=dev_id), headers=device_auth(tok))
            assert st == 200
            out = io.StringIO()
            assert cli.main(["--db", cfg["db_path"], "list"], out=out) == 0
            assert "PC-1" in out.getvalue() and "테스트" in out.getvalue() and tok not in out.getvalue()
        finally:
            await client.close()
    asyncio.run(scn())
