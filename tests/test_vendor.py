"""벤더 사본 계약 — VENDOR.json 해시 일치 · 경량 import 경계 · config 심의 키."""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
VENDOR_DIR = SRC / "yuktracker" / "seassist"
sys.path.insert(0, str(ROOT / "tools"))

import sync_seassist_core as sync  # noqa: E402


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add("." * node.level + (node.module or "").split(".")[0])
    return mods


class VendorManifestTest(unittest.TestCase):
    """`seassist/` 는 벤더 사본이 아니라 **이 레포 소유**다(2026-09-22 전량 소유 전환).
    복사 경로가 없으므로 해시 고정 대신 *출처 기록*을 검사한다 — 출처와 달라진 파일은
    반드시 `VENDOR.json` 의 `diverged` 가 사유와 함께 선언한다(조용한 변경 금지)."""

    def setUp(self) -> None:
        self.vendor = json.loads((VENDOR_DIR / "VENDOR.json").read_text(encoding="utf-8"))

    def test_manifest_covers_every_file(self) -> None:
        on_disk = {p.name for p in VENDOR_DIR.iterdir() if p.is_file()}
        declared = set(self.vendor["origin"]["files"]) | set(self.vendor["repo_own"])
        self.assertEqual(on_disk, declared,
                         "새 파일은 VENDOR.json 의 origin.files(출처 있음) 또는 repo_own(이 레포 것)에 적는다")
        self.assertEqual(set(sync.OWNED), declared)
        # 동기화(복사) 대상은 없다 — 스크립트가 파일을 덮어쓰는 경로가 사라졌다.
        self.assertEqual(sync.MODULES, ())
        self.assertTrue(self.vendor["origin"].get("commit"), "출처 커밋이 비어 있다")

    def test_origin_records_hash_and_size(self) -> None:
        for name, meta in self.vendor["origin"]["files"].items():
            self.assertRegex(meta["sha256"], r"^[0-9a-f]{64}$", name)
            self.assertGreater(meta["bytes"], 0, name)

    def test_diverged_declares_every_local_change(self) -> None:
        actual = set(sync.diverged_now(self.vendor))
        declared = {d["file"] for d in self.vendor["diverged"]}
        self.assertEqual(actual, declared,
                         "출처와 내용이 다른 파일은 diverged 에 사유와 함께 적는다 "
                         "(tools/sync_seassist_core.py --check 와 같은 검사)")
        for d in self.vendor["diverged"]:
            self.assertIn(d["file"], self.vendor["origin"]["files"], d["file"])
            self.assertTrue(str(d.get("why", "")).strip(), f"{d['file']}: 사유가 비어 있다")

    def test_config_shim_is_repo_own(self) -> None:
        self.assertIn("config.py", self.vendor["repo_own"])
        self.assertNotIn("config.py", self.vendor["origin"]["files"])


class ImportBoundaryTest(unittest.TestCase):
    """관측기는 stdlib + ctypes 로 돈다 — cv2·numpy·PIL·pywin32 를 끌고 오면 exe 가 무거워지고
    SEAssist 없는 PC 에서 못 뜬다."""

    ALLOWED_STDLIB = {
        "__future__", "argparse", "base64", "ctypes", "collections", "dataclasses", "datetime",
        "hashlib", "ipaddress", "json", "logging", "os", "pathlib", "platform", "queue", "re",
        "socket", "subprocess", "sys", "threading", "time", "traceback", "types", "typing",
        # zlib — item_names.py 가 클라 gersang.gcs 의 스트림을 inflate 한다(내장 확장, PyInstaller 기본 포함).
        "zlib",
        # tempfile — item_names.py 캐시 쓰기의 고유 이름 tmp(mkstemp) — 관측기 두 개가 겹쳐도 안전.
        "tempfile",
    }

    def test_static_imports(self) -> None:
        pkg = SRC / "yuktracker"
        for path in sorted(pkg.rglob("*.py")):
            mods = _imports(path)
            external = {m for m in mods if not m.startswith(".")} - self.ALLOWED_STDLIB
            self.assertEqual(external, set(), f"{path.relative_to(SRC)}: 허용 밖 import {external}")

    def test_runtime_import_is_light(self) -> None:
        code = ("import sys; import yuktracker.agent, yuktracker.cli, yuktracker.item_names; "
                "bad=[m for m in ('cv2','numpy','PIL','win32gui','win32api','google') "
                "if m in sys.modules]; print('HEAVY=' + ','.join(bad))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=str(SRC), timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("HEAVY=\n", out.stdout, out.stdout)


class ConfigShimTest(unittest.TestCase):
    def test_load_settings_reads_known_keys(self) -> None:
        import tempfile
        from unittest import mock
        from yuktracker.seassist import config
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict("os.environ", {"APPDATA": d}):
                s = config.load_settings()
                self.assertEqual(s.display_name, "")
                self.assertEqual(s.packet_data_dir, "")
                config.settings_path().write_text(
                    json.dumps({"display_name": "DEV-A", "packet_data_dir": "%OneDrive%/pkt",
                                "unrelated": 1, "dashboard_secret": None}),
                    encoding="utf-8")
                s = config.load_settings()
                self.assertEqual((s.display_name, s.packet_data_dir, s.dashboard_secret),
                                 ("DEV-A", "%OneDrive%/pkt", ""))
                self.assertEqual(config.app_dir(), Path(d) / "SEAssist")

    def test_ledger_paths_uses_shim(self) -> None:
        import tempfile
        from types import SimpleNamespace
        from unittest import mock
        from yuktracker.seassist import ledger_paths as P
        with tempfile.TemporaryDirectory() as d:
            s = SimpleNamespace(display_name="DEV-A", packet_data_dir=str(Path(d) / "pkt"),
                                wordinput_review_dir="")
            with mock.patch("yuktracker.seassist.config.load_settings", lambda: s):
                self.assertEqual(P.packet_dir(), Path(d) / "pkt" / "DEV-A")


if __name__ == "__main__":
    unittest.main()
