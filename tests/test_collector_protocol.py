"""Bounded protocol contract tests using synthetic archives and media."""

import io
import json
from pathlib import Path
import shutil
import socketserver
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer
import zipfile

from handouter_zhiyun_collector import collector, protocol
from package_assertions import inspect


class Response(io.BytesIO):
    headers = {"Content-Type": "video/mp4"}

    def geturl(self):
        return "https://media.example.test/clip.mp4?auth_key=PRIVATE_SYNTHETIC&cookie=SECRET"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class CollectorProtocolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "browser.zip"
        with zipfile.ZipFile(self.source, "w") as archive:
            archive.writestr("private/source.json", json.dumps({
                "videoUrl": "https://media.example.test/clip.mp4?auth_key=PRIVATE_SYNTHETIC&cookie=SECRET"}))
            archive.writestr("course_info.json", json.dumps({"courseName": "合成课程", "lectureDate": "2026-09-27"}))
            archive.writestr("slides_meta.json", "[]")

    def invoke(self, request):
        stdin = io.BytesIO(json.dumps(request, ensure_ascii=False).encode() + b"\n")
        stdout = io.BytesIO()
        output = type("Output", (), {"buffer": stdout})()
        with patch.object(protocol.sys, "stdout", output), patch.object(protocol.sys, "stdin", type("Input", (), {"buffer": stdin})()):
            code = protocol.run()
        wire = stdout.getvalue().decode("utf-8")
        frames = [json.loads(line) for line in wire.splitlines()]
        return code, frames, wire

    def replace_export_members(self, replacements):
        with zipfile.ZipFile(self.source) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members.update(replacements)
        with zipfile.ZipFile(self.source, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)

    def test_identity_has_stable_tool_identity(self):
        code, frames, _ = self.invoke({"protocol_version": 1, "method": "identity", "params": {}})
        self.assertEqual(code, 0)
        self.assertEqual(frames, [{"type": "identity", "tool_id": "handouter-zhiyun-collector",
                                   "protocol_version": 1, "tool_version": "0.3.0.dev2",
                                   "capabilities": ["inspect", "convert"]}])

    def test_inspection_returns_host_and_hash_but_never_signed_url(self):
        code, frames, wire = self.invoke({"protocol_version": 1, "method": "inspect",
                                          "params": {"source_path": str(self.source)}})
        self.assertEqual(code, 0)
        frame = frames[0]
        self.assertEqual(frame["media_hosts"], ["media.example.test"])
        self.assertEqual(frame["course_title"], "合成课程")
        self.assertRegex(frame["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("PRIVATE_SYNTHETIC", wire)
        self.assertNotIn("auth_key", wire)
        self.assertNotIn("cookie", wire)

    def test_inspection_and_conversion_suppress_credentials_in_course_title(self):
        self.replace_export_members({"course_info.json": json.dumps({
            "courseName": "https://media.example.test/clip.mp4?auth_key=LEAK",
            "lectureDate": "2026-09-27"}).encode()})
        code, frames, wire = self.invoke({"protocol_version": 1, "method": "inspect",
                                          "params": {"source_path": str(self.source)}})
        self.assertEqual(code, 0)
        self.assertEqual(frames[0]["course_title"], "智云课程")
        self.assertNotIn("LEAK", wire)
        details = collector.inspect_export(self.source, require_https=True)
        target = self.root / "clean-title.zip"
        request = {"protocol_version": 1, "method": "convert", "params": {
            "source_path": str(self.source), "source_sha256": details["source_sha256"],
            "allowed_media_hosts": ["media.example.test"], "output_path": str(target)}}
        with patch.object(collector, "_resolved_host", return_value="127.0.0.1"), \
             patch.object(collector, "_media_request", return_value=Response(
                b"\x00\x00\x00\x10ftypisom" + b"0" * 40)):
            code, frames, wire = self.invoke(request)
        self.assertEqual(code, 0)
        with zipfile.ZipFile(target) as archive:
            manifest = json.loads(archive.read("handouter-input.json"))
        self.assertEqual(manifest["lecture"]["course_title"], "智云课程")
        self.assertNotIn("LEAK", target.read_bytes().decode("utf-8", errors="ignore"))

    def test_real_loopback_https_convert_and_package_structure(self):
        openssl = shutil.which("openssl")
        if not openssl:
            self.skipTest("openssl unavailable for local synthetic TLS certificate")
        cert = self.root / "test-cert.pem"
        key = self.root / "test-key.pem"
        subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj",
                        "/CN=media.example.test", "-addext",
                        "subjectAltName=DNS:media.example.test"],
                       check=True, capture_output=True, timeout=30)
        media_bytes = b"\x00\x00\x00\x10ftypisom" + b"S" * 64

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(media_bytes)))
                self.end_headers()
                self.wfile.write(media_bytes)

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(cert), str(key))
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        url = f"https://media.example.test:{server.server_port}/clip.mp4?auth_key=LOCAL_ONLY"
        self.replace_export_members({"private/source.json": json.dumps({"videoUrl": url}).encode()})
        details = collector.inspect_export(self.source, require_https=True)
        target = self.root / "tls-standard.zip"
        request = {"protocol_version": 1, "method": "convert", "params": {
            "source_path": str(self.source), "source_sha256": details["source_sha256"],
            "allowed_media_hosts": ["media.example.test"], "output_path": str(target)}}
        with patch.object(collector, "_resolved_host", return_value="127.0.0.1"), \
             patch.object(collector.certifi, "where", return_value=str(cert)):
            code, frames, wire = self.invoke(request)
        self.assertEqual(code, 0, frames)
        self.assertEqual(inspect(target).source_kind, "asset_zip")
        self.assertNotIn("LOCAL_ONLY", wire)

        # The embedded CA choice must still reject an untrusted certificate.
        untrusted_target = self.root / "untrusted-standard.zip"
        untrusted = {**request, "params": {**request["params"], "output_path": str(untrusted_target)}}
        with patch.object(collector, "_resolved_host", return_value="127.0.0.1"):
            code, frames, wire = self.invoke(untrusted)
        self.assertNotEqual(code, 0)
        self.assertFalse(untrusted_target.exists())
        self.assertNotIn("LOCAL_ONLY", wire)

        # Trusting the issuer must not disable HTTPS hostname verification.
        wrong_url = f"https://other.example.test:{server.server_port}/clip.mp4?auth_key=LOCAL_ONLY"
        self.replace_export_members({"private/source.json": json.dumps({"videoUrl": wrong_url}).encode()})
        wrong_details = collector.inspect_export(self.source, require_https=True)
        wrong_target = self.root / "wrong-host-standard.zip"
        wrong = {**request, "params": {**request["params"],
            "source_sha256": wrong_details["source_sha256"],
            "allowed_media_hosts": ["other.example.test"], "output_path": str(wrong_target)}}
        with patch.object(collector, "_resolved_host", return_value="127.0.0.1"), \
             patch.object(collector.certifi, "where", return_value=str(cert)):
            code, frames, wire = self.invoke(wrong)
        self.assertNotEqual(code, 0)
        self.assertFalse(wrong_target.exists())
        self.assertNotIn("LOCAL_ONLY", wire)

    def test_confirmed_convert_publishes_standard_zip_structure(self):
        details = collector.inspect_export(self.source)
        target = self.root / "standard.zip"
        request = {"protocol_version": 1, "method": "convert", "params": {
            "source_path": str(self.source), "source_sha256": details["source_sha256"],
            "allowed_media_hosts": ["media.example.test"], "output_path": str(target)}}
        with patch.object(collector, "_resolved_host", return_value="127.0.0.1"), \
             patch.object(collector, "_media_request", return_value=Response(
                b"\x00\x00\x00\x10ftypisom" + b"0" * 40)):
            code, frames, wire = self.invoke(request)
        self.assertEqual(code, 0)
        self.assertEqual(frames[-1], {"type": "result", "output_path": str(target)})
        self.assertEqual(inspect(target).source_kind, "asset_zip")
        self.assertNotIn("PRIVATE_SYNTHETIC", wire)
        # Deterministic packaging: no member carries the collection time.
        with zipfile.ZipFile(target) as archive:
            self.assertEqual({info.date_time for info in archive.infolist()}, {collector.PACKAGE_DATE_TIME})

    def test_domain_or_source_change_rejected_before_download(self):
        details = collector.inspect_export(self.source)
        target = self.root / "wrong-host.zip"
        request = {"protocol_version": 1, "method": "convert", "params": {
            "source_path": str(self.source), "source_sha256": details["source_sha256"],
            "allowed_media_hosts": ["other.example.test"], "output_path": str(target)}}
        with patch.object(collector, "_media_request") as download:
            code, frames, wire = self.invoke(request)
        self.assertNotEqual(code, 0)
        download.assert_not_called()
        self.assertFalse(target.exists())
        self.assertEqual(frames[-1]["type"], "error")
        self.assertNotIn("PRIVATE_SYNTHETIC", wire)

    def test_unknown_fields_fail_closed(self):
        code, frames, wire = self.invoke({"protocol_version": 1, "method": "identity",
                                          "params": {}, "extra": "ignored?"})
        self.assertNotEqual(code, 0)
        self.assertEqual(frames, [{"type": "error", "code": "COLLECTOR_FAILED",
                                   "message": protocol.ERROR_MESSAGE}])
        self.assertEqual(len(wire.splitlines()), 1)

    def test_download_failure_does_not_publish_partial_zip(self):
        details = collector.inspect_export(self.source)
        target = self.root / "failed.zip"
        request = {"protocol_version": 1, "method": "convert", "params": {
            "source_path": str(self.source), "source_sha256": details["source_sha256"],
            "allowed_media_hosts": ["media.example.test"], "output_path": str(target)}}
        with patch.object(collector, "_media_request", side_effect=OSError("SECRET URL must not escape")):
            code, frames, wire = self.invoke(request)
        self.assertNotEqual(code, 0)
        self.assertFalse(target.exists())
        self.assertNotIn("SECRET", wire)
        self.assertEqual(frames[-1]["type"], "error")

    def test_source_slide_names_are_replaced_with_safe_package_names(self):
        self.replace_export_members({
            "slides_meta.json": json.dumps([{"filename": "cookie=PRIVATE_NAME.jpg", "index": 1, "seconds": 1}]).encode(),
            "slides/cookie=PRIVATE_NAME.jpg": b"\xff\xd8\xff" + b"J" * 20,
            "slides/slide_001.jpg": b"",
        })
        details = collector.inspect_export(self.source, require_https=True)
        target = self.root / "renamed-slides.zip"
        request = {"protocol_version": 1, "method": "convert", "params": {
            "source_path": str(self.source), "source_sha256": details["source_sha256"],
            "allowed_media_hosts": ["media.example.test"], "output_path": str(target)}}
        with patch.object(collector, "_resolved_host", return_value="127.0.0.1"), \
             patch.object(collector, "_media_request", return_value=Response(
                b"\x00\x00\x00\x10ftypisom" + b"0" * 40)):
            code, frames, _wire = self.invoke(request)
        self.assertEqual(code, 0, frames)
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            events = json.loads(archive.read("slides/slides_meta.json"))
        self.assertIn("slides/images/slide_0001.jpg", names)
        self.assertEqual(events[0]["filename"], "slide_0001.jpg")
        self.assertNotIn("PRIVATE_NAME", target.read_bytes().decode("latin1"))


if __name__ == "__main__":
    unittest.main()
