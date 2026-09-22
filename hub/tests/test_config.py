"""hub 설정 — secret 규칙 · 상대 db_path 는 config 폴더 기준 · HUB_DB_PATH 환경변수 우선."""
from __future__ import annotations

import json
import os

import pytest

import server as server_mod

GOOD_SECRET = "s3cret-long-enough-0123"


def _write(tmp_path, **over):
    cfg = {"secret": GOOD_SECRET, "db_path": "data/hub.db"}
    cfg.update(over)
    p = tmp_path / "conf" / "config.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def test_relative_db_path_resolves_against_config_dir_not_cwd(tmp_path, monkeypatch):
    p = _write(tmp_path)
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cfg = server_mod.load_config(str(p), env={})
    assert cfg["db_path"] == os.path.normpath(str(tmp_path / "conf" / "data" / "hub.db"))
    assert not (elsewhere / "data").exists()
    cfg = server_mod.load_config(str(_write(tmp_path / "abs", db_path=str(tmp_path / "x.db"))), env={})
    assert cfg["db_path"] == str(tmp_path / "x.db")
    assert server_mod.load_config(str(_write(tmp_path / "mem", db_path=":memory:")), env={})["db_path"] == ":memory:"


def test_env_db_path_overrides_config(tmp_path):
    p = _write(tmp_path, db_path="ignored.db")
    cfg = server_mod.load_config(str(p), env={server_mod.ENV_DB_PATH: "/data/hub.db"})
    assert cfg["db_path"] == "/data/hub.db"   # 컨테이너: Dockerfile ENV 가 config 를 이긴다, 루트 경로는 그대로
    cfg = server_mod.load_config(str(p), env={server_mod.ENV_DB_PATH: "rel/x.db"})
    assert cfg["db_path"] == os.path.normpath(str(tmp_path / "conf" / "rel" / "x.db"))   # 상대면 config 폴더 기준
    cfg = server_mod.load_config(str(p), env={server_mod.ENV_DB_PATH: ""})
    assert cfg["db_path"].endswith("ignored.db")                 # 빈 값은 미설정


@pytest.mark.parametrize("secret, hint", [
    (123, "문자열"), ("", "문자열"), ("CHANGE-ME", "예시값"), ("short", "16자"),
])
def test_bad_secrets_refuse_to_start(tmp_path, secret, hint):
    p = _write(tmp_path, secret=secret)
    with pytest.raises(SystemExit) as ei:
        server_mod.load_config(str(p), env={})
    assert hint in str(ei.value)


def test_defaults_filled_and_unknown_keys_kept(tmp_path):
    cfg = server_mod.load_config(str(_write(tmp_path, port=1234, extra="x")), env={})
    assert (cfg["port"], cfg["extra"], cfg["max_agent_ts_ahead_sec"]) == (1234, "x", 86400)
    assert cfg["listings_limit_max"] == server_mod.DEFAULTS["listings_limit_max"]
