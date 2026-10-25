"""Independent v2 adapter tests: synthetic APIs, no real login or media."""

import copy
import io
from http.cookies import SimpleCookie
import json
from pathlib import Path
import tempfile
import sys
import time
import types
import unittest
from unittest.mock import patch
import zipfile

from handouter_zhiyun_collector import direct, protocol, collector
from package_assertions import inspect


SECRET = "PRIVATE_SIGNED_TOKEN"
VIDEO = "https://vod.example.test/record.mp4?auth_key=" + SECRET
IMAGE = "https://slides.example.test/one.png?auth_key=" + SECRET
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-image"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"synthetic-video"


class MemoryState:
    def __init__(self):
        self.current = {"token": SECRET, "id": "session-a"}
        self.values = {}

    def session(self):
        return self.current

    def set_session(self, token):
        self.current = {"token": token, "id": "session-b"}

    def logout(self):
        self.current = None

    def put(self, kind, value):
        identifier = f"{len(self.values) + 1:048x}"
        self.values[kind, identifier] = copy.deepcopy(value)
        return identifier

    def get(self, kind, identifier):
        return copy.deepcopy(self.values[kind, identifier])


class Response(io.BytesIO):
    def __init__(self, url, data, content_type):
        super().__init__(data)
        self.url = url
        self.headers = {"Content-Type": content_type}

    def geturl(self):
        return self.url

    def __exit__(self, *_args):
        self.close()


class DirectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = MemoryState()
        self.adapter = direct.Adapter(self.state)
        self.api_calls = []
        self.api_patch = patch.object(direct, "_api", side_effect=self.api)
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)

    def api(self, host, path, token, params=None):
        self.assertEqual(token, SECRET)
        self.api_calls.append(path)
        if path.endswith("infosimple"):
            return {"params": {"id": 1, "tenant_id": 112, "phone": "123", "account": "user"}}
        if path.endswith("searchlist"):
            return {"total": {"list": [{"course_id": "9", "title": "合成课程"}], "total": 1}}
        if path.endswith("get-course-detail"):
            return {"data": {"sub_list": {"2026": {"10": {"1": [
                {"id": i, "sub_title": "讲次" + str(i), "class_begin": 1790000000} for i in range(1, 120)]}}}}}
        if path.endswith("get-sub-info"):
            return {"data": {"content": {"save_playback": {"contents": VIDEO}}}}
        if path.endswith("search-ppt"):
            return {"total": 1, "list": [{"id": 1, "content": json.dumps({"pptimgurl": IMAGE})}]}
        raise AssertionError(path)

    def resource(self):
        course = self.adapter.catalog(None, None, "")["items"][0]
        return self.adapter.catalog(course["id"], None, "")["items"][0]["id"]

    def media(self, url, host, development):
        self.assertFalse(development)
        self.assertEqual(host, direct._host(url))
        self.assertEqual(list(self.root.glob("**/source.zip")), [])
        return Response(url, PNG if host == "slides.example.test" else MP4,
                        "image/png" if host == "slides.example.test" else "video/mp4")

    def test_plan_only_reveals_domains_and_does_not_download(self):
        with patch.object(direct, "_media_request") as download:
            plan = self.adapter.plan(self.resource())
        download.assert_not_called()
        self.assertEqual(plan["media_hosts"], ["slides.example.test", "vod.example.test"])
        wire = json.dumps(plan)
        self.assertNotIn(SECRET, wire)
        self.assertNotIn("https://", wire)
        self.assertNotIn(SECRET, json.dumps(list(self.state.values.values())))

    def test_catalog_paginates_without_dropping_resources(self):
        course = self.adapter.catalog(None, None, "")["items"][0]["id"]
        cursor, titles = None, []
        while True:
            page = self.adapter.catalog(course, cursor, "")
            self.assertLess(len(json.dumps(page, ensure_ascii=False).encode()), 16 * 1024)
            titles += [item["title"] for item in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(len(set(titles)), 119)

    def test_unconfirmed_hosts_expired_plan_and_changed_session_reject_before_download(self):
        plan = self.adapter.plan(self.resource())
        with patch.object(direct, "_media_request") as download:
            with self.assertRaisesRegex(direct.DirectError, "HOST_NOT_ALLOWED"):
                self.adapter.collect(plan["plan_id"], ["vod.example.test"], str(self.root / "a.zip"), lambda _: None)
            self.state.values["plan", plan["plan_id"]]["expires_at"] = 0
            with self.assertRaisesRegex(direct.DirectError, "PLAN_EXPIRED"):
                self.adapter.collect(plan["plan_id"], plan["media_hosts"], str(self.root / "a.zip"), lambda _: None)
            self.state.values["plan", plan["plan_id"]]["expires_at"] = int(time.time()) + 99
            self.state.current["id"] = "changed"
            with self.assertRaisesRegex(direct.DirectError, "AUTH_REQUIRED"):
                self.adapter.collect(plan["plan_id"], plan["media_hosts"], str(self.root / "a.zip"), lambda _: None)
        download.assert_not_called()

    def test_refreshed_resource_cannot_expand_hosts(self):
        plan = self.adapter.plan(self.resource())
        with patch.object(self.adapter, "_assets", return_value=(VIDEO.replace("vod.example", "other.example"), [])), \
             patch.object(direct, "_media_request") as download:
            with self.assertRaisesRegex(direct.DirectError, "HOST_NOT_ALLOWED"):
                self.adapter.collect(plan["plan_id"], plan["media_hosts"], str(self.root / "a.zip"), lambda _: None)
        download.assert_not_called()

    def test_same_host_asset_changes_require_new_plan_but_signature_refresh_does_not(self):
        plan = self.adapter.plan(self.resource())
        self.assertEqual(direct._revision(VIDEO, [{"url": IMAGE}]),
                         direct._revision(VIDEO.replace(SECRET, "REFRESHED"), [{"url": IMAGE.replace(SECRET, "REFRESHED")}]))
        with patch.object(self.adapter, "_assets", return_value=(VIDEO.replace("record.mp4", "different.mp4"), [{"url": IMAGE}])), \
             patch.object(direct, "_media_request") as download:
            with self.assertRaisesRegex(direct.DirectError, "PLAN_EXPIRED"):
                self.adapter.collect(plan["plan_id"], plan["media_hosts"], str(self.root / "a.zip"), lambda _: None)
        download.assert_not_called()

    def test_collect_reuses_converter_and_standard_package_structure(self):
        plan = self.adapter.plan(self.resource())
        target = self.root / "standard.zip"
        stages = []
        def convert_public_assets(source, *args, **kwargs):
            self.assertNotIsInstance(source, io.BytesIO)
            self.assertIn(SECRET, kwargs["media_url"])
            with zipfile.ZipFile(source) as archive:
                self.assertNotIn("private/source.json", archive.namelist())
                for name in archive.namelist():
                    self.assertNotIn(SECRET.encode(), archive.read(name))
            source.seek(0)
            return collector._convert_stream(source, *args, **kwargs)
        with patch.object(direct, "_media_request", side_effect=self.media), \
             patch.object(collector, "_media_request", side_effect=self.media), \
             patch.object(collector, "_resolved_host", return_value="127.0.0.1"), \
             patch.object(direct, "_convert_stream", side_effect=convert_public_assets):
            result = self.adapter.collect(plan["plan_id"], plan["media_hosts"], str(target), stages.append)
        self.assertEqual(result, {"type": "result", "output_path": str(target)})
        self.assertEqual(stages, ["preparing", "downloading", "packaging", "publishing"])
        with zipfile.ZipFile(target) as archive:
            self.assertNotIn("private/source.json", archive.namelist())
            for name in archive.namelist():
                self.assertNotIn(SECRET.encode(), archive.read(name))
        report = inspect(target)
        self.assertEqual(report.source_kind, "asset_zip")
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_failed_media_does_not_publish(self):
        plan = self.adapter.plan(self.resource())
        target = self.root / "standard.zip"
        with patch.object(direct, "_media_request", side_effect=OSError(SECRET)):
            with self.assertRaises(OSError):
                self.adapter.collect(plan["plan_id"], plan["media_hosts"], str(target), lambda _: None)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def invoke(self, method, params=None):
        request = {"protocol_version": 2, "method": method, "params": params or {}}
        output = io.BytesIO()
        with patch.object(protocol.sys, "stdin", type("In", (), {"buffer": io.BytesIO(json.dumps(request).encode() + b"\n")})()), \
             patch.object(protocol.sys, "stdout", type("Out", (), {"buffer": output})()), \
             patch.object(direct, "Adapter", return_value=self.adapter):
            code = protocol.run()
        return code, [json.loads(line) for line in output.getvalue().splitlines()], output.getvalue()

    def test_protocol_identity_and_safe_errors(self):
        code, frames, _ = self.invoke("identity")
        self.assertEqual(code, 0)
        self.assertEqual(frames[0]["protocol_version"], 2)
        self.assertEqual(frames[0]["capabilities"], direct.CAPABILITIES)
        with patch.object(self.adapter, "plan", side_effect=ValueError(SECRET)):
            code, frames, wire = self.invoke("plan", {"resource_id": "opaque"})
        self.assertEqual(code, 1)
        self.assertEqual(frames[0]["code"], "COLLECTOR_FAILED")
        self.assertNotIn(SECRET.encode(), wire)

    def test_auth_logout_invalidates_resource_and_plans(self):
        resource = self.resource()
        self.assertEqual(self.adapter.auth_logout()["status"], "signed_out")
        with self.assertRaisesRegex(direct.DirectError, "AUTH_REQUIRED"):
            self.adapter.plan(resource)

    def test_login_only_persists_token_and_uses_ephemeral_native_window(self):
        cookie = SimpleCookie()
        cookie["session"] = '{i:0;s:6:"_token";i:1;s:20:"' + SECRET + '";}'
        cookie["session"]["domain"] = ".zju.edu.cn"
        class Event:
            def __iadd__(self, callback):
                self.callback = callback
                return self
        class Window:
            events = types.SimpleNamespace(closed=Event())
            gui = types.SimpleNamespace(renderer="edgechromium" if sys.platform == "win32" else "wkwebview")
            def get_current_url(self):
                return "https://classroom.zju.edu.cn/"
            def get_cookies(self):
                return [cookie]
            def destroy(self):
                self.events.closed.callback()
        window = Window()
        options = []
        def start(callback, **kwargs):
            options.append(kwargs)
            callback()
        webview = types.SimpleNamespace(create_window=lambda *a, **kw: window, start=start)
        with patch.dict("sys.modules", {"webview": webview}):
            self.assertEqual(self.adapter.auth_login(), {"type": "auth", "status": "signed_in"})
        self.assertEqual(self.state.current, {"token": SECRET, "id": "session-b"})
        self.assertEqual(options, [{"private_mode": True, "debug": False,
                                    "gui": "edgechromium" if sys.platform == "win32" else "cocoa"}])

    def test_login_close_reports_cancellation(self):
        class Event:
            def __iadd__(self, callback):
                self.callback = callback
                return self
        event = Event()
        window = types.SimpleNamespace(events=types.SimpleNamespace(closed=event))
        webview = types.SimpleNamespace(create_window=lambda *a, **kw: window,
            start=lambda callback, **kwargs: event.callback())
        with patch.dict("sys.modules", {"webview": webview}):
            with self.assertRaisesRegex(direct.DirectError, "AUTH_CANCELLED"):
                self.adapter.auth_login()


if __name__ == "__main__":
    unittest.main()
