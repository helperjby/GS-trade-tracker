"""합성 프레임 빌더 — 프레이머·파서·엔진 테스트 공용.

**실데이터 없음**이 이 모듈의 존재 이유다. 판매자명은 `판매자A`·`테스트판매자A` 처럼 합성이고,
바이트는 전부 여기서 만든다(캡처 파일·실판매자명은 레포에 들어가지 않는다 — AGENTS.md).

프레임 배치는 `[len:2 LE incl-self][body…]`, body 는 `[magic][opcode:2 LE][reserved][…]`.
육의전 행 배치는 `docs/PACKET-MARKET.md` §필드 표(9B 헤더 + 48B × 행).
"""
from __future__ import annotations

from yuktracker.seassist import gersang_protocol as gp
from yuktracker.seassist import packet_market as pm

# ─────────────────────────── 일반 프레임 ───────────────────────────

#: idle 베이스라인 틱 1개(0x03ee, 7B body) — 락 채우기·프레임 경계 확인용.
TICK = bytes.fromhex("00 ee 03 00 00 01 00")


def wire(body: bytes) -> bytes:
    """본문에 길이 접두(자기 포함)를 붙여 스트림 바이트로."""
    return (len(body) + gp.LEN_PREFIX).to_bytes(2, "little") + body


def build_frame(opcode: int, *, subtype: int | None = None, body_len: int = 16,
                fill: int = 0x5A) -> bytes:
    """`[len:2 LE incl-self][00][opcode:2 LE][00][?][subtype][fill...]`"""
    body = bytearray(body_len)
    body[0] = gp.MAGIC
    body[1:3] = opcode.to_bytes(2, "little")
    if body_len > 3:
        body[3] = 0x00
    if body_len > 4:
        body[4] = 0x00
    if subtype is not None and body_len > gp.SUBTYPE_OFFSET:
        body[gp.SUBTYPE_OFFSET] = subtype
    for i in range(gp.SUBTYPE_OFFSET + 1, body_len):
        body[i] = fill
    return (body_len + gp.LEN_PREFIX).to_bytes(2, "little") + bytes(body)


def enter_frame(body_len: int = 5163) -> bytes:
    return build_frame(gp.OP_BATTLE_ENTER, subtype=gp.ENTER_SUBTYPE_VALUE, body_len=body_len)


def exit_frame(body_len: int = 560) -> bytes:
    return build_frame(gp.OP_BATTLE_EXIT, subtype=gp.EXIT_SUBTYPE_VALUE, body_len=body_len)


def idle_frames(n: int, start: int = 0) -> list[bytes]:
    """idle 베이스라인 opcode 를 순환하는 짧은 틱 프레임들 (실측 9~16B)."""
    ops = sorted(gp.IDLE_BASELINE_OPCODES)
    return [build_frame(ops[(start + i) % len(ops)], subtype=0x00, body_len=9 + (i % 8))
            for i in range(n)]


def locked_stream(*frames: bytes, lead: int = gp.DEFAULT_LOCK_K) -> bytes:
    """락에 필요한 만큼 idle 프레임을 앞에 붙인 스트림."""
    return b"".join(idle_frames(lead)) + b"".join(frames)


def jochul_body(opcode: int = gp.OP_JOCHUL_PATCHED, tail: bytes = b"\x00\x00\x00") -> bytes:
    """합성 조철 본문 13B — `is_jochul` 이 보는 것은 magic + opcode u32 + 길이뿐이다.
    body[5:10] 인스턴스 ID·꼬리는 판정 조건이 아니라 아무 값이나 넣는다."""
    body = bytes((gp.MAGIC,)) + opcode.to_bytes(4, "little") + b"\x11\x22\x33\x44\x55" + tail
    assert len(body) == 13
    return body


# ─────────────────────────── 육의전 프레임 ───────────────────────────

OP = gp.OP_MARKET_LIST
#: 합성 판매자명 — cp949 로 12B(≤ SELLER_LEN 16). 실판매자명은 쓰지 않는다.
SELLER_A = "테스트판매자A"
SELLER_B = "판매자B"
#: 문서 표기(원바이트) `20 90 27 09` = u32 LE 0x09279020.
UNKNOWN40 = bytes.fromhex("20902709")


def seller_bytes(name: str, *, residue: bytes = b"") -> bytes:
    """cp949 이름 + NUL + 잔여 바이트(서버 버퍼 잔재 재현) + 0 패딩 → 16B."""
    s = name.encode("cp949")
    raw = s + b"\x00" + residue
    assert len(raw) <= pm.SELLER_LEN, name
    return raw + b"\x00" * (pm.SELLER_LEN - len(raw))


def market_row(listing_id: int = 3769170, item_id: int = 6215, quantity: int = 329,
               price: int = 780_000, seller: str = SELLER_A, *, quantity_hi: int = 0,
               price_hi: int = 0, seller_raw: bytes | None = None,
               unknown40: bytes = UNKNOWN40, raw44: int = 0, flag45: int = 2,
               flag46: int = 3, raw47: int = 0) -> bytes:
    """48B 행 — `docs/PACKET-MARKET.md` §필드 표 배치."""
    if seller_raw is None:
        seller_raw = seller_bytes(seller)
    assert len(seller_raw) == pm.SELLER_LEN and len(unknown40) == 4
    return (listing_id.to_bytes(4, "little") + item_id.to_bytes(4, "little")
            + quantity.to_bytes(4, "little") + quantity_hi.to_bytes(4, "little")
            + price.to_bytes(4, "little") + price_hi.to_bytes(4, "little")
            + seller_raw + unknown40 + bytes((raw44, flag45, flag46, raw47)))


def market_body(rows, *, total_pages: int | None = None, count: int | None = None,
                hdr4: int = 0, opcode: int = OP) -> bytes:
    """9B 헤더 + 행들. `count` 를 주면 헤더의 행 수를 실제 행 수와 다르게 만들 수 있다."""
    rows = list(rows)
    count = len(rows) if count is None else count
    total_pages = (1 if rows else 0) if total_pages is None else total_pages
    return (bytes((gp.MAGIC,)) + opcode.to_bytes(2, "little") + b"\x00" + bytes((hdr4,))
            + total_pages.to_bytes(2, "little") + count.to_bytes(2, "little")
            + b"".join(rows))


def market_frame(rows, **kw) -> bytes:
    return wire(market_body(rows, **kw))


def default_rows(n: int, *, start: int = 3769258) -> list[bytes]:
    """기본 목록 관찰대로 등록 id 내림차순 n 행."""
    return [market_row(listing_id=start - k, item_id=100 + k, quantity=1 + k,
                       price=10_000 * (k + 1), seller=SELLER_A if k % 2 == 0 else SELLER_B)
            for k in range(n)]
