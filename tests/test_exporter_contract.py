"""Contract checks for the browser userscript's timestamp/privacy behavior."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src/handouter_zhiyun_collector/zhiyun_exporter.user.js"


@unittest.skipUnless(shutil.which("node"), "node required")
class ExporterContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def _function(self, name: str, next_name: str) -> str:
        start = self.source.index(f"    function {name}")
        end = self.source.index(f"    function {next_name}", start)
        return self.source[start:end]

    def _lecture_date(self, expression: str) -> str | None:
        js = "\n".join([
            self._function("parseLectureDate", "extractLectureDate"),
            self._function("extractLectureDate", "extractPptUrl"),
            f"console.log(JSON.stringify(extractLectureDate({expression})));",
        ])
        out = subprocess.check_output([shutil.which("node"), "-e", js], text=True)
        return json.loads(out)

    def _timing(self, expression: str) -> dict:
        js = "\n".join([
            self._function("timeStrToSeconds", "secToTime"),
            self._function("secToTime", "getFormattedDate"),
            self._function("extractPptTiming", "extractPptTime"),
            f"console.log(JSON.stringify(extractPptTiming({expression})));",
        ])
        out = subprocess.check_output([shutil.which("node"), "-e", js], text=True)
        return json.loads(out)

    def test_long_generic_numeric_time_is_not_guessed_as_milliseconds(self):
        value = self._timing("({time: 11400})")
        self.assertIsNone(value["seconds"])
        self.assertEqual(value["timeStatus"], "unknown_numeric_unit")
        self.assertEqual(value["rawValue"], 11400)

    def test_created_is_explicitly_seconds(self):
        value = self._timing("({created: 11400})")
        self.assertEqual(value["seconds"], 11400)
        self.assertEqual(value["timestamp"], "03:10:00")
        self.assertEqual(value["timeStatus"], "known_seconds")

    def test_missing_time_is_null_not_fake_zero(self):
        value = self._timing("({})")
        self.assertIsNone(value["seconds"])
        self.assertIsNone(value["timestamp"])

    def test_clock_string_preserves_milliseconds(self):
        value = self._timing("({switchTime: '01:02:03.125'})")
        self.assertEqual(value["seconds"], 3723.125)
        self.assertEqual(value["timestamp"], "01:02:03.125")

    def test_lecture_date_comes_from_real_lecture_metadata_not_export_time(self):
        self.assertEqual(self._lecture_date("({sub_title: '2026-06-16第1-2节'})"), '2026-06-16')
        self.assertEqual(self._lecture_date("({nested: {lectureDate: '2026年9月3日'}})"), '2026-09-03')
        self.assertIsNone(self._lecture_date("({sub_title: '2026-02-30第1-2节', exportDate: '2026-09-21'})"))
        self.assertIn("lectureDate: data.lectureDate || null", self.source)

    def test_exporter_has_browser_memory_safety_limits(self):
        self.assertNotIn("document.querySelectorAll('*')", self.source)
        self.assertIn('MAX_VUE_SCAN_ELEMENTS = 3000', self.source)
        self.assertIn('PPT_DOWNLOAD_CONCURRENCY = 3', self.source)
        self.assertIn('MAX_ARCHIVE_SOURCE_BYTES = 256 * 1024 * 1024', self.source)
        self.assertIn("compression: 'STORE'", self.source)
        self.assertIn('MAX_CANVAS_PIXELS', self.source)

    def test_public_slide_metadata_no_longer_copies_source_url(self):
        self.assertNotIn("url: task.url", self.source)
        self.assertIn("status: downloaded ? 'ok' : 'missing_image'", self.source)
        self.assertIn("zip.folder('private').file('source.json'", self.source)

    def test_userscript_syntax(self):
        subprocess.run([shutil.which("node"), "--check", str(SCRIPT)], check=True)


if __name__ == "__main__":
    unittest.main()
