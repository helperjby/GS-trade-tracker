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
소비한 만큼 건너뛰므로 스트림 본문 안은 검색하지 않는다(가짜 시그니처에 덜 흔들린다). 실측(2026-09-22,
클라 사본 3벌 내용 동일): 아카이브 8,399,234B · 스트림 1,247개 · 표 4,001행, 추출 0.12s · 전체 sha256 4ms.
**아카이브 전체를 메모리에 읽는다**(파일 크기만큼, 상한 `MAX_GCS_BYTES` 는 읽기 전에 stat 으로 본다) —
inflate 출력만 64KiB 단위 drain 이라 스트림 크기와 무관하다. 순차 패스가 실패하면 시그니처 위치마다
독립으로 선두만 보는 2차 패스를 돈다(가짜 스트림이 진짜를 삼킨 병적 경우).

경계
----
- stdlib(zlib·json·hashlib·tempfile) 만 — 관측기 import 경계(`tests/test_vendor.py`). `struct` 도 안 쓴다.
- **읽기 전용**: 파일을 열어 읽을 뿐 게임 프로세스·메모리·네트워크에 닿지 않는다. 게임이 떠 있어도 파일
  잠금이 없다(실측). 추출한 표(JSON)는 레포 밖(`%APPDATA%\\YukTracker\\item_names.json`)에만 둔다.
- 패치 드리프트: 캐시 유효성 = 크기 일치 + **같은 경로면 mtime 일치, 다른 경로(클라 사본 3벌)면 전체 sha256
  일치** — 그 외는 재스캔(0.12s). 크기가 같은 패치도 mtime 이 바뀌므로 잡는다. 마커가 사라지면
  `ItemTableNotFound` — 관측기는 이름 없이(id 만) 올리고 카운터로 드러낸다(PR-Y1b).
- 클라 폴더: `--client-dir` 또는 `%YUKTRACKER_CLIENT_DIR%` 로 **고정**하면 그 폴더만 본다(gcs 가 없으면
  다른 클라로 조용히 넘어가지 않고 미발견). 고정이 없을 때만 실행 중 `gersang.exe` 의 폴더 →
  `C:\\AKInteractive\\Gersang*`. 시각은 전부 UTC `YYYY-MM-DDTHH:MM:SSZ` 한 형식(`archive_ts`·`extracted_at`).

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
import tempfile
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
#: 캐시 JSON 스키마 — 2: `head_sha256` 제거(전체 `gcs_sha256` 로 대체), `extracted_at` 도 `Z` 형식.
CACHE_VERSION = 2
GCS_NAME = "gersang.gcs"
#: 아카이브 파일 헤더 8B(`gersang.gcs`·`system.gcs` 공통, 실측).
GCS_MAGIC = bytes.fromhex("17bc586401000010")
ENV_CLIENT_DIR = "YUKTRACKER_CLIENT_DIR"
DEFAULT_CLIENT_GLOBS: tuple[str, ...] = (r"C:\AKInteractive\Gersang*",)
#: zlib 스트림 헤더(CMF=0x78 + FLG) — 압축 레벨별 4종.
ZLIB_SIGS: tuple[bytes, ...] = (b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda")
#: 표 식별에 보는 inflate 선두 바이트.
HEAD_BYTES = 512
#: 스트림 끝을 찾을 때 버리는 단위 = inflate 출력 메모리 상한.
DRAIN_CHUNK = 1 << 16
#: 표 하나의 inflate 상한(현재 101KB).
MAX_TABLE_BYTES = 8 << 20
#: 아카이브 파일 상한(현재 8.4MB) — 넘으면 읽지 않고 거부.
MAX_GCS_BYTES = 256 << 20
#: 마커만으로는 부족하다 — 구조 검증(현재 4,001행).
MIN_ROWS = 1000
_M_PREFIX = "[M]"
_FILETIME_EPOCH_DELTA = 116444736000000000  # 1601-01-01 → 1970-01-01, 100ns 단위
_ISO_Z = "%Y-%m-%dT%H:%M:%SZ"


class ItemTableNotFound(RuntimeError):
    """아카이브에서 육의전 검색 리스트 표를 찾지 못했다(마커·행 수 검증 실패·크기 상한)."""


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
    """id → 원본 이름(`raw`) + 출처. 표시명·검색 인덱스는 생성 시 한 번 만든다.

    `raw` 는 생성 때 한 번 `dict[int, str]` 로 정규화한다 — JSON 의 str 키·임의 Mapping 을 넣어도
    `lookup`·`lookup_raw`·`to_json_dict` 가 같은 키를 본다.
    """
    raw: Mapping[int, str]
    source: TableSource
    _display: dict[int, str] = field(init=False, repr=False, compare=False, default_factory=dict)
    _norm: dict[str, list[int]] = field(init=False, repr=False, compare=False, default_factory=dict)

    def __post_init__(self) -> None:
        raw = {int(i): str(n) for i, n in self.raw.items()}
        display = {i: display_name(n) for i, n in raw.items()}
        norm: dict[str, list[int]] = {}
        for i, n in display.items():
            norm.setdefault(norm_name(n), []).append(i)
        for ids in norm.values():
            ids.sort()
        object.__setattr__(self, "raw", raw)
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
        return cls(dict(d["items"]), TableSource(**src))

    def write_json(self, path: Path) -> None:
        """고유 이름의 tmp(`mkstemp`, 같은 폴더)에 쓰고 `os.replace` — 관측기 두 개가 같은 PC 에서 겹쳐도
        서로의 tmp 를 건드리지 않고 반쪽 파일이 남지 않는다. 실패하면 tmp 를 지우고 던진다.
        폴더는 여기서 만든다(`paths.app_dir` 은 순수)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(self.to_json_dict(), ensure_ascii=False, indent=1) + "\n")
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @classmethod
    def read_json(cls, path: Path) -> "ItemTable":
        return cls.from_json_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ─────────────────────────── 아카이브 스캔 ───────────────────────────


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_ISO_Z)


def archive_timestamp(data: bytes) -> Optional[str]:
    """파일 헤더 @32 의 FILETIME(아카이브 빌드 시각) → UTC ISO(`Z`). magic 불일치·범위 밖이면 None."""
    if len(data) < 40 or data[:8] != GCS_MAGIC:
        return None
    ft = int.from_bytes(data[32:40], "little")
    unix = (ft - _FILETIME_EPOCH_DELTA) / 10_000_000
    if not (946_684_800 <= unix <= 4_102_444_800):   # 2000-01-01 ~ 2100-01-01
        return None
    return _utc_iso(datetime.fromtimestamp(unix, tz=timezone.utc))


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


def iter_zlib_streams(data: bytes) -> Iterator[ZlibStream]:
    """순차 스캔 — 시그니처에서 inflate 를 시도해 성공하면 (offset, 소비 바이트, 선두 512B) 를 내고
    소비한 만큼 건너뛴다. 실패(가짜 시그니처·절단)면 1바이트 전진. 커서는 단조 증가."""
    sigs = _SignatureCursor(data)
    cursor = 0
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
    """`;`/`#` 주석·빈 줄 skip, 탭 분리 `id<TAB>이름[<TAB>…]`. 첫 id 우선(중복은 센다), 이름은 원본 보존.

    id 는 **ASCII 숫자만**(`str.isdigit()` 은 `²`·`①` 같은 cp949 문자에도 참이라 `int()` 가 던진다 —
    그런 행 하나가 표 전체를 잃게 하지 않고 skipped 로 센다)."""
    rows: dict[int, str] = {}
    skipped = dups = 0
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in ";#":
            continue
        parts = line.split("\t")
        key = parts[0].strip()
        if len(parts) < 2 or not (key.isascii() and key.isdigit()) or not parts[1].strip():
            skipped += 1
            continue
        item_id = int(key)
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
    """파일 → `ItemTable`(출처 스탬프 포함). 읽기 전용.

    크기 상한은 읽기 **전에** stat 으로 보고, 읽은 뒤 다시 stat 해 읽는 동안 파일이 바뀌었으면(패치 중)
    던진다 — 새 (크기, mtime) 에 옛 표가 찍히는 캐시 오염을 막는다. 스탬프는 읽기 전 stat 값."""
    gcs_path = Path(gcs_path)
    st = gcs_path.stat()
    if st.st_size > MAX_GCS_BYTES:
        raise ItemTableNotFound(f"아카이브가 상한({MAX_GCS_BYTES}B)을 넘는다: {st.st_size}B")
    data = gcs_path.read_bytes()
    after = gcs_path.stat()
    if len(data) != st.st_size or (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
        raise RuntimeError(f"{gcs_path} 가 읽는 동안 바뀌었다(패치 중?) — 다음 시작에 다시 읽는다")
    rows, stats, offset = extract_item_table_bytes(data, spec=spec)
    source = TableSource(
        gcs_path=str(gcs_path), gcs_size=st.st_size, gcs_mtime_ns=st.st_mtime_ns,
        gcs_sha256=hashlib.sha256(data).hexdigest(), archive_ts=archive_timestamp(data),
        stream_offset=offset, rows=stats.rows, skipped=stats.skipped,
        duplicate_ids=stats.duplicate_ids, decode_errors=stats.decode_errors,
        extracted_at=_utc_iso(datetime.now(timezone.utc)),
        extractor_version=EXTRACTOR_VERSION,
    )
    return ItemTable(rows, source)


# ─────────────────────────── 클라 폴더 탐색 ───────────────────────────


def _expand(p: str | Path) -> Path:
    """`%VAR%`·`$VAR`·`~` 확장 — `ledger_paths` 가 사용자 경로에 하는 것과 같은 처리."""
    return Path(os.path.expandvars(str(p))).expanduser()


def _has_gcs(d: Path) -> bool:
    try:
        return (d / GCS_NAME).is_file()
    except Exception:
        return False


def pinned_client_dir(explicit: Optional[str | Path] = None, *,
                      env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """사용자가 고정한 클라 폴더 — `--client-dir`(명시)가 있으면 그것, 없으면 `%YUKTRACKER_CLIENT_DIR%`,
    둘 다 없으면 None. 있으면 탐색은 그 폴더만 본다."""
    if explicit:
        return _expand(explicit)
    env = os.environ if env is None else env
    v = str(env.get(ENV_CLIENT_DIR, "") or "").strip()
    return _expand(v) if v else None


def discover_client_dirs(explicit: Optional[str | Path] = None, *,
                         env: Optional[Mapping[str, str]] = None,
                         pids: Callable[[], Iterable[int]] = game_pids,
                         image_path: Callable[[int], Optional[str]] = process_image_path,
                         glob_roots: Iterable[str] = DEFAULT_CLIENT_GLOBS) -> list[Path]:
    """`gersang.gcs` 가 있는 클라 폴더 후보. 고정 폴더(`pinned_client_dir`)가 있으면 **그것만** — gcs 가
    없으면 빈 목록(다른 클라의 표를 조용히 캐시에 쓰지 않는다). 고정이 없으면 실행 중 gersang.exe 의 폴더 →
    glob 순, 중복 제거. 어느 단계도 던지지 않는다."""
    pinned = pinned_client_dir(explicit, env=env)
    if pinned is not None:
        return [pinned] if _has_gcs(pinned) else []
    cands: list[Path] = []
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
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        if _has_gcs(c):
            out.append(c)
    return out


def find_gcs(client_dir: Optional[str | Path] = None, **kw) -> Optional[Path]:
    dirs = discover_client_dirs(client_dir, **kw)
    return dirs[0] / GCS_NAME if dirs else None


# ─────────────────────────── 캐시 · 로드 ───────────────────────────


@dataclass(frozen=True)
class LoadResult:
    """`load_item_table_result` 의 결과 — 표가 어디서 왔는지(도구의 hit/miss 표시·게이트 종료 코드용).

    origin: ``cache``(gcs 와 일치하는 캐시) · ``extracted``(재스캔) · ``cache-fallback``(gcs 미발견·추출 실패라
    묵은 캐시) · ``none``(표 없음). ``gcs_path`` 가 None 이면 클라 폴더를 못 찾은 것, 아니면 그 파일을 봤다.
    ``error`` 는 미발견·추출 실패 사유 한 줄(없으면 None)."""
    table: Optional[ItemTable]
    origin: str
    gcs_path: Optional[Path]
    error: Optional[str] = None


def _read_cache(path: Path) -> Optional[ItemTable]:
    try:
        table = ItemTable.read_json(path)
    except Exception:
        return None
    if table.source.extractor_version != EXTRACTOR_VERSION:
        return None
    return table


def _same_path(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(DRAIN_CHUNK * 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_matches(cached: ItemTable, gcs: Path) -> bool:
    """크기가 다르면 아니다. 같은 경로면 mtime 까지 같아야 한다(같은 크기 패치도 mtime 은 바뀐다 — 재스캔
    0.12s). 다른 경로(클라 사본 3벌·복사된 mtime)면 전체 sha256 이 같을 때만 같은 표다(실측 4ms)."""
    try:
        st = gcs.stat()
        src = cached.source
        if st.st_size != src.gcs_size:
            return False
        if _same_path(src.gcs_path, gcs):
            return st.st_mtime_ns == src.gcs_mtime_ns
        return _sha256_file(gcs) == src.gcs_sha256
    except Exception:
        return False


def _missing_reason(explicit_gcs: Optional[Path], client_dir: Optional[str | Path]) -> str:
    if explicit_gcs is not None:
        return f"{explicit_gcs} 없음"
    pinned = pinned_client_dir(client_dir)
    if pinned is not None:
        return f"지정 클라 폴더 {pinned} 에 {GCS_NAME} 없음(--client-dir / %{ENV_CLIENT_DIR}%)"
    return (f"클라 폴더({GCS_NAME}) 미발견 — 실행 중 gersang.exe·{', '.join(DEFAULT_CLIENT_GLOBS)} 에 없음"
            f"(--client-dir / %{ENV_CLIENT_DIR}% 로 지정)")


def load_item_table_result(*, client_dir: Optional[str | Path] = None, gcs_path: Optional[str | Path] = None,
                           cache_path: Optional[str | Path] = None, refresh: bool = False,
                           use_cache: bool = True,
                           extractor: Callable[[Path], ItemTable] = extract_item_table,
                           finder: Callable[..., Optional[Path]] = find_gcs) -> LoadResult:
    """표 1개 + 출처. **절대 던지지 않는다.**

    - 캐시가 gcs 와 일치하면(`_cache_matches`) 캐시 — `refresh=True` 는 이 단축만 건너뛴다.
    - 아니면 추출 후 캐시 갱신(`use_cache`). 캐시 쓰기에 실패해도 표는 돌려준다.
    - gcs 미발견·추출 실패면 캐시가 있으면 그것(origin ``cache-fallback``, 경고 1줄), 없으면 table None.
      `refresh` 여도 폴백은 산다 — 재스캔 요청이 표를 통째로 잃게 하지 않는다.
    """
    log = get_logger()
    cache = Path(cache_path) if cache_path else item_table_cache_path()
    cached = _read_cache(cache) if use_cache else None
    if gcs_path:
        gcs: Optional[Path] = Path(gcs_path)
    else:
        try:
            gcs = finder(client_dir)
        except Exception:
            gcs = None
    if gcs is None or not gcs.is_file():
        where = _missing_reason(gcs if gcs_path else None, client_dir)
        if cached is not None:
            log.warning("[아이템표] %s — 캐시 사용 %s (%d건)", where, cache, len(cached))
            return LoadResult(cached, "cache-fallback", None, where)
        log.warning("[아이템표] %s — 아이템명 없이 진행", where)
        return LoadResult(None, "none", None, where)
    if cached is not None and not refresh and _cache_matches(cached, gcs):
        return LoadResult(cached, "cache", gcs, None)
    try:
        table = extractor(gcs)
    except Exception as e:
        msg = f"추출 실패 {gcs}: {e}"
        log.warning("[아이템표] %s%s", msg, " — 캐시 사용" if cached is not None else "")
        return LoadResult(cached, "cache-fallback" if cached is not None else "none", gcs, msg)
    if use_cache:
        try:
            table.write_json(cache)
        except Exception as e:
            log.warning("[아이템표] 캐시 쓰기 실패 %s: %s", cache, e)
    return LoadResult(table, "extracted", gcs, None)


def load_item_table(**kw) -> Optional[ItemTable]:
    """`load_item_table_result(**kw).table` — 표만 필요한 호출자용(관측기 PR-Y1b). None 은 클라 폴더도
    캐시도 없거나, 추출에 실패했는데 캐시도 없을 때."""
    return load_item_table_result(**kw).table


__all__ = [
    "AUCTION_SPEC", "CACHE_VERSION", "DEFAULT_CLIENT_GLOBS", "ENV_CLIENT_DIR", "EXTRACTOR_VERSION",
    "GCS_MAGIC", "GCS_NAME", "HEAD_BYTES", "MIN_ROWS", "ItemTable", "ItemTableNotFound", "LoadResult",
    "ParseStats", "TableSource", "TableSpec", "ZlibStream", "archive_timestamp", "discover_client_dirs",
    "display_name", "extract_item_table", "extract_item_table_bytes", "find_gcs", "iter_zlib_streams",
    "load_item_table", "load_item_table_result", "norm_name", "parse_item_rows", "pinned_client_dir",
]
