"""CLI/TUI helpers operate only on explicitly selected local files."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from handouter_zhiyun_collector.__main__ import main
from handouter_zhiyun_collector.collector import inspect_export


class CliHelpersTests(unittest.TestCase):
    def test_stable_legacy_identity_and_fingerprint_ignore_signature_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            def create(name, media, slides="[]"):
                target = Path(directory) / name
                with zipfile.ZipFile(target, "w") as archive:
                    archive.writestr("private/source.json", json.dumps({"videoUrl": media,
                        "pageUrl": "https://example.test/#/replay?tenant_code=112&course_id=85&sub_id=196"}))
                    archive.writestr("course_info.json", json.dumps({"courseName": "Synthetic"}))
                    archive.writestr("slides_meta.json", slides)
                return inspect_export(target)
            first = create("first.zip", "https://media.example.test/a?auth_key=SYNTHETIC_1")
            rotated = create("rotated.zip", "https://media.example.test/a?auth_key=SYNTHETIC_2")
            changed = create("changed.zip", "https://media.example.test/b?auth_key=SYNTHETIC_2")
            self.assertEqual(first["source_id"], "zhiyun-112-85-196")
            self.assertEqual(first["source_id"], rotated["source_id"])
            self.assertEqual(first["source_fingerprint"], rotated["source_fingerprint"])
            self.assertNotEqual(first["source_sha256"], rotated["source_sha256"])
            self.assertNotEqual(first["source_fingerprint"], changed["source_fingerprint"])
            self.assertNotEqual(first["source_fingerprint"], create("slides.zip", "https://media.example.test/a?auth_key=SYNTHETIC_1", "[1]")["source_fingerprint"])

    def test_inspect_has_no_download_or_signed_url(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "browser.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("private/source.json", json.dumps({"videoUrl": "https://media.example.test/video.mp4?auth_key=SYNTHETIC_SECRET"}))
                archive.writestr("course_info.json", json.dumps({"courseName": "Synthetic course"}))
                archive.writestr("slides_meta.json", "[]")
            output = io.StringIO()
            with patch("handouter_zhiyun_collector.collector._media_request", side_effect=AssertionError("unexpected download")), contextlib.redirect_stdout(output):
                self.assertEqual(main(["inspect", "--browser-export", str(source)]), 0)
            value = json.loads(output.getvalue())
            self.assertEqual(value["media_hosts"], ["media.example.test"])
            self.assertNotIn("SYNTHETIC_SECRET", output.getvalue())
            self.assertNotIn("auth_key", output.getvalue())

    def test_export_script_is_self_contained_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "collector.user.js"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["export-script", "--output", str(target)]), 0)
            original = target.read_bytes()
            self.assertIn(b"==UserScript==", original)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(["export-script", "--output", str(target)])
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(target.read_bytes(), original)
