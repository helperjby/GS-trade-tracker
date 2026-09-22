"""관측기 설정 — `%APPDATA%\\YukTracker\\config.json`.

키는 다섯이다: `hub_url` · `hub_device_id` · `hub_token` · `local_id` · `client_dir`.
**초대 코드는 저장하지 않는다**(1회용) 그리고 **허브 관리 시크릿은 여기에도 exe 에도 없다**
(관측기는 기기 토큰만 쓴다 — `docs/HUB-PROTOCOL.md` §0).

읽기는 절대 던지지 않는다 — 손상된 파일은 빈 설정으로 보고 경고만 남긴다(업로드가 막힐 뿐
관측은 계속돼야 한다). 쓰기는 `item_names.ItemTable.write_json` 과 같은 mkstemp + `os.replace`
라서 관측기 두 개가 겹쳐도 반쪽 파일이 남지 않는다. 모르는 키는 그대로 보존해 되쓴다.

허브 주소 우선순위(`resolve_hub_url`): CLI `--hub-url` > config `hub_url` > 환경변수
`%YUKTRACKER_HUB_URL%` > 빌드 시 주입(`_build_config.HUB_URL`) > 없음(업로드 비활성).
빌드 주입 모듈은 `.gitignore` 대상이다 — 공개 레포 소스에 tailnet 주소를 두지 않는다.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import paths
from .seassist.logger import get_logger

try:  # 빌드 시 tools/gen_build_config.py 가 만든다(레포에는 없다).
    from ._build_config import HUB_URL as BUILD_HUB_URL
except ImportError:  # pragma: no cover - 소스 실행의 정상 경로
    BUILD_HUB_URL = ""

ENV_HUB_URL = "YUKTRACKER_HUB_URL"
#: 저장하는 키 — 이 목록 밖의 키는 `extra` 로 보존만 한다.
KEYS = ("hub_url", "hub_device_id", "hub_token", "local_id", "client_dir")


@dataclass
class Config:
    hub_url: str = ""
    hub_device_id: str = ""
    hub_token: str = ""
    #: 이 설치본의 고유 id — `obs_id` 접두사. 허브가 발급하는 `hub_device_id` 와 **별개**라
    #: 재등록해도 옛 스풀의 `obs_id` 가 흔들리지 않는다.
    local_id: str = ""
    client_dir: str = ""
    extra: dict = field(default_factory=dict)

    def to_json_dict(self) -> dict:
        out = dict(self.extra)
        out.update({k: getattr(self, k) for k in KEYS})
        return out


def load(path: Optional[Path] = None) -> Config:
    """설정 1개. 파일이 없거나 깨졌으면 빈 설정(경고 1줄)."""
    p = Path(path) if path is not None else paths.config_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config()
    except (OSError, ValueError) as e:
        get_logger().warning("설정을 읽지 못했습니다(%s) — 빈 설정으로 계속합니다: %s", p, e)
        return Config()
    if not isinstance(raw, dict):
        get_logger().warning("설정 모양이 dict 가 아닙니다(%s) — 빈 설정으로 계속합니다", p)
        return Config()
    vals = {k: str(raw.get(k) or "").strip() for k in KEYS}
    extra = {k: v for k, v in raw.items() if k not in KEYS}
    return Config(**vals, extra=extra)


def save(cfg: Config, path: Optional[Path] = None) -> None:
    """고유 이름 tmp → `os.replace`. 폴더는 여기서 만든다(`paths` 는 순수)."""
    p = Path(path) if path is not None else paths.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(cfg.to_json_dict(), ensure_ascii=False, indent=1) + "\n")
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_local_id(cfg: Config, path: Optional[Path] = None) -> str:
    """없으면 만들어 **저장까지** 한다 — 재시작해도 같은 값이어야 `obs_id` 가 안정적이다.
    저장에 실패해도 이번 실행분은 쓴다(업로드가 멈추는 것보다 낫다)."""
    if cfg.local_id:
        return cfg.local_id
    cfg.local_id = os.urandom(4).hex()
    try:
        save(cfg, path)
    except OSError as e:
        get_logger().warning("설정을 저장하지 못했습니다 — 이번 실행에만 유효한 기기 id 를 씁니다: %s", e)
    return cfg.local_id


def resolve_hub_url(cli_url: str = "", cfg: Optional[Config] = None,
                    env: Optional[dict] = None) -> str:
    """CLI > config > 환경변수 > 빌드 주입. 빈 문자열이면 업로드를 켜지 않는다."""
    environ = os.environ if env is None else env
    for candidate in (cli_url, (cfg.hub_url if cfg is not None else ""),
                      environ.get(ENV_HUB_URL, ""), BUILD_HUB_URL):
        value = str(candidate or "").strip().rstrip("/")
        if value:
            return value
    return ""
