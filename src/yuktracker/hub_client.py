"""허브 HTTP 클라이언트 — 기기 등록과 관측 업로드. 계약 정본은 `docs/HUB-PROTOCOL.md` §3-0·§3-1.

stdlib `urllib` 만 쓴다(관측기 런타임 의존성 0). 관측기가 말을 거는 곳은 **공개 리스너** 하나이고,
자격은 등록 때 받은 **기기 토큰**뿐이다 — 관리 시크릿은 exe 에 없다.

응답 해석을 이 모듈 한 곳에 모았다(`classify_upload`). 스풀·업로더는 정책을 알 필요 없이
행동(`ok`/`retry`/`wait`/`split`/`quarantine`/`stop`)만 받는다:

- `200` 스풀 파일 삭제 · `400` 격리(재시도해도 같은 400) · `401`·`403` 업로더 정지(스풀 유지,
  자동 재등록 금지) · `429` `Retry-After` 만큼 기다렸다 재시도 · `413`(평문 본문) 배치 분할 ·
  `5xx`·네트워크 지수 백오프.
- 인증서 검증 실패는 무한 재시도 대신 정지 + 안내다(인증서 저장소가 낡은 PC).
"""
from __future__ import annotations

import http.client
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

#: 등록·업로드 모두 짧게 — 관측 스레드와 무관한 백그라운드지만 종료를 붙잡으면 안 된다.
TIMEOUT_SEC = 15.0
#: 허브 기본값과 같은 상한(HUB-PROTOCOL §3-1) — 넘겨도 400/413 이라 미리 자른다.
MAX_OBSERVATIONS = 100
MAX_ROWS = 64
USER_AGENT = "YukTracker"


@dataclass(frozen=True)
class Response:
    """허브 응답 1개. `status == 0` 은 네트워크 실패(HTTP 응답 자체가 없었다)."""
    status: int
    body: dict = field(default_factory=dict)
    error: str = ""
    retry_after: float = 0.0
    text: str = ""
    tls: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.body.get("ok", True))


def _retry_after(headers) -> float:
    try:
        return max(0.0, float(str(headers.get("Retry-After", "")).strip()))
    except (TypeError, ValueError):
        return 0.0


def _read(status: int, headers, raw: bytes) -> Response:
    text = raw.decode("utf-8", errors="replace")[:500]
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        # 413 은 aiohttp 가 평문으로 낸다 — JSON 을 기대하지 않는다.
        return Response(status=status, error="", retry_after=_retry_after(headers), text=text)
    return Response(status=status, body=body, error=str(body.get("error") or ""),
                    retry_after=float(body.get("retry_after") or 0.0) or _retry_after(headers),
                    text=text)


def _send(req: urllib.request.Request, timeout: float, opener) -> Response:
    """요청 1회 — 예외를 던지지 않고 `Response` 로(status 0 = 네트워크 실패)."""
    do_open = opener.open if opener is not None else urllib.request.urlopen
    try:
        with do_open(req, timeout=timeout) as resp:
            return _read(resp.status, resp.headers, resp.read())
    except urllib.error.HTTPError as e:  # 4xx·5xx — 본문이 있다
        return _read(e.code, e.headers, e.read() or b"")
    except urllib.error.URLError as e:
        tls = isinstance(e.reason, ssl.SSLCertVerificationError)
        return Response(status=0, error=("tls" if tls else "network"), text=str(e.reason), tls=tls)
    except ssl.SSLCertVerificationError as e:  # opener 를 직접 쓰는 경로
        return Response(status=0, error="tls", text=str(e), tls=True)
    except (OSError, http.client.HTTPException) as e:
        return Response(status=0, error="network", text=str(e))


def post_json(url: str, payload: dict, *, token: str = "",
              timeout: float = TIMEOUT_SEC, opener=None) -> Response:
    """POST 1회(JSON 본문)."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = "Bearer " + token
    return _send(urllib.request.Request(url, data=data, headers=headers, method="POST"),
                 timeout, opener)


def get_json(url: str, *, token: str = "", timeout: float = TIMEOUT_SEC, opener=None) -> Response:
    """GET 1회(본문 없음) — 상태 줄·ping 용."""
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = "Bearer " + token
    return _send(urllib.request.Request(url, headers=headers, method="GET"), timeout, opener)


def register(hub_url: str, invite_code: str, label: str = "", *, opener=None) -> Response:
    """`POST /api/market/register` — 무인증. 성공 200 이면 `device_id`·`token` 이 온다(토큰은 1회만 준다)."""
    payload = {"v": 1, "invite_code": invite_code}
    if label:
        payload["label"] = label
    return post_json(hub_url.rstrip("/") + "/api/market/register", payload, opener=opener)


def status(hub_url: str, *, opener=None) -> Response:
    """`GET /` — 무인증 상태 줄. 허브에 **닿는지**만 본다(자격 무관, `--selftest`)."""
    return get_json(hub_url.rstrip("/") + "/", opener=opener)


def ping(hub_url: str, token: str, *, opener=None) -> Response:
    """`GET /api/market/ping` — 기기 토큰이 아직 유효한지(HUB-PROTOCOL §3-7). 200 이면 허브가 아는 기기 id 가 온다."""
    return get_json(hub_url.rstrip("/") + "/api/market/ping", token=token, opener=opener)


def upload(hub_url: str, token: str, device_id: str, observations: list, *, opener=None) -> Response:
    """`POST /api/market/observations` — 기기 토큰. `device_id` 는 **POST 시점에** 채운다."""
    payload = {"v": 1, "device_id": device_id, "observations": observations}
    return post_json(hub_url.rstrip("/") + "/api/market/observations", payload,
                     token=token, opener=opener)


def describe_register(resp: Response) -> str:
    """등록 실패 사유 한 줄 — 사용자가 다음에 뭘 해야 하는지까지."""
    if resp.ok:
        return f"등록 완료 — 기기 {resp.body.get('device_id', '?')}"
    if resp.tls:
        return ("허브 인증서를 검증하지 못했습니다 — 이 PC 의 인증서 저장소가 낡았을 수 있습니다"
                f" (Windows 업데이트 뒤 다시 시도). {resp.text}")
    if resp.status == 0:
        return f"허브에 닿지 못했습니다 — 주소·네트워크를 확인하세요. {resp.text}"
    if resp.error == "bad_invite":
        return "초대 코드가 맞지 않습니다 — 관리자에게 받은 코드를 다시 확인하세요."
    if resp.error == "registration_full":
        return "허브 정원이 찼습니다 — 관리자에게 자리 정리를 요청하세요."
    if resp.error == "registration_closed":
        return "허브가 지금 등록을 받지 않습니다 — 관리자에게 문의하세요."
    if resp.error == "rate_limited":
        return f"등록 시도가 너무 잦습니다 — {resp.retry_after:.0f}초 뒤에 다시 시도하세요."
    if resp.error == "bad_request":
        field_name = resp.body.get("field")
        return f"등록 요청이 거부됐습니다(형식 오류{': ' + str(field_name) if field_name else ''})."
    return f"등록 실패 — HTTP {resp.status} {resp.error or resp.text}".strip()


def describe_status(resp: Response) -> str:
    """`GET /` 결과 한 줄 — 허브에 **닿는지**만 말한다(자격 문제는 ping 이 본다)."""
    if resp.ok:
        return f"허브가 응답했습니다 (v{resp.body.get('v', '?')})"
    if resp.tls:
        return ("허브 인증서를 검증하지 못했습니다 — 이 PC 의 인증서 저장소가 낡았을 수 있습니다"
                f" (Windows 업데이트 뒤 다시 시도). {resp.text}")
    if resp.status == 0:
        return f"허브에 닿지 못했습니다 — 주소·네트워크를 확인하세요. {resp.text}"
    return f"허브 주소가 이상합니다 — HTTP {resp.status} {resp.error or resp.text}".strip()


def describe_ping(resp: Response) -> str:
    """토큰 확인 결과 한 줄 — 다음에 뭘 해야 하는지까지(`describe_register` 와 같은 문체).

    허브가 **옛 판**(ping 없음)이면 404 이거나, 공개 리스너에서는 관리 라우트로 보여 403 `not_public` 이다 —
    둘 다 사용자가 할 일은 같다(관리자에게 허브 업데이트 요청).
    """
    if resp.ok:
        label = str(resp.body.get("label") or "")
        return (f"허브가 기기 토큰을 확인했습니다 — 기기 {resp.body.get('device_id', '?')}"
                + (f" ({label})" if label else ""))
    if resp.tls:
        return ("허브 인증서를 검증하지 못했습니다 — 이 PC 의 인증서 저장소가 낡았을 수 있습니다"
                f" (Windows 업데이트 뒤 다시 시도). {resp.text}")
    if resp.status == 0:
        return f"허브에 닿지 못했습니다 — 주소·네트워크를 확인하세요. {resp.text}"
    if resp.status == 404 or (resp.status == 403 and resp.error == "not_public"):
        return "허브에 토큰 확인 기능이 없습니다 — 허브가 옛 판입니다(관리자에게 업데이트를 요청하세요)."
    if resp.status == 401:
        return "허브가 기기 토큰을 거부했습니다(401) — --invite-code 로 다시 등록하세요."
    if resp.status == 403 and resp.error == "device_revoked":
        return "이 기기가 허브 명단에서 제거됐습니다(403) — 관리자에게 문의하세요."
    if resp.status == 429:
        return f"요청이 너무 잦습니다 — {resp.retry_after:.0f}초 뒤에 다시 시도하세요."
    return f"토큰 확인 실패 — HTTP {resp.status} {resp.error or resp.text}".strip()


def classify_upload(resp: Response) -> tuple[str, str]:
    """업로드 응답 → (행동, 사람이 읽는 사유). 행동은 여섯 가지뿐이다.

    `ok` 스풀 삭제 · `retry` 백오프 재시도 · `wait` `Retry-After` 대기 후 재시도 ·
    `split` 배치 분할 · `quarantine` 격리 · `stop` 업로더 정지(스풀 유지).
    """
    if resp.ok:
        return "ok", (f"관측 {resp.body.get('accepted', 0)}건 반영"
                      f"(중복 {resp.body.get('duplicates', 0)} · 행 {resp.body.get('rows', 0)})")
    if resp.tls:
        return "stop", "허브 인증서 검증 실패 — 이 PC 의 인증서 저장소를 갱신한 뒤 다시 실행하세요."
    if resp.status == 0:
        return "retry", f"허브에 닿지 못했습니다 — {resp.text}"
    if resp.status == 429:
        return "wait", f"허브가 속도를 제한했습니다 — {resp.retry_after:.0f}초 대기"
    if resp.status == 413:
        return "split", "배치가 너무 큽니다 — 반으로 나눠 다시 보냅니다."
    if resp.status == 401:
        return "stop", "허브가 기기 토큰을 거부했습니다(401) — 업로드를 멈춥니다. --invite-code 로 다시 등록하세요."
    if resp.status == 403:
        if resp.error == "device_revoked":
            return "stop", "이 기기가 허브 명단에서 제거됐습니다(403) — 관리자에게 문의하세요."
        if resp.error == "device_mismatch":
            return "stop", ("설정의 기기 id 가 토큰과 다릅니다(403) — 허브가 알려준 id 는 "
                            f"{resp.body.get('device_id', '?')} 입니다.")
        return "stop", f"허브가 요청을 거부했습니다(403 {resp.error})."
    if resp.status == 400:
        where = resp.body.get("field") or resp.body.get("index")
        return "quarantine", f"허브가 배치를 거부했습니다(400 {resp.error}{' ' + str(where) if where else ''})."
    if 500 <= resp.status:
        return "retry", f"허브 오류(HTTP {resp.status} {resp.error})."
    return "quarantine", f"예상 밖 응답(HTTP {resp.status} {resp.error or resp.text})."


def chunks(observations: list, size: int = MAX_OBSERVATIONS):
    """배치 상한(100)으로 자른다 — 넘겨 보내면 400 `too_many` 다."""
    size = max(1, min(int(size), MAX_OBSERVATIONS))
    for i in range(0, len(observations), size):
        yield observations[i:i + size]


def make_opener(ca_file: Optional[str] = None):
    """기본 검증 컨텍스트의 opener — 테스트가 로컬 서버를 가리킬 때만 인자를 준다."""
    ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
