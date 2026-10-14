"""사통팔달(서버 전체 외치기) `0x1f82` 파서 — 검증된 배치만 수용 (2026-10-14, H-2610-01 B).

배경
----
사통팔달은 서버에 접속한 클라 전부에 8000 s2c 로 내려온다(유저가 아무것도 안 해도). 서버 큐가 약 15초
간격으로 한 건씩 내보낸다. 본문 73B 고정(docs/PACKET-SATONG.md §필드 표)::

    [0]=0x00 magic  [1:5]=0x1f82 u32 LE  [5:25] 보낸이 cp949 20B NUL 종료  [25] 미상(잔재)
    [26:66] 내용 cp949 40B — 짧으면 NUL 종료, 40B 를 꽉 채우면 NUL 없음  [66:73] 꼬리 7B([67] 관측 0x01)

보낸이·내용 뒤의 바이트는 서버 버퍼 잔재라 값이 매번 다르다(코퍼스 고유 252프레임 기준) — 첫 NUL 까지만 읽는다.
서버 식별자는 프레임에 없다 — 어느 서버인지는 관측기 설정(`satong_server_id`)이 정한다.

원칙 (`packet_market.py` 선례)
------------------------------
- 구조(길이·헤더)가 관측된 배치와 다르면 `None`. 소비자가 프레이머 없이도 부르므로 `is_satong` 을 다시 한다.
- C 등급 관찰(꼬리 `[67]` 0x01, 보낸이 NUL 존재, cp949 해독 가능)은 판별 조건이 아니라 anomaly 다 — 값이
  어긋나도 메시지는 수용하고 `SatongMessage.anomalies` 에 표시한다. 단 **보낸이가 비면 None** 이다 — 허브와
  미루봇이 보낸이 없는 사통을 표시할 수 없고, 코퍼스에 한 건도 없다.
- stdlib + `.gersang_protocol` 상대 import 만.
- 보낸이·내용은 다른 플레이어의 글이다. 원본은 로컬·허브에만, 문서·PR·테스트에는 합성값.
"""
from __future__ import annotations

from dataclasses import dataclass

from .gersang_protocol import SATONG_BODY_LEN, is_satong

__all__ = [
    "SatongMessage",
    "parse_satong",
    "SENDER_LEN",
    "TEXT_LEN",
]

#: 보낸이 필드 폭(B) — 관측 최대 10B.
SENDER_LEN = 20
#: 내용 필드 폭(B) — 관측 최대 40B(꽉 차면 NUL 없음, 252 중 15).
TEXT_LEN = 40
_SENDER_OFF = 5
_MID_OFF = _SENDER_OFF + SENDER_LEN          # 25
_TEXT_OFF = _MID_OFF + 1                     # 26
_TAIL_OFF = _TEXT_OFF + TEXT_LEN             # 66
#: 꼬리 안의 관측 상수 위치(body[67]) — 252/252 가 0x01.
_TAIL_ONE_OFF = _TAIL_OFF + 1
assert _TAIL_OFF + 7 == SATONG_BODY_LEN


def _cstr(raw: bytes) -> tuple[str, bool, bool]:
    """첫 NUL 까지 cp949 → (문자열, NUL 있었나, 해독 오류 없었나). 해독 불가 바이트는 U+FFFD."""
    nul = raw.find(0)
    head = raw[:nul] if nul >= 0 else raw
    try:
        return head.decode("cp949"), nul >= 0, True
    except UnicodeDecodeError:
        return head.decode("cp949", errors="replace"), nul >= 0, False


@dataclass(frozen=True)
class SatongMessage:
    """사통팔달 1건. 시각·서버는 없다(프레임에 없다) — 관측기가 붙인다."""
    sender: str
    text: str
    sender_nul: bool
    #: 내용 40B 를 꽉 채우면 False 가 정상이다(anomaly 아님).
    text_nul: bool
    decode_ok: bool
    #: body[25] — 보낸이와 내용 사이 1B(관측 0 이 165/252, 나머지는 잔재로 보인다, D).
    mid: int
    #: body[66:73] 원바이트.
    tail: bytes

    @property
    def tail_hex(self) -> str:
        return self.tail.hex()

    @property
    def anomalies(self) -> tuple[str, ...]:
        """필드 표의 falsifier 가 관측된 항목 — 수용은 하되 표시한다. 결정적 순서."""
        out: list[str] = []
        if not self.sender_nul:
            out.append("sender_no_nul")
        if not self.decode_ok:
            out.append("cp949")
        if self.tail[_TAIL_ONE_OFF - _TAIL_OFF] != 0x01:
            out.append("tail67")
        return tuple(out)


def parse_satong(body: bytes) -> SatongMessage | None:
    """완성 본문 → `SatongMessage`. 길이·헤더가 다르거나 보낸이가 비면 **None**. 빈 내용은 수용한다."""
    opcode = int.from_bytes(body[1:5], "little") if len(body) >= 5 else -1
    if not is_satong(opcode, body):
        return None
    sender, sender_nul, ok1 = _cstr(body[_SENDER_OFF:_MID_OFF])
    text, text_nul, ok2 = _cstr(body[_TEXT_OFF:_TAIL_OFF])
    if not sender.strip():
        return None
    return SatongMessage(sender=sender, text=text, sender_nul=sender_nul, text_nul=text_nul,
                         decode_ok=ok1 and ok2, mid=body[_MID_OFF], tail=bytes(body[_TAIL_OFF:]))
