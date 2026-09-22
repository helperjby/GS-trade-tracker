"""첫 실행 등록 — 초대 코드 하나로 허브에 기기를 등록한다(`docs/HUB-PROTOCOL.md` §3-0).

관측기는 **Npcap 만 있는 일반 사용자 PC** 에서 돈다. 그래서 자격은 관리 시크릿이 아니라
**기기 토큰**이고, 그 토큰은 이 흐름에서 한 번만 받는다:

    hub_token 없음 → --invite-code 또는 콘솔 프롬프트 → POST /api/market/register
                   → hub_device_id·hub_token 저장(초대 코드는 저장하지 않는다)

등록에 실패해도 **관측은 계속한다** — 업로더만 뜨지 않는다. 사유는 한 줄로 보이고, 자동 재등록은
하지 않는다(정원·명단은 관리자 소관이다). 등록을 건너뛰어도(빈 입력) 마찬가지다.
"""
from __future__ import annotations

import platform
from pathlib import Path
from typing import Callable, Optional, TextIO

from . import app_config, hub_client

PROMPT = "초대 코드를 입력하세요(엔터 = 업로드 없이 관측만): "


def device_label(explicit: str = "") -> str:
    """허브 목록에 보일 이름. 기본은 이 PC 의 호스트명 — 관리자가 누구 기기인지 알아야 한다.
    (허브 DB 로만 가고 레포·문서에는 남지 않는다. 싫으면 `--device-label` 로 바꾼다.)"""
    label = (explicit or "").strip()
    if not label:
        try:
            label = platform.node().strip()
        except Exception:
            label = ""
    label = "".join(ch for ch in label if ch.isprintable())
    return label[:64]


def prompt_invite_code(stdin: Optional[TextIO], say: Callable[[str], None]) -> str:
    """한 줄 읽는다. 파이프·리다이렉트(EOF)면 빈 문자열 — 무인 실행이 여기서 멎지 않는다."""
    if stdin is None:
        return ""
    say("[허브] 이 PC 는 아직 등록되지 않았습니다.")
    try:
        print(PROMPT, end="", flush=True)
        line = stdin.readline()
    except (OSError, ValueError):
        return ""
    return (line or "").strip()


def ensure_registered(cfg: app_config.Config, hub_url: str, *,
                      invite_code: str = "",
                      label: str = "",
                      say: Callable[[str], None],
                      stdin: Optional[TextIO] = None,
                      register: Callable[..., hub_client.Response] = hub_client.register,
                      config_path: Optional[Path] = None) -> bool:
    """토큰이 있으면 그대로 True. 없으면 등록을 시도하고 성공했을 때만 True."""
    if cfg.hub_token:
        return True
    code = (invite_code or "").strip() or prompt_invite_code(stdin, say)
    if not code:
        say("[허브] 초대 코드가 없어 업로드 없이 관측만 합니다 — 나중에 --invite-code 로 등록하세요.")
        return False
    resp = register(hub_url, code, device_label(label))
    if not resp.ok:
        say("[허브] " + hub_client.describe_register(resp))
        return False
    cfg.hub_device_id = str(resp.body.get("device_id") or "")
    cfg.hub_token = str(resp.body.get("token") or "")
    cfg.hub_url = hub_url
    if not cfg.hub_device_id or not cfg.hub_token:
        say("[허브] 등록 응답에 기기 id·토큰이 없습니다 — 업로드 없이 관측만 합니다.")
        cfg.hub_device_id = cfg.hub_token = ""
        return False
    try:
        app_config.save(cfg, config_path)
    except OSError as e:
        say(f"[허브] 등록은 됐지만 설정을 저장하지 못했습니다 — 다음 실행에 다시 등록해야 합니다: {e}")
    say("[허브] " + hub_client.describe_register(resp))
    return True
