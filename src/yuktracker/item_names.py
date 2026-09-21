"""아이템 id → 이름 표 — 클라 리소스 `gersang.gcs` 의 `육의전 검색 기능 리스트` 추출 (2026-09-21, PR-Y2b).

왜 이 표인가
------------
육의전 목록 응답(`0x321f`, SEAssist PROCESS §3 H-2609-08)에는 아이템 **id** 만 있고 이름이 없다
(H-2609-07 기각). 소비자(미루봇 `!육의전 <아이템명>`)는 이름으로 찾으니 id→이름 표가 있어야 한다.
출처 세 후보(클라 리소스 / 라벨 누적 / 화면 OCR) 중 클라 리소스가 이겼다(2026-09-21 조사):
`C:\\AKInteractive\\Gersang\\gersang.gcs`(zlib 스트림 아카이브) 안의 `;\\t육의전 검색 기능 리스트` 표
(`아이템 코드\\t이름\\t이름 코드(GTS)`, 4,001행)가 콘솔 라벨 8/8 을 맞히고 관측 188 id 중 186 을 풀었다
(H-2609-10). 라벨 누적(8건)은 검증용으로만 남기고, OCR(한글 OCR 없음·import 경계)·웹(geota 403)은 탈락.

어떻게 찾나 — 오프셋이 아니라 내용으로
------------------------------------------
아카이브의 엔트리 이름은 난독화돼 있고 컨테이너 헤더 규칙을 다 알지 못한다(20번째 엔트리 뒤에서
어긋난다). 스트림 오프셋·순번은 패치마다 바뀐다. 그래서 **zlib 시그니처를 순차 스캔**해 스트림을
하나씩 inflate 하고, 선두 512B 에 마커(`육의전 검색 기능 리스트` cp949 또는 `#Item Code<TAB>Name<TAB>Name
Code`)가 있는 것만 전량 inflate 해 표로 읽는다 — 행 수가 `MIN_ROWS` 미만이면 미끼로 보고 계속 간다.
소비한 만큼 건너뛰므로 스트림 본문 안은 검색하지 않고(가짜 시그니처에 덜 흔들린다), 실측 1,247 스트림
64.8MB 를 0.5s 안에 훑는다. 메모리는 64KiB 단위 drain 이라 파일 크기와 무관하다. 순차 패스가 실패하면
시그니처 위치마다 독립으로 선두만 보는 2차 패스를 돈다(가짜 스트림이 진짜를 삼킨 병적 경우).

경계
----
- stdlib(zlib·json·hashlib) 만 — 관측기 import 경계(`tests/test_vendor.py`). `struct` 도 안 쓴다.
- **읽기 전용**: 파일을 열어 읽을 뿐 게임 프로세스·메모리·네트워크에 닿지 않는다. 게임이 떠 있어도 파일
  잠금이 없다(실측). 추출한 표(JSON)는 레포 밖(`%APPDATA%\\YukTracker\\item_names.json`)에만 둔다.
- 패치 드리프트: 캐시 유효성은 `(크기, mtime)` → 불일치면 앞 1MiB sha256(클라 3벌의 mtime 차이 흡수)
  → 그래도 다르면 재스캔. 마커가 사라지면 `ItemTableNotFound` — 관측기는 이름 없이(id 만) 올리고
  카운터로 드러낸다(PR-Y1b).

정규화 — 한 정의
-----------------
`display_name`: strip → 선두 `[M]` 한 번 제거 → strip(게임 UI 가 `[M]` 을 지운다 — 라벨 853 `봉인의돌`
은 표에 `[M]봉인의돌`). `[천권]`·`<삼족오>` 는 이름의 일부라 남긴다. `norm_name`: 공백 전부 제거 +
casefold — 대시보드 `item_name_norm`·봇 `_squash` 와 **같은 정의**여야 검색이 맞는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import zlib
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Optional

from .game_processes import game_pids, process_image_path
from .paths import item_table_cache_path
from .seassist.logger import get_logger

#: 파싱·정규화 규칙이 바뀌면 올린다 → 캐시 무효.
EXTRACTOR_VERSION = 1
CACHE_VERSION = 1
GCS_NAME = "gersang.gcs"
#: 아카이브 파일 헤더 8B(`gersang.gcs`·`system.gcs` 공통, 실측).
GCS_MAGIC = bytes.fromhex("17bc586401000010")
ENV_CLIENT_DIR = "YUKTRACKER_CLIENT_DIR"
DEFAULT_CLIENT_GLOBS: tuple[str, ...] = (r"C:\AKInteractive\Gersang*",)
#: zlib 스트림 헤더(CMF=0x78 + FLG) — 압축 레벨별 4종.
ZLIB_SIGS: tuple[bytes, ...] = (b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda")
#: 표 식별에 보는 inflate 선두 바이트.
HEAD_BYTES = 512
#: 스트림 끝을 찾을 때 버리는 단위 = 메모리 상한.
DRAIN_CHUNK = 1 << 16
#: 표 하나의 inflate 상한(현재 101KB).
MAX_TABLE_BYTES = 8 << 20
#: 아카이브 파일 상한(현재 8.4MB) — 넘으면 거부.
MAX_GCS_BYTES = 256 << 20
#: 마커만으로는 부족하다 — 구조 검증(현재 4,001행).
MIN_ROWS = 1000
#: 캐시 동일성 보조 키 — 앞 1MiB sha256.
HEAD_SHA_BYTES = 1 << 20
_M_PREFIX = "[M]"
_FILETIME_EPOCH_DELTA = 116444736000000000  # 1601-01-01 → 1970-01-01, 100ns 단위


class ItemTableNotFound(RuntimeError):
    """아카이브에서 육의전 검색 리스트 표를 찾지 못했다(마커·행 수 검증 실패)."""


@dataclass(frozen=True)
class TableSpec:
    key: str
    markers: tuple[bytes, ...]
    min_rows: int = MIN_ROWS


#: v1 은 육의전 검색 리스트 하나. 강화품 표(`DelSocketItem`) 등은 v1.1 에 `TableSpec` 으로 추가한다.
AUCTION_SPEC = TableSpec(
    "auction",
    ("육의전 검색 기능 리스트".encode("cp949"), b"#Item Code\tName\tName Code"),
)


@dataclass(frozen=True)
class ZlibStream:
    offset: int
    consumed: int
    head: bytes


@dataclass(frozen=True)
class ParseStats:
    rows: int
    skipped: int
    duplicate_ids: int
    decode_errors: int


@dataclass(frozen=True)
class TableSource:
    gcs_path: str
    gcs_size: int
    gcs_mtime_ns: int
    head_sha256: str
    gcs_sha256: str
    archive_ts: Optional[str]
    stream_offset: int
    rows: int
    skipped: int
    duplicate_ids: int
    decode_errors: int
    extracted_at: str
    extractor_version: int


# ─────────────────────────── 정규화 ───────────────────────────


def display_name(raw: str) -> str:
    """표의 원본 이름 → 게임 UI 가 보여주는 이름(선두 `[M]` 만 지운다)."""
    s = str(raw).strip()
    if s.startswith(_M_PREFIX):
        s = s[len(_M_PREFIX):].strip()
    return s


def norm_name(name: str) -> str:
    """검색 키 — 공백 전부 제거 + casefold. 대시보드 `item_name_norm`·봇 `_squash` 와 같은 정의."""
    return "".join(str(name).split()).casefold()


# ─────────────────────────── 표 객체 ───────────────────────────


@dataclass(frozen=True)
class ItemTable:
    """id → 원본 이름(`raw`) + 출처. 표시명·검색 인덱스는 생성 시 한 번 만든다."""
    raw: Mapping[int, str]
    source: TableSource
    _display: dict[int, str] = field(init=False, repr=False, compare=False, default_factory=dict)
    _norm: dict[str, list[int]] = field(init=False, repr=False, compare=False, default_factory=dict)

    def __post_init__(self) -> None:
        display = {int(i): display_name(n) for i, n in self.raw.items()}
        norm: dict[str, list[int]] = {}
        for i, n in display.items():
            norm.setdefault(norm_name(n), []).append(i)
        for ids in norm.values():
            ids.sort()
        object.__setattr__(self, "_display", display)
        object.__setattr__(self, "_norm", norm)

    def __len__(self) -> int:
        return len(self._display)

    def __contains__(self, item_id: object) -> bool:
        try:
            return int(item_id) in self._display  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    def lookup(self, item_id: int) -> Optional[str]:
        """표시명(`[M]` 제거) — 없으면 None."""
        return self._display.get(int(item_id))

    def lookup_raw(self, item_id: int) -> Optional[str]:
        return self.raw.get(int(item_id))

    def search(self, query: str, limit: int = 50) -> list[int]:
        """정규화 정확 일치 → 부분 일치 순, 각각 id 오름차순. 같은 이름의 여러 id 는 합집합."""
        q = norm_name(query)
        if not q:
            return []
        exact = list(self._norm.get(q, []))
        partial = sorted(i for key, ids in self._norm.items() if key != q and q in key for i in ids)
        return (exact + partial)[:max(0, int(limit))]

    def to_json_dict(self) -> dict:
        return {"version": CACHE_VERSION, "source": asdict(self.source),
                "items": {str(k): self.raw[k] for k in sorted(self.raw)}}

    @classmethod
    def from_json_dict(cls, d: Mapping) -> "ItemTable":
        if int(d.get("version", -1)) != CACHE_VERSION:
            raise ValueError(f"cache version {d.get('version')!r} != {CACHE_VERSION}")
        known = {f.name for f in fields(TableSource)}
        src = {k: v for k, v in dict(d["source"]).items() if k in known}
        missing = known - set(src)
        if missing:
            raise ValueError(f"cache source missing {sorted(missing)}")
        items = {int(k): str(v) for k, v in dict(d["items"]).items()}
        return cls(items, TableSource(**src))

    def write_json(self, path: Path) -> None:
        """tmp 에 쓰고 `os.replace` — 관측기 두 개가 같은 PC 에서 겹쳐도 반쪽 파일이 남지 않는다."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.to_json_dict(), ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def read_json(cls, path: Path) -> "ItemTable":
        return cls.from_json_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ─────────────────────────── 아카이브 스캔 ───────────────────────────


def archive_timestamp(data: bytes) -> Optional[str]:
    """파일 헤더 @32 의 FILETIME(아카이브 빌드 시각) → UTC ISO. magic 불일치·범위 밖이면 None."""
    if len(data) < 40 or data[:8] != GCS_MAGIC:
        return None
    ft = int.from_bytes(data[32:40], "little")
    unix = (ft - _FILETIME_EPOCH_DELTA) / 10_000_000
    if not (946_684_800 <= unix <= 4_102_444_800):   # 2000-01-01 ~ 2100-01-01
        return None
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _SignatureCursor:
    """시그니처 4종의 다음 위치를 캐시해 스캔이 O(4 × 파일 크기)로 끝나게 한다(매번 find 하면 2차식)."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._next: dict[bytes, int] = {sig: -2 for sig in ZLIB_SIGS}   # -2 미탐색, -1 없음

    def next_at_or_after(self, cursor: int) -> int:
        best = -1
        for sig, pos in self._next.items():
            if pos == -2 or (pos != -1 and pos < cursor):
                pos = self._data.find(sig, cursor)
                self._next[sig] = pos
            if pos != -1 and (best == -1 or pos < best):
                best = pos
        return best


def iter_zlib_streams(data: bytes, *, start: int = 0) -> Iterator[ZlibStream]:
    """순차 스캔 — 시그니처에서 inflate 를 시도해 성공하면 (offset, 소비 바이트, 선두 512B) 를 내고
    소비한 만큼 건너뛴다. 실패(가짜 시그니처·절단)면 1바이트 전진. 커서는 단조 증가."""
    sigs = _SignatureCursor(data)
    cursor = max(0, int(start))
    n = len(data)
    while cursor < n:
        off = sigs.next_at_or_after(cursor)
        if off == -1:
            return
        d = zlib.decompressobj()
        fed = 0
        head = b""
        try:
            while not d.eof:
                if d.unconsumed_tail:
                    piece = d.unconsumed_tail
                else:
                    piece = data[off + fed: off + fed + DRAIN_CHUNK]
                    if not piece:
                        raise zlib.error("truncated stream")
                    fed += len(piece)
                out = d.decompress(piece, DRAIN_CHUNK)
                if len(head) < HEAD_BYTES:
                    head += out[:HEAD_BYTES - len(head)]
        except zlib.error:
            cursor = off + 1
            continue
        consumed = fed - len(d.unused_data)
        yield ZlibStream(off, consumed, bytes(head))
        cursor = off + max(consumed, 1)


def _peek(data: bytes, offset: int) -> bytes:
    """스트림 선두 `HEAD_BYTES` 만 inflate(2차 패스용) — 실패면 빈 바이트."""
    try:
        return zlib.decompressobj().decompress(data[offset: offset + DRAIN_CHUNK], HEAD_BYTES)
    except zlib.error:
        return b""


def _inflate(data: bytes, offset: int, limit: int = MAX_TABLE_BYTES) -> Optional[bytes]:
    """`offset` 의 스트림을 끝까지 inflate. 상한 초과·불완전·오류면 None."""
    d = zlib.decompressobj()
    out = bytearray()
    pos = offset
    try:
        while not d.eof:
            piece = d.unconsumed_tail or data[pos: pos + DRAIN_CHUNK]
            if not d.unconsumed_tail:
                if not piece:
                    return None
                pos += len(piece)
            out += d.decompress(piece, DRAIN_CHUNK)
            if len(out) > limit:
                return None
    except zlib.error:
        return None
    return bytes(out)


def _decode(raw: bytes) -> tuple[str, int]:
    """BOM 이면 UTF-8, 아니면 cp949(실측) — 해독 불가 바이트 수를 함께."""
    if raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        text = raw.decode("cp949", errors="replace")
    return text, text.count("\ufffd")


def parse_item_rows(text: str) -> tuple[dict[int, str], ParseStats]:
    """`;`/`#` 주석·빈 줄 skip, 탭 분리 `id<TAB>이름[<TAB>…]`. 첫 id 우선(중복은 센다), 이름은 원본 보존."""
    rows: dict[int, str] = {}
    skipped = dups = 0
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in ";#":
            continue
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0].strip().isdigit() or not parts[1].strip():
            skipped += 1
            continue
        item_id = int(parts[0].strip())
        if item_id in rows:
            dups += 1
            continue
        rows[item_id] = parts[1]
    return rows, ParseStats(rows=len(rows), skipped=skipped, duplicate_ids=dups, decode_errors=0)


def _accept(data: bytes, offset: int, spec: TableSpec) -> Optional[tuple[dict[int, str], ParseStats]]:
    raw = _inflate(data, offset)
    if raw is None:
        return None
    text, errs = _decode(raw)
    rows, stats = parse_item_rows(text)
    if stats.rows < spec.min_rows:
        return None
    return rows, replace(stats, decode_errors=errs)


def extract_item_table_bytes(data: bytes, *, spec: TableSpec = AUCTION_SPEC
                             ) -> tuple[dict[int, str], ParseStats, int]:
    """아카이브 바이트 → (id→원본 이름, 통계, 스트림 offset). 순수 함수 — 테스트의 주 대상."""
    if len(data) > MAX_GCS_BYTES:
        raise ItemTableNotFound(f"아카이브가 상한({MAX_GCS_BYTES}B)을 넘는다: {len(data)}B")
    seen = candidates = 0
    for st in iter_zlib_streams(data):
        seen += 1
        if not any(m in st.head for m in spec.markers):
            continue
        candidates += 1
        hit = _accept(data, st.offset, spec)
        if hit is not None:
            return hit[0], hit[1], st.offset
    # 2차 패스 — 시그니처 위치마다 독립으로 선두만 본다(가짜 스트림이 진짜를 삼켰을 때).
    sigs = _SignatureCursor(data)
    cursor = 0
    fallback = fb_candidates = 0
    while True:
        off = sigs.next_at_or_after(cursor)
        if off == -1:
            break
        cursor = off + 1
        fallback += 1
        if not any(m in _peek(data, off) for m in spec.markers):
            continue
        fb_candidates += 1
        hit = _accept(data, off, spec)
        if hit is not None:
            return hit[0], hit[1], off
    raise ItemTableNotFound(
        f"{spec.key} 표 없음 — 스트림 {seen}개 중 마커 후보 {candidates}개"
        f"(2차 시그니처 {fallback}개 중 후보 {fb_candidates}개), 행 ≥{spec.min_rows} 인 표 0개")


def extract_item_table(gcs_path: Path, *, spec: TableSpec = AUCTION_SPEC) -> ItemTable:
    """파일 → `ItemTable`(출처 스탬프 포함). 읽기 전용."""
    gcs_path = Path(gcs_path)
    data = gcs_path.read_bytes()
    rows, stats, offset = extract_item_table_bytes(data, spec=spec)
    st = gcs_path.stat()
    source = TableSource(
        gcs_path=str(gcs_path), gcs_size=st.st_size, gcs_mtime_ns=st.st_mtime_ns,
        head_sha256=hashlib.sha256(data[:HEAD_SHA_BYTES]).hexdigest(),
        gcs_sha256=hashlib.sha256(data).hexdigest(), archive_ts=archive_timestamp(data),
        stream_offset=offset, rows=stats.rows, skipped=stats.skipped,
        duplicate_ids=stats.duplicate_ids, decode_errors=stats.decode_errors,
        extracted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        extractor_version=EXTRACTOR_VERSION,
    )
    return ItemTable(rows, source)


# ─────────────────────────── 클라 폴더 탐색 ───────────────────────────


def discover_client_dirs(explicit: Optional[str | Path] = None, *,
                         env: Optional[Mapping[str, str]] = None,
                         pids: Callable[[], Iterable[int]] = game_pids,
                         image_path: Callable[[int], Optional[str]] = process_image_path,
                         glob_roots: Iterable[str] = DEFAULT_CLIENT_GLOBS) -> list[Path]:
    """`gersang.gcs` 가 있는 클라 폴더 후보 — 명시 → env → 실행 중 gersang.exe 의 폴더 → glob 순,
    중복 제거. 어느 단계도 던지지 않는다."""
    env = os.environ if env is None else env
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit))
    v = str(env.get(ENV_CLIENT_DIR, "") or "").strip()
    if v:
        cands.append(Path(v))
    try:
        for pid in pids():
            try:
                p = image_path(int(pid))
            except Exception:
                p = None
            if p:
                cands.append(Path(p).parent)
    except Exception:
        pass
    for root in glob_roots:
        try:
            r = Path(root)
            cands.extend(sorted(r.parent.glob(r.name)))
        except Exception:
            pass
    out: list[Path] = []
    seen: set[str] = set()
    for c in cands:
        try:
            key = os.path.normcase(str(c.resolve()))
            if key in seen:
                continue
            seen.add(key)
            if (c / GCS_NAME).is_file():
                out.append(c)
        except Exception:
            continue
    return out


def find_gcs(client_dir: Optional[str | Path] = None, **kw) -> Optional[Path]:
    dirs = discover_client_dirs(client_dir, **kw)
    return dirs[0] / GCS_NAME if dirs else None


# ─────────────────────────── 캐시 · 로드 ───────────────────────────


def _read_cache(path: Path) -> Optional[ItemTable]:
    try:
        table = ItemTable.read_json(path)
    except Exception:
        return None
    if table.source.extractor_version != EXTRACTOR_VERSION:
        return None
    return table


def _cache_matches(cached: ItemTable, gcs: Path) -> bool:
    """(크기, mtime) 일치 → 그대로. 크기만 같으면 앞 1MiB sha 로 재확인(다른 사본·복사된 mtime)."""
    try:
        st = gcs.stat()
        src = cached.source
        if st.st_size != src.gcs_size:
            return False
        if (st.st_mtime_ns == src.gcs_mtime_ns
                and os.path.normcase(str(Path(src.gcs_path))) == os.path.normcase(str(gcs))):
            return True
        with open(gcs, "rb") as f:
            head = f.read(HEAD_SHA_BYTES)
        return hashlib.sha256(head).hexdigest() == src.head_sha256
    except Exception:
        return False


def load_item_table(*, client_dir: Optional[str | Path] = None, gcs_path: Optional[str | Path] = None,
                    cache_path: Optional[str | Path] = None, refresh: bool = False,
                    use_cache: bool = True,
                    extractor: Callable[[Path], ItemTable] = extract_item_table,
                    finder: Callable[..., Optional[Path]] = find_gcs) -> Optional[ItemTable]:
    """표 1개 — 캐시가 맞으면 캐시, 아니면 추출 후 캐시 갱신. **절대 던지지 않는다.**

    None 은 "클라 폴더도 캐시도 없다" 일 때만. gcs 가 없어도 캐시가 있으면 캐시(경고 1줄), 추출에
    실패하면 캐시(있으면), 캐시 쓰기에 실패해도 표는 돌려준다.
    """
    log = get_logger()
    cache = Path(cache_path) if cache_path else item_table_cache_path()
    gcs = Path(gcs_path) if gcs_path else finder(client_dir)
    cached = _read_cache(cache) if (use_cache and not refresh) else None
    if gcs is None or not Path(gcs).is_file():
        if cached is not None:
            log.warning("[아이템표] 클라 폴더(%s) 미발견 — 캐시 사용 %s (%d건)", GCS_NAME, cache, len(cached))
            return cached
        log.warning("[아이템표] 클라 폴더(%s) 미발견 — 아이템명 없이 진행(--client-dir / %s)", GCS_NAME, ENV_CLIENT_DIR)
        return None
    if cached is not None and _cache_matches(cached, gcs):
        return cached
    try:
        table = extractor(gcs)
    except Exception as e:
        log.warning("[아이템표] 추출 실패 %s: %s", gcs, e)
        return cached
    if use_cache:
        try:
            table.write_json(cache)
        except Exception as e:
            log.warning("[아이템표] 캐시 쓰기 실패 %s: %s", cache, e)
    return table


__all__ = [
    "AUCTION_SPEC", "CACHE_VERSION", "DEFAULT_CLIENT_GLOBS", "ENV_CLIENT_DIR", "EXTRACTOR_VERSION",
    "GCS_MAGIC", "GCS_NAME", "HEAD_BYTES", "MIN_ROWS", "ItemTable", "ItemTableNotFound", "ParseStats",
    "TableSource", "TableSpec", "ZlibStream", "archive_timestamp", "discover_client_dirs", "display_name",
    "extract_item_table", "extract_item_table_bytes", "find_gcs", "iter_zlib_streams", "load_item_table",
    "norm_name", "parse_item_rows",
]
