"""hub 설정 — secret·invite_code 규칙 · 상대 db_path 는 config 폴더 기준 · HUB_DB_PATH 환경변수 우선 · 새 키 검증."""
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


@pytest.mark.parametrize("invite, hint", [
    (123, "문자열"), ("short", "8자"), ("CHANGE-ME-INVITE", "예시값"), ("CHANGE-ME", "예시값"),
    (GOOD_SECRET, "secret"),   # 초대 코드는 반공개 — 관리 시크릿을 그대로 쓰면 시크릿이 새는 것
])
def test_bad_invite_codes_refuse_to_start(tmp_path, invite, hint):
    p = _write(tmp_path, invite_code=invite)
    with pytest.raises(SystemExit) as ei:
        server_mod.load_config(str(p), env={})
    assert hint in str(ei.value)


def test_invite_code_empty_means_closed_and_new_defaults(tmp_path):
    cfg = server_mod.load_config(str(_write(tmp_path)), env={})
    assert cfg["invite_code"] == ""                              # 키 없음 = 등록 닫힘
    assert server_mod.load_config(str(_write(tmp_path / "n", invite_code=None)), env={})["invite_code"] == ""
    assert (cfg["max_devices"], cfg["admin_public"], cfg["proxy_header"]) == (500, False, "Tailscale-Funnel-Request")
    assert (cfg["register_limit_per_hour"], cfg["upload_limit_per_min"], cfg["auth_fail_limit_per_min"]) == (10, 120, 30)
    cfg = server_mod.load_config(str(_write(tmp_path / "ok", invite_code="invite-code-1", proxy_header=" CF-Connecting-IP ",
                                           admin_public=True, max_devices=3)), env={})
    assert (cfg["invite_code"], cfg["proxy_header"], cfg["admin_public"], cfg["max_devices"]) == (
        "invite-code-1", "CF-Connecting-IP", True, 3)


@pytest.mark.parametrize("key, value", [
    ("max_devices", 0), ("max_devices", True), ("max_devices", "5"),
    ("register_limit_per_hour", "10"), ("upload_limit_per_min", 0), ("auth_fail_limit_per_min", -1),
    ("admin_public", "no"), ("admin_public", 1), ("proxy_header", ""), ("proxy_header", "   "), ("proxy_header", 5),
])
def test_bad_limits_and_flags_refuse_to_start(tmp_path, key, value):
    with pytest.raises(SystemExit) as ei:
        server_mod.load_config(str(_write(tmp_path, **{key: value})), env={})
    assert key in str(ei.value)


def test_defaults_filled_and_unknown_keys_kept(tmp_path):
    cfg = server_mod.load_config(str(_write(tmp_path, port=1234, extra="x")), env={})
    assert (cfg["port"], cfg["extra"], cfg["max_agent_ts_ahead_sec"]) == (1234, "x", 86400)
    assert cfg["listings_limit_max"] == server_mod.DEFAULTS["listings_limit_max"]


def test_example_config_refuses_to_start_as_is(tmp_path):
    """config.json.example 을 복사만 하면 기동 거부 — secret 이 먼저 걸린다; secret 만 고쳐도 invite 예시값이 걸린다."""
    example = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json.example")
    raw = json.loads(open(example, encoding="utf-8").read())
    with pytest.raises(SystemExit) as ei:
        server_mod.load_config(str(_write(tmp_path, **raw)), env={})
    assert "secret" in str(ei.value)
    raw["secret"] = GOOD_SECRET
    with pytest.raises(SystemExit) as ei:
        server_mod.load_config(str(_write(tmp_path / "b", **raw)), env={})
    assert "invite_code" in str(ei.value)
