"""아이템 id→이름 표 — `gersang.gcs` 순차 zlib 스캔·마커 식별·정규화·캐시·클라 탐색·덤프 도구 (PR-Y2b).

전부 **합성 아카이브**다 — 실제 클라 파일은 건드리지 않는다(경로는 항상 명시 주입). 판매자명 없음.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zlib
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from yuktracker import item_names as IN

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "dump_item_names.py"
#: 2026-09-21T02:04:11Z — 실측 아카이브 빌드 시각과 같은 값(FILETIME, 100ns 단위).
FILETIME = int(datetime(2026, 9, 21, 2, 4, 11, tzinfo=timezone.utc).timestamp() * 10_000_000) + 116444736000000000

HEADER_LINES = [
    ";\t육의전 검색 기능 리스트",
    ";=======================================================================",
    ";",
    ";아이템 코드\t이름\t이름 코드(GTS)",
    "#Item Code\tName\tName Code",
]
#: 라벨 앵커 8쌍(게임 아이템명) + 정규화·중복·불량 행 케이스.
BASE_ROWS = [
    "853\t[M]봉인의돌\t1234", "2204\t청색 정기의 돌\t2", "3506\t봉인의서\t6744", "5317\t시간의금화\t13522",
    "6215\t작은바람의속성석\t16317", "6342\t흑호의발톱\t8", "8245\t[천권] 주술 비법\t9",
    "13468\t파손된 집행 견갑\t10",
    "986\t떡국\t3", "3085\t떡국\t4", "20\t 앞뒤공백 \t5",
    "abc\t잘못된 행\t6", "10\t\t0", "11\t이름만", "853\t중복id\t7",
]
VALID_BASE = 12   # 앵커 8 + 떡국 2 + 앞뒤공백 + 이름만
SKIPPED_BASE = 2  # abc · 빈 이름
DUP_BASE = 1


def table_text(rows=BASE_ROWS, *, n_generated: int = 1000, start: int = 10_000) -> str:
    lines = list(HEADER_LINES) + list(rows) + [f"{start + i}\t생성{i}\t{i}" for i in range(n_generated)]
    return "\r\n".join(lines) + "\r\n"


def gcs_bytes(payloads, levels=(1, 6, 9), *, magic: bytes = IN.GCS_MAGIC, filetime: int = FILETIME) -> bytes:
    """48B 헤더(magic + @32 FILETIME) + [가짜 33B 엔트리 헤더 + zlib 스트림]…"""
    hdr = bytearray(48)
    hdr[:8] = magic
    hdr[32:40] = filetime.to_bytes(8, "little")
    out = bytes(hdr)
    for i, payload in enumerate(payloads):
        out += b"\x00" * 33 + zlib.compress(payload, levels[i % len(levels)])
    return out


UNRELATED = "\r\n".join(f"{i}\t몬스터{i}\t{i * 3}" for i in range(1500)).encode("cp949")
DECOY = "\r\n".join(HEADER_LINES + ["1\t식혜\t1", "2\t수정과\t2", "3\t잡과병\t3"]).encode("cp949")
TABLE = table_text().encode("cp949")


class ZlibScanTest(unittest.TestCase):
    def test_streams_in_file_order_with_mixed_levels(self) -> None:
        payloads = [b"alpha" * 100, b"beta" * 300, b"gamma" * 50]
        data = gcs_bytes(payloads)
        streams = list(IN.iter_zlib_streams(data))
        self.assertEqual(len(streams), 3)
        self.assertEqual([s.offset for s in streams], sorted(s.offset for s in streams))
        for s, p, lvl in zip(streams, payloads, (1, 6, 9)):
            self.assertEqual(s.head, p[:IN.HEAD_BYTES])
            self.assertEqual(s.consumed, len(zlib.compress(p, lvl)))
            self.assertEqual(data[s.offset:s.offset + 2], zlib.compress(p, lvl)[:2])

    def test_fake_signature_in_gap_advances_one_byte(self) -> None:
        p1, p2 = b"first" * 80, b"second" * 90
        data = gcs_bytes([p1]) + b"\x78\x9c\xff\xff\xff\xff" + zlib.compress(p2, 6)
        streams = list(IN.iter_zlib_streams(data))
        self.assertEqual([s.head for s in streams], [p1[:512], p2[:512]])

    def test_truncated_stream_at_eof_is_skipped(self) -> None:
        p1, p2 = b"first" * 80, b"second" * 90
        data = gcs_bytes([p1]) + zlib.compress(p2, 6)[:-5]
        streams = list(IN.iter_zlib_streams(data))
        self.assertEqual([s.head for s in streams], [p1[:512]])

    def test_empty_and_no_signature(self) -> None:
        self.assertEqual(list(IN.iter_zlib_streams(b"")), [])
        self.assertEqual(list(IN.iter_zlib_streams(b"\x00" * 1000)), [])


class ExtractTest(unittest.TestCase):
    def test_table_found_by_marker_not_position(self) -> None:
        data = gcs_bytes([UNRELATED, DECOY, TABLE])
        rows, stats, offset = IN.extract_item_table_bytes(data)
        self.assertEqual(rows[853], "[M]봉인의돌")
        self.assertEqual(rows[20], " 앞뒤공백 ")
        self.assertEqual((stats.rows, stats.skipped, stats.duplicate_ids, stats.decode_errors),
                         (VALID_BASE + 1000, SKIPPED_BASE, DUP_BASE, 0))
        third = list(IN.iter_zlib_streams(data))[2].offset
        self.assertEqual(offset, third)
        self.assertEqual(data[offset:offset + 2], zlib.compress(TABLE, 9)[:2])

    def test_not_found_raises_with_counts(self) -> None:
        with self.assertRaisesRegex(IN.ItemTableNotFound, "스트림 2개") as cm:
            IN.extract_item_table_bytes(gcs_bytes([UNRELATED, DECOY]))
        self.assertIn("후보 1개", str(cm.exception))   # 미끼는 마커가 있어 후보로는 센다

    def test_rows_comments_malformed_dup_and_stats(self) -> None:
        rows, stats = IN.parse_item_rows(table_text())
        self.assertEqual(stats, IN.ParseStats(rows=VALID_BASE + 1000, skipped=SKIPPED_BASE,
                                              duplicate_ids=DUP_BASE, decode_errors=0))
        self.assertEqual(rows[853], "[M]봉인의돌", "첫 id 가 이긴다")
        self.assertEqual(rows[11], "이름만")
        self.assertNotIn(10, rows)

    def test_utf8_bom_variant_decodes(self) -> None:
        data = gcs_bytes([b"\xef\xbb\xbf" + table_text().encode("utf-8")])
        rows, stats, _ = IN.extract_item_table_bytes(data)
        self.assertEqual(rows[2204], "청색 정기의 돌")
        self.assertEqual(stats.decode_errors, 0)

    def test_undecodable_bytes_are_counted_not_fatal(self) -> None:
        broken = TABLE.replace("떡국".encode("cp949"), b"\xff\xfe", 1)
        rows, stats, _ = IN.extract_item_table_bytes(gcs_bytes([broken]))
        self.assertGreaterEqual(stats.decode_errors, 1)
        self.assertEqual(rows[3506], "봉인의서")

    def test_archive_timestamp_from_header_and_none_on_bad_magic(self) -> None:
        self.assertEqual(IN.archive_timestamp(gcs_bytes([TABLE])), "2026-09-21T02:04:11Z")
        self.assertIsNone(IN.archive_timestamp(gcs_bytes([TABLE], magic=b"\x00" * 8)))
        self.assertIsNone(IN.archive_timestamp(gcs_bytes([TABLE], filetime=0)))
        self.assertIsNone(IN.archive_timestamp(b"\x17\xbc"))

    def test_fallback_pass_finds_table_swallowed_by_a_stored_outer_stream(self) -> None:
        # 저장(level 0) 스트림 안에 진짜 표 스트림이 통째로 실린 병적 배치 — 순차 패스는 바깥 스트림을
        # 소비하며 안을 건너뛰지만, 2차 패스가 시그니처 위치마다 독립으로 선두를 봐서 찾는다.
        inner = zlib.compress(TABLE, 6)
        outer = zlib.compress(inner, 0)
        data = gcs_bytes([]) + b"\x00" * 33 + outer
        rows, stats, offset = IN.extract_item_table_bytes(data)
        self.assertEqual(rows[853], "[M]봉인의돌")
        self.assertEqual(data[offset:offset + len(inner)], inner)
        self.assertEqual(stats.rows, VALID_BASE + 1000)

    def test_oversize_archive_is_rejected(self) -> None:
        with mock.patch.object(IN, "MAX_GCS_BYTES", 100):
            with self.assertRaisesRegex(IN.ItemTableNotFound, "상한"):
                IN.extract_item_table_bytes(gcs_bytes([TABLE]))

    def test_extract_from_file_stamps_source(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            g = Path(d) / IN.GCS_NAME
            data = gcs_bytes([DECOY, TABLE])
            g.write_bytes(data)
            table = IN.extract_item_table(g)
            s = table.source
            self.assertEqual((s.gcs_path, s.gcs_size, s.rows, s.extractor_version),
                             (str(g), len(data), VALID_BASE + 1000, IN.EXTRACTOR_VERSION))
            self.assertEqual(s.archive_ts, "2026-09-21T02:04:11Z")
            self.assertEqual(len(s.gcs_sha256), 64)
            self.assertTrue(s.extracted_at.startswith("20"))
            self.assertEqual(table.lookup(853), "봉인의돌")


class NormalizeTest(unittest.TestCase):
    def test_display_strips_only_one_leading_m_prefix(self) -> None:
        for raw, want in (("[M]봉인의돌", "봉인의돌"), (" [M]봉인의돌 ", "봉인의돌"), ("[M][M]x", "[M]x"),
                          ("[천권] 주술 비법", "[천권] 주술 비법"), ("<삼족오>선사의원앙월", "<삼족오>선사의원앙월"),
                          ("", ""), ("  ", "")):
            with self.subTest(raw=raw):
                self.assertEqual(IN.display_name(raw), want)

    def test_norm_squash_and_casefold(self) -> None:
        for name, want in (("[천권] 주술 비법", "[천권]주술비법"), ("Lv.3 ABC", "lv.3abc"),
                           ("  a  b ", "ab"), ("", "")):
            with self.subTest(name=name):
                self.assertEqual(IN.norm_name(name), want)
        self.assertEqual(IN.norm_name(IN.display_name(" [M]봉인의돌 ")), "봉인의돌")


def _source(**kw) -> IN.TableSource:
    base = dict(gcs_path="x", gcs_size=1, gcs_mtime_ns=1, head_sha256="h", gcs_sha256="g", archive_ts=None,
                stream_offset=0, rows=0, skipped=0, duplicate_ids=0, decode_errors=0, extracted_at="t",
                extractor_version=IN.EXTRACTOR_VERSION)
    base.update(kw)
    return IN.TableSource(**base)


class SearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = IN.ItemTable({986: "떡국", 3085: "떡국", 853: "[M]봉인의돌", 3506: "봉인의서",
                                   1: "봉인 의서", 5: "청색 정기의 돌"}, _source())

    def test_lookup_display_and_raw(self) -> None:
        self.assertEqual(self.table.lookup(853), "봉인의돌")
        self.assertEqual(self.table.lookup_raw(853), "[M]봉인의돌")
        self.assertIsNone(self.table.lookup(999))
        self.assertIn(853, self.table)
        self.assertNotIn("x", self.table)
        self.assertEqual(len(self.table), 6)

    def test_union_for_duplicate_names_and_squash_collision(self) -> None:
        self.assertEqual(self.table.search("떡국"), [986, 3085])
        self.assertEqual(self.table.search("봉인 의서"), [1, 3506], "공백 제거 정규화로 두 id 가 한 키")

    def test_exact_before_substring_and_limit(self) -> None:
        self.assertEqual(self.table.search("봉인의서"), [1, 3506])
        self.assertEqual(self.table.search("봉인"), [1, 853, 3506])
        self.assertEqual(self.table.search("봉인", limit=2), [1, 853])
        self.assertEqual(self.table.search("   "), [])
        self.assertEqual(self.table.search("없는이름"), [])

    def test_json_roundtrip(self) -> None:
        d = self.table.to_json_dict()
        self.assertEqual(d["version"], IN.CACHE_VERSION)
        self.assertEqual(list(d["items"])[:3], ["1", "5", "853"], "숫자 순 정렬")
        back = IN.ItemTable.from_json_dict(json.loads(json.dumps(d, ensure_ascii=False)))
        self.assertEqual(dict(back.raw), dict(self.table.raw))
        self.assertEqual(back.source, self.table.source)
        with self.assertRaises(ValueError):
            IN.ItemTable.from_json_dict({"version": 99, "source": {}, "items": {}})


class CacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.gcs = self.dir / IN.GCS_NAME
        self.gcs.write_bytes(gcs_bytes([DECOY, TABLE]))
        self.cache = self.dir / "cache" / "item_names.json"

    def _load(self, **kw) -> IN.ItemTable | None:
        kw.setdefault("gcs_path", self.gcs)
        kw.setdefault("cache_path", self.cache)
        return IN.load_item_table(**kw)

    def test_first_load_extracts_and_writes_cache(self) -> None:
        t = self._load()
        self.assertIsNotNone(t)
        self.assertEqual(len(t), VALID_BASE + 1000)
        self.assertTrue(self.cache.is_file())
        self.assertFalse(self.cache.with_name(self.cache.name + ".tmp").exists())

    def test_second_load_uses_cache(self) -> None:
        self._load()
        boom = mock.Mock(side_effect=AssertionError("must not rescan"))
        t = self._load(extractor=boom)
        self.assertEqual(t.lookup(853), "봉인의돌")
        boom.assert_not_called()

    def test_size_change_rescans(self) -> None:
        self._load()
        self.gcs.write_bytes(gcs_bytes([TABLE, UNRELATED]))
        spy = mock.Mock(wraps=IN.extract_item_table)
        t = self._load(extractor=spy)
        spy.assert_called_once()
        self.assertEqual(t.source.gcs_size, self.gcs.stat().st_size)

    def test_same_content_other_mtime_or_copy_uses_head_sha(self) -> None:
        self._load()
        copy = self.dir / "copy" / IN.GCS_NAME
        copy.parent.mkdir()
        copy.write_bytes(self.gcs.read_bytes())
        os.utime(copy, (1_000_000_000, 1_000_000_000))
        boom = mock.Mock(side_effect=AssertionError("must not rescan"))
        t = self._load(gcs_path=copy, extractor=boom)
        self.assertEqual(len(t), VALID_BASE + 1000)
        boom.assert_not_called()

    def test_corrupt_or_old_version_cache_rescans(self) -> None:
        self._load()
        self.cache.write_text("{not json", encoding="utf-8")
        spy = mock.Mock(wraps=IN.extract_item_table)
        self.assertIsNotNone(self._load(extractor=spy))
        spy.assert_called_once()
        d = json.loads(self.cache.read_text(encoding="utf-8"))
        d["source"]["extractor_version"] = IN.EXTRACTOR_VERSION - 1
        self.cache.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        spy2 = mock.Mock(wraps=IN.extract_item_table)
        self.assertIsNotNone(self._load(extractor=spy2))
        spy2.assert_called_once()

    def test_refresh_forces_rescan(self) -> None:
        self._load()
        spy = mock.Mock(wraps=IN.extract_item_table)
        self._load(refresh=True, extractor=spy)
        spy.assert_called_once()

    def test_cache_write_failure_still_returns_table(self) -> None:
        blocked = self.dir / "blocked"
        blocked.mkdir()     # 디렉터리를 캐시 파일 경로로 — os.replace 가 실패한다
        t = self._load(cache_path=blocked)
        self.assertIsNotNone(t)
        self.assertEqual(t.lookup(3506), "봉인의서")

    def test_missing_gcs_with_and_without_cache(self) -> None:
        self.assertIsNone(self._load(gcs_path=self.dir / "nope.gcs"))
        self._load()
        t = self._load(gcs_path=self.dir / "nope.gcs")
        self.assertIsNotNone(t)
        self.assertEqual(t.lookup(853), "봉인의돌")

    def test_extract_failure_returns_cache_or_none_never_raises(self) -> None:
        self.assertIsNone(self._load(extractor=mock.Mock(side_effect=RuntimeError("boom"))))
        self._load()
        self.gcs.write_bytes(gcs_bytes([UNRELATED]))    # 표 없음 → 추출 실패
        t = self._load()
        self.assertIsNotNone(t, "추출 실패면 캐시라도")
        self.assertEqual(t.lookup(853), "봉인의돌")

    def test_use_cache_false_neither_reads_nor_writes(self) -> None:
        t = self._load(use_cache=False)
        self.assertIsNotNone(t)
        self.assertFalse(self.cache.exists())


class DiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("A", "B", "Client1"):
            (self.root / name).mkdir()
            (self.root / name / IN.GCS_NAME).write_bytes(b"x")
        (self.root / "C").mkdir()
        (self.root / "ClientX").mkdir()

    def test_order_explicit_env_process_glob_with_dedupe(self) -> None:
        image = {1: str(self.root / "B" / "Gersang.exe"), 2: None}
        got = IN.discover_client_dirs(
            self.root / "A", env={IN.ENV_CLIENT_DIR: str(self.root / "B")}, pids=lambda: [1, 2],
            image_path=image.get, glob_roots=(str(self.root / "Client*"),))
        self.assertEqual(got, [self.root / "A", self.root / "B", self.root / "Client1"])

    def test_missing_gcs_is_filtered_and_failures_ignored(self) -> None:
        def boom(_pid):
            raise OSError("access denied")
        got = IN.discover_client_dirs(self.root / "C", env={}, pids=lambda: [7],
                                      image_path=boom, glob_roots=(str(self.root / "Client*"),))
        self.assertEqual(got, [self.root / "Client1"])
        got = IN.discover_client_dirs(None, env={}, pids=mock.Mock(side_effect=RuntimeError),
                                      image_path=lambda p: None, glob_roots=(str(self.root / "Nope*"),))
        self.assertEqual(got, [])

    def test_find_gcs(self) -> None:
        self.assertEqual(IN.find_gcs(self.root / "A", env={}, pids=lambda: [], glob_roots=()),
                         self.root / "A" / IN.GCS_NAME)
        self.assertIsNone(IN.find_gcs(self.root / "C", env={}, pids=lambda: [], glob_roots=()))


class DumpToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.gcs = self.dir / IN.GCS_NAME
        self.gcs.write_bytes(gcs_bytes([DECOY, TABLE]))
        self.cache = self.dir / "cache.json"

    def _run(self, *args: str, gcs: Path | None = None) -> tuple[int, str, str]:
        cmd = [sys.executable, "-X", "utf8", str(TOOL), "--gcs", str(gcs or self.gcs), "--cache", str(self.cache), *args]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=120, cwd=str(ROOT))
        return p.returncode, p.stdout, p.stderr

    def test_check_ok_then_cache_hit(self) -> None:
        rc, out, err = self._run("--check")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("앵커 8/8 일치", out)
        self.assertIn(f"표: {VALID_BASE + 1000:,}행", out)
        self.assertIn("캐시 miss", out)
        rc, out, _ = self._run("--check")
        self.assertEqual(rc, 0)
        self.assertIn("캐시 hit", out)

    def test_anchor_mismatch_is_rc1(self) -> None:
        bad = table_text([r.replace("[M]봉인의돌", "다른이름") for r in BASE_ROWS]).encode("cp949")
        g = self.dir / "bad.gcs"
        g.write_bytes(gcs_bytes([bad]))
        rc, out, _ = self._run("--check", "--no-cache", gcs=g)
        self.assertEqual(rc, 1)
        self.assertIn("앵커 7/8 일치", out)
        self.assertIn("X    853", out)

    def test_out_ids_from_and_search(self) -> None:
        out_path = self.dir / "dump" / "item_names.json"
        rc, out, _ = self._run("--out", str(out_path), "--no-cache")
        self.assertEqual(rc, 0, out)
        d = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(len(d["items"]), VALID_BASE + 1000)
        probe = self.dir / "probe.txt"
        probe.write_text("row0  등록=3769258 아이템=853    수량=50 가격=150,000,000 판매자='테○A'\n"
                         "row1  등록=3769257 아이템=424242 수량=1  가격=5 판매자='x'\n", encoding="utf-8")
        rc, out, _ = self._run("--ids-from", str(probe), "--no-cache")
        self.assertEqual(rc, 1)
        self.assertIn("853\t봉인의돌", out)
        self.assertIn("424242\t?", out)
        self.assertIn("해석 1/2 · 미해석 [424242]", out)
        rc, out, _ = self._run("--search", "봉인", "--no-cache")
        self.assertEqual(rc, 0)
        self.assertIn("853\t봉인의돌", out)
        self.assertIn("3506\t봉인의서", out)
        self.assertIn("검색 '봉인': 2건", out)

    def test_no_client_is_rc2(self) -> None:
        rc, out, err = self._run("--check", "--no-cache", gcs=self.dir / "absent.gcs")
        self.assertEqual(rc, 2)
        self.assertIn(IN.GCS_NAME, err)

    def test_ids_from_text_prefers_item_tokens(self) -> None:
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import dump_item_names as tool
        finally:
            sys.path.remove(str(ROOT / "tools"))
        self.assertEqual(tool.ids_from_text("아이템=853 아이템=3506 아이템=853"), [853, 3506])
        self.assertEqual(tool.ids_from_text("853, 3506 and 12.5"), [853, 3506])
        self.assertEqual(tool.ids_from_text(""), [])


if __name__ == "__main__":
    unittest.main()
