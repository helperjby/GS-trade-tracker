"""육의전(유저 거래소) 목록 응답 `0x321f` 파서 — 검증된 배치만 수용 (2026-09-21, 패킷 PR-Y2).

배경
----
육의전 목록은 유저가 육의전을 열·검색·페이지를 넘길 때만 서버 8000 s2c 로 내려온다
(docs/PACKET-MARKET-2026-09-21.md, PACKET-PROCESS §3 H-2609-08 B). 본문 = 9B 헤더 + 48B × 행::

    헤더  [0]=0x00 magic  [1:3]=0x321f LE  [3]=0  [4]=0(관측)  [5:7]=총 페이지 u16  [7:9]=행 수 u16
    행    [+0 등록 id u32][+4 아이템 id u32][+8 수량 u32][+12 0×4][+16 단가 u32][+20 0×4]
          [+24 판매자 cp949 16B NUL 종료][+40 4B 미상 — 프레임 내 동일][+44 0][+45 u8 1|2][+46 u8 0~7][+47 0]

인게임 `물품 목록` 창의 열 `물품명 · 판매개수 · 판매자 · 단가` 가 각각 `+4`(id → 이름 표) · `+8` ·
`+24` · `+16` 이다. 아이템명은 프레임에 없다(H-2609-07 기각) — id→이름은 소비자(YukTracker)가
클라 리소스 표로 푼다. 페이지 번호도 응답에 없다(요청에만) — 소비자는 등록 id 로 페이지를 잇는다.
용병 탭(레벨 열)은 아직 표본이 없어 이 파서의 보장 범위 밖이다.

원칙 (`packet_death.py` 선례)
--------------------------------
- 구조(길이·헤더·행 수)가 관측된 배치와 다르면 `None` — 잘못 파싱한 페이지를 내지 않는다.
  소비자가 프레이머 없이도 부르므로 `gersang_protocol.is_market` 과 같은 검사를 여기서 다시 한다.
- C/D 등급 필드의 "항상 0"·"1|2" 같은 관찰은 **판별 조건이 아니라 anomaly** 다 — 값이 어긋나도
  페이지는 수용하고 `MarketPage.anomalies` 에 표시한다(원장·헬스·explore 로 드리프트가 보인다).
  상수로 박았다가 기각된 H-2607-05 의 교훈. H-2609-09(u32 vs u64)의 승격 신호(@12·@20 비영)도
  같은 통로다(`quantity_hi`/`price_hi`).
- stdlib + `.gersang_protocol` 상대 import 만 — YukTracker 가 벤더 사본으로 그대로 가져간다
  (`struct` 도 쓰지 않는다: 그쪽 import 허용 목록에 없다).
- 판매자명은 다른 플레이어의 이름이다. 원본은 로컬 원장에만, 문서·PR·테스트 출력에는 `mask_seller`.
"""
from __future__ import annotations

from dataclasses import dataclass

from .gersang_protocol import (
    MAGIC,
    MARKET_HEADER_LEN,
    MARKET_MAX_ROWS,
    MARKET_OPCODES,
    MARKET_ROW_LEN,
    market_row_count,
)

__all__ = [
    "MarketRow",
    "MarketPage",
    "parse_market_page",
    "mask_seller",
    "row_to_dict",
    "OBSERVED_PAGE_ROWS",
    "SELLER_LEN",
]

#: 관측된 페이지 크기(행) — anomaly `count_gt_page` 의 문턱이지 판별 조건이 아니다.
OBSERVED_PAGE_ROWS = 10
#: 판매자 필드 폭(B) — cp949, NUL 종료, 관측 ≤10B. NUL 뒤 잔여 바이트는 서버 버퍼 잔재(213/269행)
#: 라 첫 NUL 까지만 읽는다.
SELLER_LEN = 16
_SELLER_OFF = 24
_UNKNOWN40_OFF = 40


@dataclass(frozen=True)
class MarketRow:
    """목록 행 1개(48B). `*_hi` 는 @12·@20 의 4B(관측 전부 0 — u64 상위 또는 패딩, H-2609-09)."""
    listing_id: int
    item_id: int
    quantity: int
    quantity_hi: int
    price: int
    price_hi: int
    #: cp949, 첫 NUL 까지, 해독 불가 바이트는 U+FFFD.
    seller: str
    #: 16B 안에 NUL 이 있었는가 — False 면 필드 표의 falsifier("NUL 없음").
    seller_nul: bool
    #: @24 16B 원본(잔여 바이트 포함) — 원장·업로드에는 쓰지 않는다(`row_to_dict` 제외).
    seller_raw: bytes
    #: @40 u32 LE — 프레임 내 전 행 동일(D). 표시는 원바이트 hex(프로브·문서 표기와 동일).
    unknown40: int
    raw44: int
    #: @45 — 관측 1|2. UI 의 `기간`(장기/단기) 열과 대응 후보(H-2609-11, D) — 이름은 붙이지 않는다.
    flag45: int
    #: @46 — 관측 0~7(D).
    flag46: int
    raw47: int

    @property
    def quantity_u64(self) -> int:
        return self.quantity | (self.quantity_hi << 32)

    @property
    def price_u64(self) -> int:
        return self.price | (self.price_hi << 32)

    @property
    def unknown40_hex(self) -> str:
        """@40 원바이트 순서 hex 8자리 — `packet_market_probe`·PACKET-MARKET 표와 같은 표기."""
        return self.unknown40.to_bytes(4, "little").hex()


@dataclass(frozen=True)
class MarketPage:
    """프레임 1개 = 페이지 1개(행 0~N). 페이지 번호는 없다 — `listing_ids` 로 잇는다."""
    opcode: int
    total_pages: int
    count: int
    #: body[4] — 관측 전부 0(페이지 번호가 아니다). 0 이 아니면 anomaly `hdr4`.
    hdr4: int
    rows: tuple[MarketRow, ...]

    @property
    def listing_ids(self) -> tuple[int, ...]:
        return tuple(r.listing_id for r in self.rows)

    @property
    def listing_desc(self) -> bool:
        """등록 id 엄격 내림차순인가 — 기본 목록의 관찰(검색 응답은 아이템 묶음별이라 False 일 수
        있다 → anomaly 가 아니다, 표시용)."""
        ids = self.listing_ids
        return all(a > b for a, b in zip(ids, ids[1:]))

    @property
    def unknown40(self) -> int | None:
        return self.rows[0].unknown40 if self.rows else None

    @property
    def anomalies(self) -> tuple[str, ...]:
        """필드 표(PACKET-MARKET §4)의 falsifier 가 관측된 항목 — 수용은 하되 표시한다. 결정적 순서."""
        out: list[str] = []
        rows = self.rows
        if self.hdr4 != 0:
            out.append("hdr4")
        if self.count > OBSERVED_PAGE_ROWS:
            out.append("count_gt_page")
        if self.count > 0 and self.total_pages == 0:
            out.append("total_pages_zero")
        if any(r.quantity_hi for r in rows):
            out.append("quantity_hi")
        if any(r.price_hi for r in rows):
            out.append("price_hi")
        if any(not r.seller_nul for r in rows):
            out.append("seller_no_nul")
        if len({r.unknown40 for r in rows}) > 1:
            out.append("unknown40_varies")
        if any(r.raw44 for r in rows):
            out.append("raw44")
        if any(r.raw47 for r in rows):
            out.append("raw47")
        if any(r.flag45 not in (1, 2) for r in rows):
            out.append("flag45")
        if any(r.flag46 > 7 for r in rows):
            out.append("flag46")
        return tuple(out)


def _u32(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 4], "little")


def _parse_row(raw: bytes) -> MarketRow:
    seller_raw = bytes(raw[_SELLER_OFF:_SELLER_OFF + SELLER_LEN])
    nul = seller_raw.find(0)
    seller = seller_raw[:nul if nul >= 0 else SELLER_LEN].decode("cp949", errors="replace")
    return MarketRow(
        listing_id=_u32(raw, 0), item_id=_u32(raw, 4),
        quantity=_u32(raw, 8), quantity_hi=_u32(raw, 12),
        price=_u32(raw, 16), price_hi=_u32(raw, 20),
        seller=seller, seller_nul=nul >= 0, seller_raw=seller_raw,
        unknown40=_u32(raw, _UNKNOWN40_OFF),
        raw44=raw[44], flag45=raw[45], flag46=raw[46], raw47=raw[47],
    )


def parse_market_page(body: bytes) -> MarketPage | None:
    """완성 본문 → `MarketPage`. 구조가 관측된 배치(9 + 48×count·헤더)와 다르면 **None**.

    `count == 0`(빈 검색 결과, len 9)도 유효한 페이지다. `body[4]`·플래그·@12/@20 은 판별하지
    않고 `anomalies` 로 넘긴다. 절단·여분 바이트·행 수 불일치·상한 초과는 전부 None.
    """
    if len(body) < MARKET_HEADER_LEN or body[0] != MAGIC or body[3] != 0:
        return None
    opcode = int.from_bytes(body[1:3], "little")
    if opcode not in MARKET_OPCODES:
        return None
    count = market_row_count(body)
    if count > MARKET_MAX_ROWS or len(body) != MARKET_HEADER_LEN + MARKET_ROW_LEN * count:
        return None
    rows = tuple(
        _parse_row(body[MARKET_HEADER_LEN + k * MARKET_ROW_LEN:
                        MARKET_HEADER_LEN + (k + 1) * MARKET_ROW_LEN])
        for k in range(count))
    return MarketPage(opcode=opcode, total_pages=int.from_bytes(body[5:7], "little"),
                      count=count, hdr4=body[4], rows=rows)


def mask_seller(name: str) -> str:
    """판매자명 가림 — 첫·끝 글자만 남긴다(2자는 첫 글자 + ○). 문서·PR·테스트 출력용.
    `scripts/packet_market_probe.py --mask` 와 같은 규칙(그쪽이 이 함수를 쓴다)."""
    if len(name) <= 1:
        return name
    if len(name) == 2:
        return name[0] + "○"
    return name[0] + "○" * (len(name) - 2) + name[-1]


def row_to_dict(row: MarketRow, *, mask: bool = False) -> dict:
    """원장·explore·업로드 공용 직렬화 — `seller_raw`(잔여 바이트) 제외, `unknown40` 은 원바이트
    hex. `quantity_hi`/`price_hi` 는 0 이 정상값이라도 항상 넣는다(H-2609-09 판독 근거)."""
    return {
        "listing_id": row.listing_id,
        "item_id": row.item_id,
        "quantity": row.quantity,
        "quantity_hi": row.quantity_hi,
        "price": row.price,
        "price_hi": row.price_hi,
        "seller": mask_seller(row.seller) if mask else row.seller,
        "unknown40": row.unknown40_hex,
        "flag45": row.flag45,
        "flag46": row.flag46,
    }
