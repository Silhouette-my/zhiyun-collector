"""Synthetic acquisition-side conversion; no website or paid service calls."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from handouter_zhiyun_collector.collector import CollectorError, convert
from handouter_zhiyun_collector import collector as collector_module
from package_assertions import inspect


class _Response(io.BytesIO):
    headers = {"Content-Type": "video/mp4"}

    def geturl(self):
        return "http://127.0.0.1/media.mp4?auth_key=SYNTHETIC_SECRET"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class CollectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def export(self, *, missing=False, media_url="http://127.0.0.1/media.mp4?auth_key=SYNTHETIC_SECRET"):
        archive = self.root / "browser.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("private/source.json", json.dumps({"videoUrl": media_url}))
            output.writestr("course_info.json", json.dumps({"courseName": "合成课", "lectureDate": "2026-09-26"}))
            output.writestr("slides_meta.json", json.dumps([{"index": 1, "filename": "slide_001.jpg", "seconds": 1}]))
            if not missing:
                output.writestr("slides/slide_001.jpg", b"\xff\xd8\xff" + b"0" * 20)
        return archive

    def test_convert_lands_media_and_strips_private_source(self):
        source = self.export()
        target = self.root / "standard.zip"
        with patch.object(collector_module, "_media_request", return_value=_Response(b"\x00\x00\x00\x10ftypisom" + b"0" * 40)), \
             patch.object(zipfile.ZipFile, "namelist", side_effect=AssertionError("Do not rebuild ZIP member names for each slide")):
            result = convert(source, target, allowed_media_host="127.0.0.1", development_loopback=True)
        self.assertEqual(result["slide_images"], 1)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(set(archive.namelist()), {"handouter-input.json", "media/lecture.mp4",
                "slides/slides_meta.json", "slides/images/slide_0001.jpg"})
            self.assertNotIn(b"SYNTHETIC_SECRET", target.read_bytes())
        asset = inspect(target)
        self.assertEqual(asset.source_kind, "asset_zip")
        self.assertEqual(asset.slide_images, 1)

    def test_missing_slide_fails_without_output(self):
        source = self.export(missing=True)
        target = self.root / "standard.zip"
        with self.assertRaises(CollectorError):
            convert(source, target, allowed_media_host="127.0.0.1", development_loopback=True)
        self.assertFalse(target.exists())

    def test_media_without_slides_is_a_valid_standard_package(self):
        source = self.export()
        with zipfile.ZipFile(source, "w") as output:
            output.writestr("private/source.json", json.dumps({"videoUrl": "http://127.0.0.1/media.mp4?auth_key=SYNTHETIC_SECRET"}))
            output.writestr("course_info.json", json.dumps({"courseName": "合成无课件课程"}))
            output.writestr("slides_meta.json", "[]")
        target = self.root / "standard-no-slides.zip"
        with patch.object(collector_module, "_media_request", return_value=_Response(b"\x00\x00\x00\x10ftypisom" + b"0" * 40)):
            result = convert(source, target, allowed_media_host="127.0.0.1", development_loopback=True)
        self.assertEqual(result["slide_images"], 0)
        self.assertEqual(inspect(target).slide_images, 0)
        with zipfile.ZipFile(target) as output:
            self.assertNotIn("slides/slides_meta.json", output.namelist())
            manifest = json.loads(output.read("handouter-input.json"))
        self.assertIsNone(manifest["lecture"]["lecture_date"])
        schema = json.loads((Path(__file__).resolve().parents[1] / "schemas/handouter-input-v1.schema.json").read_text())
        self.assertEqual(schema["properties"]["lecture"]["properties"]["lecture_date"]["type"], ["string", "null"])

    def test_finished_unencrypted_hls_lands_transport_stream(self):
        source = self.export(media_url="http://127.0.0.1/media.m3u8")
        target = self.root / "standard-hls.zip"
        sources = {
            "http://127.0.0.1/media.m3u8": (b"#EXTM3U\n#EXTINF:2,\nseg1.ts\n#EXTINF:2,\nseg2.ts\n#EXT-X-ENDLIST\n", "application/vnd.apple.mpegurl"),
            "http://127.0.0.1/seg1.ts": (b"\x47" + b"0" * 187, "video/mp2t"),
            "http://127.0.0.1/seg2.ts": (b"\x47" + b"1" * 187, "video/mp2t"),
        }
        class Response(io.BytesIO):
            def __init__(self, url, data, content_type):
                super().__init__(data)
                self.url = url
                self.headers = {"Content-Type": content_type}
            def geturl(self):
                return self.url
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                self.close()
        def fake_request(url, *_args):
            data, content_type = sources[url]
            return Response(url, data, content_type)
        with patch.object(collector_module, "_media_request", side_effect=fake_request):
            convert(source, target, allowed_media_host="127.0.0.1", development_loopback=True)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.read("media/lecture.ts"), sources["http://127.0.0.1/seg1.ts"][0] + sources["http://127.0.0.1/seg2.ts"][0])
        self.assertEqual(inspect(target).source_kind, "asset_zip")

    def test_validated_ip_is_pinned_into_connection_with_normal_tls_context(self):
        seen = {}
        class FakeResponse:
            status = 200
            headers = {"Content-Type": "video/mp4"}
            def close(self): pass
            def read(self, amount): return b""
        class FakeConnection:
            def __init__(self, host, port, *, timeout, context):
                seen.update(host=host, port=port, timeout=timeout, context=context)
            def request(self, method, path, *, headers):
                seen.update(method=method, path=path)
                self._create_connection((seen["host"], seen["port"]), 60)
            def getresponse(self): return FakeResponse()
            def close(self): pass
        for address in ("93.184.216.34", "10.0.0.5", "127.0.0.1"):
            with self.subTest(address=address):
                record = (2, 1, 6, "", (address, 0))
                with patch("socket.getaddrinfo", return_value=[record]), \
                     patch("socket.create_connection", return_value=object()) as connect, \
                     patch("http.client.HTTPSConnection", FakeConnection):
                    with collector_module._media_request("https://media.example.test/lecture.mp4", "media.example.test", False):
                        pass
                self.assertEqual(connect.call_args.args[0], (address, 443))
                self.assertEqual(seen["host"], "media.example.test")
                self.assertEqual(seen["context"].verify_mode, __import__("ssl").CERT_REQUIRED)


if __name__ == "__main__":
    unittest.main()
