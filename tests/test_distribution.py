"""Whitelist wheel checks; no private repository or native build dependency."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def test_wheel_contains_only_collector_and_distribution_metadata(self):
        spec = importlib.util.spec_from_file_location("build_collector", ROOT / "scripts/build_collector.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as temporary:
            wheel = builder.build(Path(temporary))
            with zipfile.ZipFile(wheel) as archive:
                names = archive.namelist()
                allowed = {"handouter_zhiyun_collector/" + name for name in builder.ALLOW}
                allowed |= {"handouter_zhiyun_collector/zhiyun_exporter.user.js", "handouter_zhiyun_collector/LICENSE"}
                info = "handouter_zhiyun_collector-0.3.0.dev2.dist-info/"
                self.assertEqual(set(names), allowed | {info + name for name in ["METADATA", "WHEEL", "entry_points.txt", "collector-manifest.json", "RECORD"]})
                metadata = archive.read(info + "METADATA").decode()
                self.assertIn("License: LicenseRef-Proprietary", metadata)
                self.assertNotIn("License: MIT", metadata)
                self.assertIn(b"All rights reserved", archive.read("handouter_zhiyun_collector/LICENSE"))
                self.assertIn(b"Copyright (c) 2026 8eoyw", archive.read("handouter_zhiyun_collector/THIRD_PARTY_NOTICES.txt"))
                manifest = json.loads(archive.read(info + "collector-manifest.json"))
                self.assertEqual(set(manifest["files"]), allowed | {info + name for name in ["METADATA", "WHEEL", "entry_points.txt"]})
