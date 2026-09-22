"""자가진단(`--selftest`) — 지인 PC 에서 "왜 안 되는지"를 한 화면으로. 절차는 `docs/DEPLOY.md`.

관측기는 아는 사람 최대 7명의 PC 에서 돈다. 안 될 때 원격으로 물어볼 수 있는 것은 **화면에 찍힌 줄**뿐이라,
막히는 지점(권한·Npcap·거상·클라 폴더·허브 주소·토큰)을 한 번에 훑어 각 줄에 **다음 행동**까지 적는다.

줄 하나 = 검사 하나이고 표시는 셋이다:

- ``O`` 통과 · ``!`` 경고(정상일 수도 있다 — 거상이 꺼져 있다·아이템 표가 없다·아직 등록 안 했다)
- ``X`` 실패(이대로면 관측이나 업로드가 안 된다) · ``-`` 앞 검사가 막혀 확인하지 않음

종료 코드: **0** 전부 통과 · **1** 실패 하나 이상 · **2** 경고만(`dump_item_names.py --check` 와 같은 방향).

실 프로브(엔진·프로세스·클라 폴더·네트워크)는 전부 `Probes` 에 주입한다 — 판정부 `run()` 은 순수 함수라
테스트가 사용자 프로필·Npcap·네트워크에 닿지 않는다(`agent.setup_market` 과 같은 관례).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from . import __version__, app_config, hub_client, market_observer, paths, spool
from .game_processes import game_pids

#: 표시 기호.
OK, WARN, FAIL, SKIP = "O", "!", "X", "-"
#: 종료 코드.
RC_OK, RC_FAIL, RC_WARN = 0, 1, 2
#: 캡처 핸들이 열리기를 기다리는 기본 상한(초) — 관측 모드의 흐름 대기(120s)보다 짧다. 자가진단은 빨리 끝나야 한다.
WAIT_SEC = 15.0
_POLL_SEC = 0.5


@dataclass(frozen=True)
class Check:
    mark: str
    name: str
    detail: str

    def line(self) -> str:
        # 전각 폭 정렬은 하지 않는다 — 콘솔 폰트마다 어긋난다(hub/devices.py 의 교훈).
        return f"  {self.mark}  {self.name} — {self.detail}"


#: Npcap 프로브 사유 토큰(`seassist.pcap_ffi.REASON_*`) → **이 프로그램의** 안내.
#: 벤더의 `reason_message` 는 SEAssist GUI 용이라("화면 감지를 계속 사용합니다") 여기서 쓰면 엉뚱한 말이 된다.
#: 특히 `not_elevated` 에 "Npcap 을 설치하라"고 하면 지인이 멀쩡한 설치를 다시 깐다.
NPCAP_HINTS = {
    "not_elevated": "관리자 권한이 아니라 Npcap 을 확인하지 못했습니다 — 관리자 권한으로 다시 실행하세요.",
    "npcap_not_installed": "Npcap 이 설치돼 있지 않습니다 — https://npcap.com 에서 설치한 뒤 다시 실행하세요.",
    "wpcap_symbol_missing": "설치된 것이 Npcap 이 아니라 옛 WinPcap 으로 보입니다 — "
                            "https://npcap.com 의 Npcap 으로 다시 설치하세요.",
    "npcap_driver_unavailable": "Npcap 드라이버가 응답하지 않습니다 — PC 를 다시 켠 뒤에도 그러면 "
                                "Npcap 을 재설치하세요.",
    "no_capture_device": "캡처할 수 있는 네트워크 어댑터가 없습니다 — Npcap 설치 때 어댑터 항목을 확인하세요.",
}


@dataclass(frozen=True)
class CaptureProbe:
    """캡처 확인 결과 — 엔진을 띄워 보고 바로 멈춘 결과다."""
    npcap_ok: bool
    npcap_msg: str
    capturing: bool = False
    packets: int = 0
    engine_msg: str = ""
    #: 실패 사유 토큰(`pcap_ffi.REASON_*`) — 안내 문장을 고르는 데 쓴다.
    npcap_reason: str = ""


@dataclass
class Probes:
    """주입 가능한 실측 함수 묶음. 기본값은 `default_probes()` 가 채운다."""
    is_admin: Callable[[], bool]
    capture: Callable[[], CaptureProbe]
    game_pids: Callable[[], Sequence[int]]
    item_names: Callable[[], market_observer.ItemNames]
    config: Callable[[], app_config.Config]
    hub_status: Callable[[str], hub_client.Response]
    hub_ping: Callable[[str, str], hub_client.Response]
    spool_counts: Callable[[], tuple[int, int]]
    env: dict = field(default_factory=dict)
    #: CLI 오버라이드 — 판정 문구가 "어디서 온 주소인지"를 말할 수 있게 그대로 들고 있는다.
    cli_hub_url: str = ""
    upload: bool = True


# ---------------------------------------------------------------- 실 프로브

def probe_capture(wait_sec: float = WAIT_SEC, *, factory=None, pids=None,
                  sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic) -> CaptureProbe:
    """엔진을 띄워 캡처 핸들이 열리는지 보고 **반드시 멈춘다**(자가진단은 관측하지 않는다).

    `start()` 가 Npcap 유무를, 그 뒤 `is_capturing()` 이 게임 흐름(8000) 을 잡았는지를 말해 준다 —
    둘은 다른 고장이라 줄도 둘이다.
    """
    from .seassist import pcap_ffi
    from .seassist.packet_state_source import PacketStateSource

    ok, reason = pcap_ffi.npcap_available()
    if not ok:
        return CaptureProbe(npcap_ok=False, npcap_msg=pcap_ffi.reason_message(reason),
                            npcap_reason=reason)
    provider = pids if pids is not None else _pid_map
    engine = (factory or PacketStateSource)(provider)
    started, msg = engine.start()
    if not started:
        return CaptureProbe(npcap_ok=False, npcap_msg=msg, npcap_reason=reason)
    try:
        deadline = clock() + max(0.0, float(wait_sec))
        while not engine.is_capturing() and clock() < deadline:
            sleep(_POLL_SEC)
        health = engine.health_snapshot()
        return CaptureProbe(npcap_ok=True, npcap_msg="Npcap 사용 가능", engine_msg=msg,
                            capturing=bool(engine.is_capturing()),
                            packets=int(health.get("packets", 0)))
    finally:
        engine.stop()


def _pid_map() -> dict[int, int]:
    return {i: pid for i, pid in enumerate(sorted(game_pids()))}


def _spool_counts() -> tuple[int, int]:
    """(대기 배치, 격리 배치). 폴더가 아직 없으면 둘 다 0 이다 — `Spool` 은 생성만으로 폴더를 만들지 않는다."""
    store = spool.Spool()
    try:
        quarantined = sum(1 for p in store.quarantine_dir.glob("obs_*.jsonl") if p.is_file())
    except OSError:
        quarantined = 0
    return len(store.pending()), quarantined


def default_probes(*, hub_url: str = "", client_dir: str = "", upload: bool = True,
                   wait_sec: float = WAIT_SEC) -> Probes:
    from .seassist.admin import is_admin

    opener = hub_client.make_opener()
    return Probes(
        is_admin=is_admin,
        capture=lambda: probe_capture(wait_sec),
        game_pids=game_pids,
        item_names=lambda: market_observer.ItemNames.load(client_dir=client_dir),
        config=app_config.load,
        hub_status=lambda url: hub_client.status(url, opener=opener),
        hub_ping=lambda url, token: hub_client.ping(url, token, opener=opener),
        spool_counts=_spool_counts,
        cli_hub_url=hub_url, upload=upload)


# ---------------------------------------------------------------- 판정(순수)

def run(p: Probes) -> tuple[list[Check], int]:
    """검사 줄들과 종료 코드. 어떤 프로브가 예외를 내도 그 줄만 실패로 접고 나머지를 계속한다."""
    checks: list[Check] = []
    add = checks.append

    admin = _safe(p.is_admin, False)
    add(Check(OK if admin else FAIL, "관리자 권한",
              "관리자로 실행 중입니다" if admin else
              "관리자 권한이 아닙니다 — exe 를 오른쪽 클릭 → '관리자 권한으로 실행' 하세요."))

    pids = list(_safe(p.game_pids, []))
    add(Check(OK if pids else WARN, "거상 실행",
              f"거상 클라이언트 {len(pids)}개를 찾았습니다" if pids else
              "실행 중인 거상이 없습니다 — 거상을 켜고 서버에 접속한 뒤 다시 실행하세요."))

    cap = _safe(p.capture, None)
    if cap is None:
        add(Check(FAIL, "Npcap", "캡처를 확인하지 못했습니다(내부 오류)."))
    elif not cap.npcap_ok:
        add(Check(FAIL, "Npcap", NPCAP_HINTS.get(
            cap.npcap_reason,
            f"{cap.npcap_msg} — https://npcap.com 에서 Npcap 을 설치한 뒤 다시 실행하세요.")))
    else:
        add(Check(OK, "Npcap", cap.npcap_msg))
    if cap is None or not cap.npcap_ok:
        add(Check(SKIP, "패킷 흐름", "Npcap 확인 뒤에 봅니다."))
    elif cap.capturing:
        add(Check(OK, "패킷 흐름", f"게임 서버 흐름(8000)을 잡았습니다 — 패킷 {cap.packets}"))
    elif not pids:
        add(Check(SKIP, "패킷 흐름", "거상이 꺼져 있어 확인하지 못했습니다."))
    else:
        add(Check(FAIL, "패킷 흐름",
                  "거상은 떠 있는데 서버 흐름(8000)을 못 잡았습니다 — 게임이 서버에 접속돼 있는지, "
                  "Npcap 이 쓰는 어댑터가 맞는지 확인하세요."))

    names = _safe(p.item_names, None)
    rows = getattr(names, "rows", 0) if names is not None else 0
    gcs = getattr(names, "gcs_path", None)
    add(Check(OK if rows else WARN, "아이템 표",
              f"{rows}건 ({gcs if gcs else '저장된 표'})" if rows else
              "아이템 이름표를 못 읽었습니다 — 이름 없이 관측합니다(허브가 다른 PC 의 이름으로 채웁니다). "
              "거상 폴더가 특이하면 --client-dir 로 알려 주세요."))

    cfg = _safe(p.config, None) or app_config.Config()
    hub_url = app_config.resolve_hub_url(p.cli_hub_url, cfg, p.env or None)
    if not p.upload:
        add(Check(WARN, "허브 설정", "--no-upload — 업로드 없이 관측만 합니다."))
    elif not hub_url:
        add(Check(WARN, "허브 설정",
                  "허브 주소가 없습니다 — --hub-url 로 주거나 주소가 주입된 exe 를 받으세요(업로드 비활성)."))
    else:
        add(Check(OK, "허브 설정", f"{hub_url} ({_url_origin(p, cfg)})"
                                   + (f" 기기 {cfg.hub_device_id}" if cfg.hub_device_id else " 미등록")))

    if not hub_url or not p.upload:
        why = "업로드를 끈 상태라 보지 않습니다." if not p.upload else "허브 주소가 정해진 뒤에 봅니다."
        add(Check(SKIP, "허브 도달", why))
        add(Check(SKIP, "기기 토큰", why))
    else:
        resp = _safe(lambda: p.hub_status(hub_url), None)
        reachable = bool(resp is not None and resp.ok)
        add(Check(OK if reachable else FAIL, "허브 도달",
                  hub_client.describe_status(resp) if resp is not None
                  else "허브 상태를 확인하지 못했습니다(내부 오류)."))
        if not cfg.hub_token:
            add(Check(WARN, "기기 토큰",
                      "아직 등록하지 않았습니다 — --invite-code <초대코드> 로 한 번만 등록하면 됩니다."))
        elif not reachable:
            add(Check(SKIP, "기기 토큰", "허브에 닿은 뒤에 봅니다."))
        else:
            add(_ping_check(p, hub_url, cfg))

    pending, quarantined = _safe(p.spool_counts, (-1, -1))
    if pending < 0 or quarantined < 0:
        add(Check(WARN, "업로드 대기", f"스풀 폴더를 읽지 못했습니다 ({paths.spool_dir()})."))
    else:
        add(Check(WARN if (pending or quarantined) else OK, "업로드 대기",
                  f"대기 {pending}배치 · 격리 {quarantined}배치"
                  + (" — 다음 실행에서 이어 올립니다." if pending else "")
                  + (" 격리는 허브가 거부한 배치입니다(관리자에게 알려 주세요)." if quarantined else "")))

    marks = [c.mark for c in checks]
    rc = RC_FAIL if FAIL in marks else (RC_WARN if WARN in marks else RC_OK)
    return checks, rc


def _ping_check(p: Probes, hub_url: str, cfg: app_config.Config) -> Check:
    resp = _safe(lambda: p.hub_ping(hub_url, cfg.hub_token), None)
    if resp is None:
        return Check(FAIL, "기기 토큰", "토큰을 확인하지 못했습니다(내부 오류).")
    if not resp.ok:
        return Check(FAIL, "기기 토큰", hub_client.describe_ping(resp))
    seen = str(resp.body.get("device_id") or "")
    if cfg.hub_device_id and seen and seen != cfg.hub_device_id:
        return Check(FAIL, "기기 토큰",
                     f"설정의 기기 id({cfg.hub_device_id})와 허브가 아는 id({seen})가 다릅니다 — "
                     "--invite-code 로 다시 등록하세요.")
    return Check(OK, "기기 토큰", hub_client.describe_ping(resp))


def _url_origin(p: Probes, cfg: app_config.Config) -> str:
    if p.cli_hub_url.strip():
        return "--hub-url"
    if cfg.hub_url:
        return "설정 파일"
    if (p.env or {}).get(app_config.ENV_HUB_URL, "").strip():
        return "환경변수"
    return "빌드 주입"


def _safe(fn: Callable, fallback):
    """프로브 하나가 터져도 자가진단 전체가 죽지 않는다 — 그 줄만 fallback 으로 접는다."""
    try:
        return fn()
    except Exception:
        return fallback


# ---------------------------------------------------------------- 출력

def render(checks: Sequence[Check], rc: int) -> list[str]:
    fails = sum(1 for c in checks if c.mark == FAIL)
    warns = sum(1 for c in checks if c.mark == WARN)
    lines = [f"육의전 관측기 v{__version__} 자가진단", ""]
    lines += [c.line() for c in checks]
    lines += ["", f"결과: 실패 {fails} · 경고 {warns} (종료 코드 {rc})"]
    if fails:
        lines.append("X 줄의 안내를 먼저 처리한 뒤 다시 실행하세요. 그래도 안 되면 이 화면을 그대로 보내 주세요.")
    elif warns:
        lines.append("! 줄은 상황에 따라 정상입니다(거상이 꺼져 있거나 아직 등록 전이면 그렇습니다).")
    else:
        lines.append("전부 정상입니다 — 육의전을 한 번 열면 허브에 올라갑니다.")
    lines.append(f"설정 파일: {paths.config_path()}")
    return lines


def main(*, hub_url: str = "", client_dir: str = "", upload: bool = True,
         wait_sec: float = WAIT_SEC, probes: Optional[Probes] = None,
         pause: bool = True, out=None, stdin=None) -> int:
    """자가진단 1회. `pause` 는 승격 재기동으로 **새로 뜬 콘솔**이 결과를 보여 주기 전에 닫히지 않게 한다."""
    import sys

    out = out if out is not None else sys.stdout
    p = probes if probes is not None else default_probes(
        hub_url=hub_url, client_dir=client_dir, upload=upload, wait_sec=wait_sec)
    checks, rc = run(p)
    for line in render(checks, rc):
        print(line, file=out, flush=True)
    if pause:
        print("Enter 를 누르면 창을 닫습니다", file=out, flush=True)
        try:
            (stdin if stdin is not None else sys.stdin).readline()
        except (EOFError, OSError, ValueError, AttributeError):
            pass
    return rc
