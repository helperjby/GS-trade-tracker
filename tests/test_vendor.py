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
    def test_manifest_matches_files(self) -> None:
        vendor = json.loads((VENDOR_DIR / "VENDOR.json").read_text(encoding="utf-8"))
        self.assertEqual(set(vendor["files"]), set(sync.MODULES))
        for name, meta in vendor["files"].items():
            # 줄바꿈 정규화(LF) 뒤 해시 — 체크아웃의 CRLF/LF 차이는 드리프트가 아니다.
            data = sync.normalize((VENDOR_DIR / name).read_bytes())
            self.assertEqual(hashlib.sha256(data).hexdigest(), meta["sha256"],
                             f"{name} 이 손으로 바뀌었다 — tools/sync_seassist_core.py 로만 갱신")
            self.assertEqual(len(data), meta["bytes"], name)
        self.assertTrue(vendor.get("commit"), "출처 커밋이 비어 있다")

    def test_owned_files_are_not_synced(self) -> None:
        self.assertNotIn("config.py", sync.MODULES)
        self.assertIn("config.py", sync.OWNED)


class ImportBoundaryTest(unittest.TestCase):
    """관측기는 stdlib + ctypes 로 돈다 — cv2·numpy·PIL·pywin32 를 끌고 오면 exe 가 무거워지고
    SEAssist 없는 PC 에서 못 뜬다."""

    ALLOWED_STDLIB = {
        "__future__", "argparse", "base64", "ctypes", "collections", "dataclasses", "datetime",
        "hashlib", "ipaddress", "json", "logging", "os", "pathlib", "platform", "queue", "re",
        "socket", "subprocess", "sys", "threading", "time", "traceback", "types", "typing",
    }

    def test_static_imports(self) -> None:
        pkg = SRC / "yuktracker"
        for path in sorted(pkg.rglob("*.py")):
            mods = _imports(path)
            external = {m for m in mods if not m.startswith(".")} - self.ALLOWED_STDLIB
            self.assertEqual(external, set(), f"{path.relative_to(SRC)}: 허용 밖 import {external}")

    def test_runtime_import_is_light(self) -> None:
        code = ("import sys; import yuktracker.agent, yuktracker.cli; "
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
