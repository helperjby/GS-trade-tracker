"""육의전 시세 허브 서버 — aiohttp 단독 + stdlib sqlite3 (관측기 ``src/`` 무결합, 독립 배포).

라우팅: ``GET /``(무인증 상태 줄) · ``POST /api/market/register``(초대 코드 → 기기 토큰, 무 Bearer) ·
``POST /api/market/observations``(관측기 업로드 — 기기 토큰, 직접 접속이면 관리 시크릿도) ·
``GET /api/market/search`` · ``GET /api/market/listings`` · ``GET /api/market/stats``(관리 시크릿, **직접 접속 전용**).
계약 정본은 docs/HUB-PROTOCOL.md.

리스너 둘(``serve``): **관리 리스너** ``port``(8800) 는 전 라우트, Pi 안(봇)·LAN·tailnet 의 직접 접속용. **공개 리스너**
``public_port``(8801) 는 Tailscale Funnel 이 가리키는 포트 — 여기로 온 요청은 헤더와 무관하게 전부 "공개"라 관리 시크릿을
받지 않고 조회 라우트는 403 ``not_public``. 관리 리스너에서도 tailscaled 가 Funnel 요청에 붙이는 ``Tailscale-Funnel-Request``
(``proxy_header``)가 있으면 공개로 본다 — 8800 에 Funnel 을 잘못 걸어도 한 번 더 막는 2차 방어. 두 판정은 서로 독립이라
헤더 이름 오타·프록시 교체가 관리 라우트를 여는 일은 없다(공개 리스너는 헤더를 보지 않는다).
공개 요청의 클라이언트 IP 는 ``X-Forwarded-For`` 마지막 항목(프록시가 붙인 원 IP), 없으면 ``public:?`` 한 버킷(경고 1회) —
컨테이너 안 ``request.remote`` 는 docker 브리지 IP 라 봇·프록시가 같아 공개 요청의 키로 쓰지 않는다.

속도제한(``RateLimiter``, 프로세스 메모리): 등록은 IP 당 시간, 업로드는 기기 당 분(취소 여부보다 먼저 — 취소된 클라의 폭주도
기기 단위로 429), 인증 실패는 **공개 요청**의 IP 당 분 — 토큰 검사 **뒤**에만 센다(공유 NAT 뒤의 고장난 클라 하나가 같은 IP 의
정상 기기를 막지 않게; 직접 접속은 세지 않는다). 429 + ``Retry-After`` — 관측기는 5xx 처럼 기다렸다 재시도한다(스풀 유지).

tolerant reader: 미지 키는 무시(관측 봉투는 payload_json 에 보존 — 행은 미지 키만)하되, 형·범위가 틀린 필수
필드는 400 으로 거부하고 그 요청은 **아무것도 쓰지 않는다**(전건 검증 뒤 트랜잭션 1개 — 기기 토큰이면 같은 트랜잭션에
``devices.last_seen_ts/upload_count`` 도 쓴다). 정수는 SQLite 의 signed 64bit 범위 안이어야 한다 — 검증을 통과한 값이
저장에서 터지면(500) 관측기가 그 배치를 영원히 재시도한다.

설정(``load_config``): ``secret`` 는 16자 이상 문자열이고 예시값(``CHANGE-ME``)이면 기동을 거부한다. ``invite_code`` 는
공백을 벗긴 뒤 비어 있으면 등록 닫힘, 있으면 8자 이상·예시값 거부·``secret`` 와 같으면 거부(초대 코드는 커뮤니티에 공유하는
반공개 값). 상대 ``db_path`` 는 CWD 가 아니라 **config 파일 폴더** 기준(레포 루트에서 띄워도 ``<repo>/data/hub.db`` 가 생기지
않는다). 환경변수 ``HUB_DB_PATH`` 가 있으면 그것이 이긴다 — 컨테이너는 Dockerfile 이 ``/data/hub.db`` 로 고정해
운영자가 config 를 안 고쳐도 볼륨에 쓴다.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import signal
import sqlite3
import time

from aiohttp import web

import db as db_mod

log = logging.getLogger("hub")

PROTO_V = 1
SERVICE = "yuktracker-hub"

DEFAULTS = {
    "host": "0.0.0.0",
    #: 관리 리스너 — 전 라우트, 직접 접속(봇 127.0.0.1·LAN·tailnet). Funnel 을 걸지 않는다.
    "port": 8800,
    #: 공개 리스너 — Funnel 대상. 여기로 온 요청은 전부 공개(관리 시크릿 무시, 조회 403).
    "public_port": 8801,
    "secret": "",
    "db_path": "data/hub.db",
    "retention_market_days": 30,
    "search_max_age_sec": 86400,
    "search_limit_default": 20,
    "search_limit_max": 100,
    "listings_limit_default": 500,
    "listings_limit_max": 2000,
    "max_observations_per_request": 100,
    "max_rows_per_observation": 64,
    #: 관측기 시계가 서버보다 이만큼 넘게 앞서면 400 — 그 안이면 recv_ts 로 깎는다(§4).
    "max_agent_ts_ahead_sec": 86400,
    #: 초대 코드 — 빈 문자열이면 등록 닫힘(§3-0). 8자 이상, 예시값·secret 와 같은 값은 기동 거부.
    "invite_code": "",
    #: 기기 정원 — 넘으면 403 registration_full. 세는 대상은 **미제거 기기 전부**(업로드 여부 무관).
    #: 2026-09-22 사용자 결정: 아는 사람에게 직접 배포(최대 7명), 활성 여부에 따른 자동 제외는 두지 않는다 —
    #: 자리를 비우는 것은 관리자의 `devices.py revoke` 뿐이다.
    "max_devices": 7,
    #: true 면 공개 요청에서도 관리 시크릿을 받는다. 기본 false — 조회 라우트는 직접 접속 전용.
    "admin_public": False,
    #: 관리 리스너에서 이 헤더가 있으면 공개 요청(tailscaled 가 Funnel 요청에 붙인다). Cloudflare Tunnel 이면 "CF-Connecting-IP".
    "proxy_header": "Tailscale-Funnel-Request",
    "register_limit_per_hour": 10,
    "upload_limit_per_min": 120,
    "auth_fail_limit_per_min": 30,
}
#: 관측 100건 × 행 64 × 행당 ~180B ≈ 1.2MB — aiohttp 기본 1MiB 보다 넉넉하게.
CLIENT_MAX_SIZE = 4 * 1024 * 1024
#: 등록 본문은 초대 코드 + label 뿐 — 무인증 라우트라 큰 본문은 파싱 전에 자른다.
MAX_REGISTER_BODY = 4096
ENV_DB_PATH = "HUB_DB_PATH"
MIN_SECRET_LEN = 16
PLACEHOLDER_SECRETS = frozenset({"CHANGE-ME"})
MIN_INVITE_LEN = 8
PLACEHOLDER_INVITES = frozenset({"CHANGE-ME", "CHANGE-ME-INVITE"})
#: SQLite INTEGER 범위 — 밖이면 sqlite3 가 OverflowError 를 던진다.
I64_MIN, I64_MAX = -(1 << 63), (1 << 63) - 1
#: 문자열 상한 — seller 는 listing_key(PK)에 들어가고 item_name 은 학습 표·정규화 키로 복사된다.
MAX_SELLER_LEN = 128
MAX_ITEM_NAME_LEN = 128
MAX_CATEGORY_LEN = 32
#: 기기 label — 운영자 화면(devices.py list)에 그대로 찍히므로 인쇄 가능 문자만.
MAX_LABEL_LEN = 64
#: obs_id 는 "{device}:{13자리 ms}:{12자리 해시}" 라 device 보다 27 길다(HUB-PROTOCOL §3-1).
#: device_id 를 obs_id 와 같은 128 로 두면 긴 기기명이 device_id 검증은 통과하고 파생 obs_id 는
#: 반드시 400 이 돼, 그 PC 의 배치가 영구히 격리된다(400 은 재시도 안 함).
#: 허브가 발급하는 id(`d-`+10hex)는 12자라 여유가 많지만, 관리 시크릿 업로드는 자유 문자열이다.
MAX_OBS_ID_LEN = 128
MAX_DEVICE_ID_LEN = MAX_OBS_ID_LEN - len(":") - 13 - len(":") - 12  # 101

CFG_KEY = web.AppKey("cfg", dict)
DB_KEY = web.AppKey("db", db_mod.Database)
RL_KEY = web.AppKey("limits", dict)
#: 공개 리스너 앱이면 True — 모든 요청을 공개로 본다(헤더 무관).
PUBLIC_KEY = web.AppKey("public_only", bool)
#: request[DEVICE_KEY] — 기기 토큰 인증이면 기기 dict(device_id·label·…), 관리 시크릿이면 None.
DEVICE_KEY = "device"
#: 라우트별 인증 모드 — 없는 경로는 "admin"(관리 시크릿, 직접 접속 전용).
ROUTE_AUTH = {"/api/market/register": "open", "/api/market/observations": "device_or_admin"}


def load_config(path: str, env=None) -> dict:
    env = os.environ if env is None else env
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise SystemExit("config: JSON 객체가 아닙니다")
    cfg = copy.deepcopy(DEFAULTS)
    cfg.update(raw)
    secret = cfg.get("secret")
    if not isinstance(secret, str) or not secret:
        raise SystemExit("config: 'secret' 는 비어 있지 않은 문자열이어야 합니다 — config.json 에 설정하세요")
    if secret in PLACEHOLDER_SECRETS:
        raise SystemExit("config: 'secret' 가 예시값입니다 — config.json.example 을 복사했으면 실제 값으로 바꾸세요")
    if len(secret) < MIN_SECRET_LEN:
        raise SystemExit(f"config: 'secret' 는 {MIN_SECRET_LEN}자 이상이어야 합니다")
    invite = cfg.get("invite_code")
    invite = "" if invite is None else invite
    if not isinstance(invite, str):
        raise SystemExit("config: 'invite_code' 는 문자열이어야 합니다(빈 문자열 = 등록 닫힘)")
    invite = invite.strip()      # 손으로 편집한 config 의 앞뒤 공백이 영원한 bad_invite 가 되지 않게
    if invite:
        if invite in PLACEHOLDER_INVITES:
            raise SystemExit("config: 'invite_code' 가 예시값입니다 — 실제 값으로 바꾸거나 비워 두세요(등록 닫힘)")
        if len(invite) < MIN_INVITE_LEN:
            raise SystemExit(f"config: 'invite_code' 는 {MIN_INVITE_LEN}자 이상이어야 합니다")
        if invite == secret:
            raise SystemExit("config: 'invite_code' 가 'secret' 와 같습니다 — 초대 코드는 반공개라 관리 시크릿과 달라야 합니다")
    cfg["invite_code"] = invite
    for key in ("port", "public_port"):
        v = cfg.get(key)
        if not _is_int(v) or not 1 <= v <= 65535:
            raise SystemExit(f"config: '{key}' 는 1~65535 정수여야 합니다")
    if cfg["port"] == cfg["public_port"]:
        raise SystemExit("config: 'public_port' 는 'port' 와 달라야 합니다 — 공개 리스너(Funnel 대상)와 관리 리스너를 나눈다")
    for key in ("max_devices", "register_limit_per_hour", "upload_limit_per_min",
                "auth_fail_limit_per_min"):
        v = cfg.get(key)
        if not _is_int(v) or v < 1:
            raise SystemExit(f"config: '{key}' 는 1 이상의 정수여야 합니다")
    if not isinstance(cfg.get("admin_public"), bool):
        raise SystemExit("config: 'admin_public' 는 true/false 여야 합니다")
    proxy_header = cfg.get("proxy_header")
    if not isinstance(proxy_header, str) or not proxy_header.strip():
        raise SystemExit("config: 'proxy_header' 는 비어 있지 않은 문자열이어야 합니다")
    cfg["proxy_header"] = proxy_header.strip()
    db_path = env.get(ENV_DB_PATH) or cfg.get("db_path")
    if not isinstance(db_path, str) or not db_path:
        raise SystemExit("config: 'db_path' 가 비어 있습니다")
    # 루트 경로(`/data/hub.db` — 컨테이너 ENV)는 Windows 의 isabs 가 3.13 부터 거부하므로 선두 슬래시도 절대로 본다.
    rooted = os.path.isabs(db_path) or db_path[:1] in ("/", "\\")
    if db_path != ":memory:" and not rooted:
        db_path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(path)), db_path))
    cfg["db_path"] = db_path
    return cfg


# ---------------------------------------------------------------- 속도제한

class RateLimiter:
    """고정 창 카운터 — 키(IP·기기)당 ``window_sec`` 안에 ``limit`` 번. 단일 프로세스 메모리(재기동이면 리셋).

    ``hit`` 은 세면서 판정(0 = 허용, 아니면 기다릴 초), ``blocked`` 는 세지 않고 확인만. 시계는 ``monotonic``
    (Pi 시계 점프 무관), 테스트는 주입. 만료 키는 창 하나마다 한 번 쓸어낸다.
    """

    def __init__(self, limit: int, window_sec: float, clock=time.monotonic) -> None:
        self.limit, self.window, self._clock = int(limit), float(window_sec), clock
        self._hits: dict = {}
        self._last_sweep = clock()

    def _retry_after(self, start: float, now: float) -> int:
        return max(1, math.ceil(start + self.window - now))

    def _sweep(self, now: float) -> None:
        if now - self._last_sweep < self.window:
            return
        self._last_sweep = now
        for key in [k for k, (start, _) in self._hits.items() if now - start >= self.window]:
            del self._hits[key]

    def blocked(self, key: str) -> int:
        now = self._clock()
        ent = self._hits.get(key)
        if ent is None or now - ent[0] >= self.window or ent[1] < self.limit:
            return 0
        return self._retry_after(ent[0], now)

    def hit(self, key: str) -> int:
        now = self._clock()
        self._sweep(now)
        start, count = self._hits.get(key, (now, 0))
        if now - start >= self.window:
            start, count = now, 0
        if count >= self.limit:
            self._hits[key] = (start, count)
            return self._retry_after(start, now)
        self._hits[key] = (start, count + 1)
        return 0


def _too_many(retry_after: int) -> web.Response:
    return web.json_response({"ok": False, "error": "rate_limited", "retry_after": int(retry_after)},
                             status=429, headers={"Retry-After": str(int(retry_after))})


# ---------------------------------------------------------------- auth

def _secret_eq(a: str, b: str) -> bool:
    # compare_digest 는 비ASCII str 비교를 거부 — 항상 UTF-8 바이트로 비교.
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bearer(request: web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth[7:] if auth.startswith("Bearer ") else ""


def _is_public(request: web.Request) -> bool:
    """공개 요청 — 공개 리스너로 왔거나(헤더 무관), 관리 리스너에 ``proxy_header`` 가 있거나(값 무관, 대소문자 무관)."""
    return bool(request.app[PUBLIC_KEY]) or request.app[CFG_KEY]["proxy_header"] in request.headers


_warned_no_xff = False


def _client_ip(request: web.Request) -> str:
    """속도제한·로그용 클라이언트 IP — 공개 요청은 ``X-Forwarded-For`` 의 **마지막** 항목(프록시가 붙인 원 IP; 클라이언트가
    앞에 위조값을 넣어도 마지막은 프록시 것), 없으면 ``public:?`` 한 버킷(브리지 IP 로 떨어지면 직접 접속과 섞인다). 직접 접속은
    소켓 peer."""
    global _warned_no_xff
    if _is_public(request):
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.rsplit(",", 1)[-1].strip() or "public:?"
        if not _warned_no_xff:
            _warned_no_xff = True
            log.warning("공개 요청에 X-Forwarded-For 가 없다 — 프록시 설정 확인(속도제한 키 'public:?' 하나로 묶인다)")
        return "public:?"
    return request.remote or "?"


def _auth_middleware():
    @web.middleware
    async def auth_mw(request: web.Request, handler):
        if not request.path.startswith("/api/"):
            return await handler(request)
        mode = ROUTE_AUTH.get(request.path, "admin")
        if mode == "open":
            return await handler(request)          # register — 초대 코드·속도제한은 핸들러 몫
        cfg, limits = request.app[CFG_KEY], request.app[RL_KEY]
        public = _is_public(request)
        admin_allowed = bool(cfg["admin_public"]) or not public
        if mode == "admin" and not admin_allowed:
            # 자격 검사보다 먼저 — 시크릿을 맞혀도 403 이라 추측 오라클이 없다.
            return web.json_response({"ok": False, "error": "not_public"}, status=403)
        token = _bearer(request)
        if token and admin_allowed and _secret_eq(token, cfg["secret"]):
            request[DEVICE_KEY] = None
            return await handler(request)
        if token and mode == "device_or_admin":
            try:
                dev = request.app[DB_KEY].get_device_by_token_hash(_token_hash(token))
            except sqlite3.Error:
                log.exception("기기 토큰 조회 실패")
                return _storage_error(time.time())
            if dev is not None:
                wait = limits["upload"].hit(dev["device_id"])      # 취소 검사보다 먼저 — 취소된 클라의 폭주도 기기 단위로 429
                if wait:
                    return _too_many(wait)
                if dev["revoked_ts"] is not None:
                    return web.json_response({"ok": False, "error": "device_revoked"}, status=403)
                request[DEVICE_KEY] = dev
                return await handler(request)
        if public:
            # 실패 뒤에만 센다 — 공유 NAT 뒤의 고장난 클라 하나가 같은 IP 의 정상 기기(위에서 통과)를 막지 않는다.
            wait = limits["auth_fail"].hit(_client_ip(request))
            if wait:
                return _too_many(wait)
        return web.json_response({"error": "unauthorized"}, status=401)
    return auth_mw


# ---------------------------------------------------------------- 검증

class _Bad(Exception):
    """400 사유 — error 코드 + (관측 index, 필드 경로)."""

    def __init__(self, error: str = "bad_request", index=None, field=None) -> None:
        super().__init__(error)
        self.error, self.index, self.field = error, index, field

    def response(self) -> web.Response:
        body: dict = {"ok": False, "error": self.error}
        if self.index is not None:
            body["index"] = self.index
        if self.field:
            body["field"] = self.field
        return web.json_response(body, status=400)


def _is_int(v) -> bool:
    # bool 은 int 서브클래스 — true/false 를 수량·가격으로 오인하지 않는다. 범위는 SQLite INTEGER(i64).
    return isinstance(v, int) and not isinstance(v, bool) and I64_MIN <= v <= I64_MAX


def _is_opt_int(v) -> bool:
    return v is None or _is_int(v)


def _is_num(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(float(v)))


def _is_str_max(v, max_len: int) -> bool:
    return isinstance(v, str) and len(v) <= max_len


def validate_upload(body, cfg: dict, now: float) -> tuple:
    """POST 본문 전건 검증 → (device_id, observations). 첫 위반에서 ``_Bad``.

    행 필드는 SEAssist ``packet_market.row_to_dict`` 키 + 관측기가 붙인 ``item_name``(null 허용).
    미지 키는 통과(payload_json 에 남는다). ``agent_ts`` 는 ``[now − 보존기간, now + max_agent_ts_ahead_sec]``
    안이어야 한다 — 시계가 리셋된 기기의 관측(seen_ts≈0)은 200 을 받고도 어떤 조회에도 안 보이고 다음 prune 에
    지워져 소리 없이 사라지므로, 관측기가 격리·로그로 알 수 있게 400 으로 돌려준다.
    """
    if not isinstance(body, dict):
        raise _Bad()
    device_id = body.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > MAX_DEVICE_ID_LEN:
        raise _Bad(field="device_id")
    obs_list = body.get("observations")
    if not isinstance(obs_list, list) or not obs_list:
        raise _Bad(field="observations")
    if len(obs_list) > int(cfg["max_observations_per_request"]):
        raise _Bad("too_many", field="observations")
    max_rows = int(cfg["max_rows_per_observation"])
    oldest = now - int(cfg["retention_market_days"]) * 86400
    newest = now + float(cfg["max_agent_ts_ahead_sec"])
    for i, obs in enumerate(obs_list):
        if not isinstance(obs, dict):
            raise _Bad(index=i)
        obs_id = obs.get("obs_id")
        if not isinstance(obs_id, str) or not 1 <= len(obs_id) <= MAX_OBS_ID_LEN:
            raise _Bad(index=i, field="obs_id")
        agent_ts = obs.get("agent_ts")
        if not _is_num(agent_ts):
            raise _Bad(index=i, field="agent_ts")
        if not oldest <= float(agent_ts) <= newest:
            raise _Bad("agent_ts_out_of_range", index=i, field="agent_ts")
        if not _is_int(obs.get("opcode")):
            raise _Bad(index=i, field="opcode")
        for k in ("page", "total_pages", "hdr4"):
            if not _is_opt_int(obs.get(k)):
                raise _Bad(index=i, field=k)
        rows = obs.get("rows")
        if not isinstance(rows, list):
            raise _Bad(index=i, field="rows")
        if len(rows) > max_rows:
            raise _Bad("too_many", index=i, field="rows")
        for j, row in enumerate(rows):
            if not isinstance(row, dict):
                raise _Bad(index=i, field=f"rows[{j}]")
            for k in ("listing_id", "item_id", "quantity", "price"):
                v = row.get(k)
                if not _is_int(v) or v < 0:
                    raise _Bad(index=i, field=f"rows[{j}].{k}")
            if not _is_str_max(row.get("seller"), MAX_SELLER_LEN):
                raise _Bad(index=i, field=f"rows[{j}].seller")
            name = row.get("item_name")
            if name is not None and not _is_str_max(name, MAX_ITEM_NAME_LEN):
                raise _Bad(index=i, field=f"rows[{j}].item_name")
            for k in ("flag45", "flag46"):
                if not _is_opt_int(row.get(k)):
                    raise _Bad(index=i, field=f"rows[{j}].{k}")
            category = row.get("category")
            if category is not None and not _is_str_max(category, MAX_CATEGORY_LEN):
                raise _Bad(index=i, field=f"rows[{j}].category")
    return device_id.strip(), obs_list


def validate_register(body) -> tuple:
    """등록 본문 → (invite_code, label). 코드는 앞뒤 공백을 벗긴다(콘솔 프롬프트에 붙은 공백). label 은 선택(없음·null = ""),
    인쇄 가능 문자 ≤64 — 운영자 터미널(devices.py list)에 그대로 찍히므로 제어 문자(터미널 이스케이프)는 거부한다."""
    if not isinstance(body, dict):
        raise _Bad()
    invite = body.get("invite_code")
    if not isinstance(invite, str) or not invite.strip():
        raise _Bad(field="invite_code")
    label = body.get("label")
    label = "" if label is None else label
    if not isinstance(label, str) or len(label) > MAX_LABEL_LEN or not label.isprintable():
        raise _Bad(field="label")
    return invite.strip(), label.strip()


def _q_num(request: web.Request, key: str, default, cast, lo=None, hi=None):
    """관용적 쿼리 수치(limit·since_ts·max_age_sec) — 없거나 못 읽으면 default, 범위는 클램프(default 도 — 설정의
    `*_limit_default` 가 `*_limit_max` 를 넘어도 max 를 지킨다). ``cast`` 는 int|float."""
    raw = request.query.get(key)
    v = default
    if raw is not None and raw != "":
        try:
            v = cast(raw)
        except (ValueError, OverflowError):
            v = default
        if cast is float and not math.isfinite(v):
            v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _q_item_id(request: web.Request):
    """``item_id`` 는 있으면 정확해야 한다 — 음수·비정수·i64 초과는 400. 조용히 0 으로 깎거나 버리면 틀린 결과가
    옳은 것처럼 나간다(예: ``item_id=abc&q=봉인`` 이 id 필터 없이 전부 반환)."""
    raw = request.query.get("item_id")
    if raw is None or raw == "":
        return None
    try:
        v = int(raw)
    except ValueError:
        raise _Bad(field="item_id") from None
    if not 0 <= v <= I64_MAX:
        raise _Bad(field="item_id")
    return v


def _storage_error(now: float) -> web.Response:
    """검증을 통과한 뒤의 DB 오류 — JSON 5xx(관측기는 5xx 를 재시도, 본문은 로그에 남긴다)."""
    return web.json_response({"ok": False, "error": "storage_error", "server_time": now}, status=500)


# ---------------------------------------------------------------- handlers

async def index(request: web.Request) -> web.Response:
    """무인증 상태 줄 — 데이터 없음(접속·healthcheck 확인용)."""
    return web.json_response({"service": SERVICE, "v": PROTO_V, "server_time": time.time()})


async def api_market_register(request: web.Request) -> web.Response:
    """초대 코드 → 기기 토큰. 순서: IP 속도제한(모든 시도를 센다 — 코드 비교 전에 무차별 대입 상한) → 본문 크기 →
    등록 닫힘 → 본문 검증 → 코드 비교 → 정원 → 발급. 토큰 원문은 응답에만, DB 엔 sha256."""
    cfg, limits = request.app[CFG_KEY], request.app[RL_KEY]
    ip = _client_ip(request)
    wait = limits["register"].hit(ip)
    if wait:
        return _too_many(wait)
    now = time.time()
    if request.content_length is None:
        return web.json_response({"ok": False, "error": "length_required"}, status=411)
    if request.content_length > MAX_REGISTER_BODY:
        return web.json_response({"ok": False, "error": "too_large"}, status=413)   # 파싱 전에 자른다(무인증 라우트)
    if not cfg["invite_code"]:
        return web.json_response({"ok": False, "error": "registration_closed"}, status=403)
    try:
        body = await request.json()
    except (ValueError, LookupError):
        # ValueError = 깨진 JSON·잘못된 UTF-8. LookupError = Content-Type 의 charset 이 모르는 인코딩
        # ("charset=bogus") — aiohttp 가 body.decode(charset) 에서 던지므로 여기서 400 으로 받지 않으면
        # 무인증 라우트가 500 + 트레이스백을 로그에 쌓는다.
        body = None
    try:
        invite, label = validate_register(body)
    except _Bad as e:
        return e.response()
    if not _secret_eq(invite, cfg["invite_code"]):
        log.info("기기 등록 거부(초대 코드 불일치): label=%r ip=%s", label, ip)
        return web.json_response({"ok": False, "error": "bad_invite"}, status=401)
    db = request.app[DB_KEY]
    try:
        registered = db.count_active_devices()
        if registered >= int(cfg["max_devices"]):
            log.warning("기기 등록 거부(정원 %d, 등록 %d): label=%r ip=%s — 자리를 비우려면 devices.py revoke",
                        int(cfg["max_devices"]), registered, label, ip)
            return web.json_response({"ok": False, "error": "registration_full"}, status=403)
        token = secrets.token_urlsafe(32)
        device_id = db.create_device(label, _token_hash(token), now, ip)
    except sqlite3.Error:
        log.exception("기기 등록 저장 실패: label=%r ip=%s", label, ip)
        return _storage_error(now)
    log.info("기기 등록: %s label=%r ip=%s", device_id, label, ip)
    return web.json_response({"ok": True, "v": PROTO_V, "device_id": device_id, "token": token, "server_time": now})


def _device_name(dev) -> str:
    """로그·표시용 이름 — 관리자가 붙인 ``alias`` 우선, 없으면 기기가 스스로 적은 ``label``, 둘 다 없으면 빈 문자열."""
    if dev is None:
        return ""
    return (dev.get("alias") or "").strip() or (dev.get("label") or "").strip()


def _touch_device(db: db_mod.Database, dev, now: float) -> None:
    """인증은 됐지만 거부된 업로드(400·403) — ``last_seen_ts`` 만 남긴다(운영자가 잘못 설정된 exe 를 찾을 수 있게)."""
    if dev is None:
        return
    try:
        db.touch_device(dev["device_id"], now)
    except sqlite3.Error:
        log.exception("기기 last_seen 기록 실패: %s", dev["device_id"])


async def api_market_observations(request: web.Request) -> web.Response:
    cfg = request.app[CFG_KEY]
    db = request.app[DB_KEY]
    dev = request.get(DEVICE_KEY)
    try:
        body = await request.json()
    except (ValueError, LookupError):
        # ValueError = 깨진 JSON·잘못된 UTF-8. LookupError = Content-Type 의 charset 이 모르는 인코딩
        # ("charset=bogus") — aiohttp 가 body.decode(charset) 에서 던지므로 여기서 400 으로 받지 않으면
        # 무인증 라우트가 500 + 트레이스백을 로그에 쌓는다.
        body = None
    now = time.time()
    try:
        device_id, obs_list = validate_upload(body, cfg, now)
    except _Bad as e:
        _touch_device(db, dev, now)
        return e.response()
    if dev is not None and device_id != dev["device_id"]:
        # 기기 토큰의 device_id 만 허용 — 기대값을 돌려줘 관측기가 설정 불일치를 스스로 진단한다.
        _touch_device(db, dev, now)
        return web.json_response({"ok": False, "error": "device_mismatch", "device_id": dev["device_id"]}, status=403)
    try:
        # 기기 토큰이면 devices.last_seen/upload_count 도 같은 트랜잭션 — 커밋 1회, 실패하면 아무것도(기기 기록도) 안 쓴다.
        accepted, duplicates, rows = db.insert_market_observations(
            device_id, obs_list, now, seen_device=dev["device_id"] if dev is not None else None)
    except sqlite3.Error:
        log.exception("market 관측 저장 실패: %s", device_id)
        return _storage_error(now)
    log.info("market 관측 수신: %s%s 신규 %d / 중복 %d / 행 %d", device_id,
             f"({_device_name(dev)})" if _device_name(dev) else "", accepted, duplicates, rows)
    return web.json_response({"ok": True, "accepted": accepted, "duplicates": duplicates,
                              "rows": rows, "server_time": now})


async def api_market_search(request: web.Request) -> web.Response:
    cfg = request.app[CFG_KEY]
    q = request.query.get("q", "")
    q_norm = db_mod.norm_item_name(q)
    try:
        item_id = _q_item_id(request)
        if not q_norm and item_id is None:
            raise _Bad(field="q")
    except _Bad as e:
        return e.response()
    limit = _q_num(request, "limit", int(cfg["search_limit_default"]), int, 1, int(cfg["search_limit_max"]))
    max_age = _q_num(request, "max_age_sec", float(cfg["search_max_age_sec"]), float, 0.0, None)
    now = time.time()
    rows, total = request.app[DB_KEY].search_market(q_norm, item_id, limit, now - max_age)
    return web.json_response({"v": PROTO_V, "server_time": now, "q": q, "q_norm": q_norm,
                              "item_id": item_id, "max_age_sec": max_age, "limit": limit,
                              "count": len(rows), "total_matches": total, "listings": rows})


async def api_market_listings(request: web.Request) -> web.Response:
    cfg = request.app[CFG_KEY]
    since = _q_num(request, "since_ts", 0.0, float, 0.0, None)
    since_key = request.query.get("since_key", "")
    limit = _q_num(request, "limit", int(cfg["listings_limit_default"]), int, 1, int(cfg["listings_limit_max"]))
    now = time.time()
    rows = request.app[DB_KEY].list_market_since(since, since_key, limit)
    next_since = rows[-1]["last_seen_ts"] if rows else since
    next_key = rows[-1]["listing_key"] if rows else since_key
    return web.json_response({"v": PROTO_V, "server_time": now, "since_ts": since, "since_key": since_key,
                              "limit": limit, "count": len(rows), "next_since_ts": next_since,
                              "next_since_key": next_key, "listings": rows})


async def api_market_stats(request: web.Request) -> web.Response:
    cfg = request.app[CFG_KEY]
    now = time.time()
    stats = request.app[DB_KEY].market_stats(now, float(cfg["search_max_age_sec"]))
    return web.json_response({"v": PROTO_V, "server_time": now,
                              "devices_max": int(cfg["max_devices"]), **stats})


# ---------------------------------------------------------------- retention

async def _retention_loop(app: web.Application) -> None:
    cfg = app[CFG_KEY]
    while True:
        try:
            n_o, n_l = app[DB_KEY].prune(time.time(), int(cfg["retention_market_days"]))
            if n_o or n_l:
                log.info("retention: 관측 %d건 / 목록 %d건 삭제", n_o, n_l)
        except Exception:
            log.exception("retention 오류 — 태스크 유지")
        await asyncio.sleep(86400)


# ---------------------------------------------------------------- app

def make_limiters(cfg: dict) -> dict:
    return {"register": RateLimiter(cfg["register_limit_per_hour"], 3600),
            "upload": RateLimiter(cfg["upload_limit_per_min"], 60),
            "auth_fail": RateLimiter(cfg["auth_fail_limit_per_min"], 60)}


def make_app(cfg: dict, database: db_mod.Database | None = None, *, limits: dict | None = None,
             public_only: bool = False, retention: bool = True) -> web.Application:
    """``database`` 를 주입하면 소유권은 호출자에게 — 앱이 닫혀도 close 하지 않는다(테스트·임베딩·``serve`` 가 DB 를 공유).
    ``limits`` 도 같은 식(두 리스너가 속도제한 버킷을 공유). ``public_only`` 앱은 모든 요청을 공개로 본다.
    ``retention`` 은 리스너 하나에서만 켠다."""
    app = web.Application(middlewares=[_auth_middleware()], client_max_size=CLIENT_MAX_SIZE)
    owns_db = database is None
    app[CFG_KEY] = cfg
    app[DB_KEY] = database if database is not None else db_mod.Database(cfg["db_path"])
    app[RL_KEY] = limits if limits is not None else make_limiters(cfg)
    app[PUBLIC_KEY] = bool(public_only)
    app.router.add_get("/", index)
    app.router.add_post("/api/market/register", api_market_register)
    app.router.add_post("/api/market/observations", api_market_observations)
    app.router.add_get("/api/market/search", api_market_search)
    app.router.add_get("/api/market/listings", api_market_listings)
    app.router.add_get("/api/market/stats", api_market_stats)

    async def _lifecycle(app_: web.Application):
        task = asyncio.create_task(_retention_loop(app_)) if retention else None
        yield
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if owns_db:
            app_[DB_KEY].close()

    app.cleanup_ctx.append(_lifecycle)
    return app


async def serve(cfg: dict, *, started: asyncio.Event | None = None) -> None:
    """관리 리스너(``port``)와 공개 리스너(``public_port``)를 한 프로세스에서 — DB·속도제한 공유, retention 은 관리 앱만.
    SIGINT/SIGTERM(가능한 플랫폼) 또는 태스크 취소로 멈추고, 리스너를 닫은 뒤 DB 를 닫는다."""
    database = db_mod.Database(cfg["db_path"])
    limits = make_limiters(cfg)
    listeners = ((make_app(cfg, database, limits=limits), int(cfg["port"]), "admin"),
                 (make_app(cfg, database, limits=limits, public_only=True, retention=False),
                  int(cfg["public_port"]), "public"))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):     # Windows — Ctrl+C 는 KeyboardInterrupt 로 온다
            pass
    runners: list = []
    try:
        for app, port, name in listeners:
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, cfg["host"], port).start()
            runners.append(runner)
            log.info("listen %s %s:%d", name, cfg["host"], port)
        if started is not None:
            started.set()
        await stop.wait()
    finally:
        for runner in reversed(runners):
            await runner.cleanup()
        database.close()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="육의전 시세 허브 서버")
    parser.add_argument("--config",
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config(args.config)
    log.info("db_path=%s host=%s port=%s public_port=%s registration=%s admin_public=%s proxy_header=%s",
             cfg["db_path"], cfg["host"], cfg["port"], cfg["public_port"],
             "open" if cfg["invite_code"] else "closed", cfg["admin_public"], cfg["proxy_header"])
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
