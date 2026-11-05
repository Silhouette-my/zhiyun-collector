"""Opt-in byte progress, frame budgets and staging cleanup; synthetic media only."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from handouter_zhiyun_collector import __main__ as cli, collector, direct, protocol
from package_assertions import inspect

SECRET = "PRIVATE_SYNTHETIC"
VIDEO = "https://media.example.test/clip.mp4?auth_key=" + SECRET
MP4_HEADER = b"\x00\x00\x00\x10ftypisom"


class ChunkedResponse:
    """A media response that hands out small chunks and may announce its length."""

    def __init__(self, url, data, content_type="video/mp4", *, length=True, chunk=1000, on_read=None):
        self.url = url
        self._stream = io.BytesIO(data)
        self._chunk = chunk
        self._on_read = on_read
        self.headers = {"Content-Type": content_type}
        if length:
            self.headers["Content-Length"] = str(len(data))

    def geturl(self):
        return self.url

    def read(self, amount):
        if self._on_read:
            self._on_read()
        return self._stream.read(min(amount, self._chunk))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self._stream.close()


class FakeClock:
    def __init__(self, step):
        self.now = 1000.0
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


class Wire:
    """Run ``protocol.run`` with a bounded fake stdin/stdout."""

    @staticmethod
    def invoke(request):
        stdin = io.BytesIO(json.dumps(request, ensure_ascii=False).encode() + b"\n")
        stdout = io.BytesIO()
        with patch.object(protocol.sys, "stdout", type("Out", (), {"buffer": stdout})()), \
             patch.object(protocol.sys, "stdin", type("In", (), {"buffer": stdin})()):
            code = protocol.run()
        wire = stdout.getvalue().decode("utf-8")
        return code, [json.loads(line) for line in wire.splitlines()], wire


def byte_frames(frames):
    return [frame for frame in frames if "completed_bytes" in frame]


class ConvertProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.out = self.root / "out"
        self.out.mkdir()
        self.source = self.root / "browser.zip"
        self.write_export(VIDEO)

    def write_export(self, url):
        with zipfile.ZipFile(self.source, "w") as archive:
            archive.writestr("private/source.json", json.dumps({"videoUrl": url}))
            archive.writestr("course_info.json", json.dumps({"courseName": "合成课程", "lectureDate": "2026-09-27"}))
            archive.writestr("slides_meta.json", "[]")

    def request(self, target, **extra):
        details = collector.inspect_export(self.source, require_https=True)
        params = {"source_path": str(self.source), "source_sha256": details["source_sha256"],
                  "allowed_media_hosts": details["media_hosts"], "output_path": str(target), **extra}
        return {"protocol_version": 1, "method": "convert", "params": params}

    def run_convert(self, request, response_factory, step=0.05):
        with patch.object(collector, "_resolved_host", return_value="127.0.0.1"), \
             patch.object(collector, "_media_request", side_effect=response_factory), \
             patch.object(protocol.time, "monotonic", FakeClock(step)):
            return Wire.invoke(request)

    def test_without_opt_in_frames_are_unchanged(self):
        media = MP4_HEADER + b"0" * 200_000
        for extra in ({}, {"progress_bytes": False}):
            target = self.out / f"plain-{len(extra)}.zip"
            code, frames, wire = self.run_convert(self.request(target, **extra),
                                                  lambda url, *_: ChunkedResponse(url, media))
            self.assertEqual(code, 0, frames)
            self.assertEqual(frames, [{"type": "progress", "stage": "preparing"},
                                      {"type": "progress", "stage": "downloading"},
                                      {"type": "progress", "stage": "packaging"},
                                      {"type": "progress", "stage": "publishing"},
                                      {"type": "result", "output_path": str(target)}])
            self.assertNotIn("bytes", wire)

    def test_opt_in_reports_throttled_bytes_for_known_length(self):
        media = MP4_HEADER + b"0" * 1_000_000
        target = self.out / "bytes.zip"
        # One clock tick per read: ~1000 chunks over ~50 simulated seconds.
        code, frames, wire = self.run_convert(self.request(target, progress_bytes=True),
                                              lambda url, *_: ChunkedResponse(url, media))
        self.assertEqual(code, 0, frames)
        self.assertEqual(frames[:2], [{"type": "progress", "stage": "preparing"},
                                      {"type": "progress", "stage": "downloading"}])
        self.assertEqual(frames[-3:], [{"type": "progress", "stage": "packaging"},
                                       {"type": "progress", "stage": "publishing"},
                                       {"type": "result", "output_path": str(target)}])
        reported = byte_frames(frames)
        self.assertGreater(len(reported), 5)
        self.assertLessEqual(len(frames) - 1, protocol.MAX_PROGRESS_FRAMES)
        previous = 0
        for frame in reported:
            self.assertEqual(set(frame), {"type", "stage", "completed_bytes", "total_bytes"})
            self.assertEqual((frame["type"], frame["stage"]), ("progress", "downloading"))
            self.assertEqual(frame["total_bytes"], len(media))
            self.assertGreaterEqual((frame["completed_bytes"] - previous) * 100, len(media))
            previous = frame["completed_bytes"]
        self.assertNotIn(SECRET, wire)
        self.assertEqual(inspect(target).source_kind, "asset_zip")

    def test_unknown_length_reports_null_total(self):
        media = MP4_HEADER + b"0" * 100_000
        target = self.out / "unknown.zip"
        code, frames, _ = self.run_convert(self.request(target, progress_bytes=True),
                                           lambda url, *_: ChunkedResponse(url, media, length=False), step=0.4)
        self.assertEqual(code, 0, frames)
        reported = byte_frames(frames)
        self.assertTrue(reported)
        self.assertTrue(all(frame["total_bytes"] is None for frame in reported))
        self.assertTrue(all(isinstance(frame["completed_bytes"], int) and frame["completed_bytes"] > 0
                            for frame in reported))

    def test_hls_reports_cumulative_bytes_without_total(self):
        playlist = "https://media.example.test/clip.m3u8?auth_key=" + SECRET
        self.write_export(playlist)
        segment = b"\x47" + b"0" * 30_000
        sources = {playlist: (b"#EXTM3U\n#EXTINF:2,\nseg1.ts\n#EXTINF:2,\nseg2.ts\n#EXT-X-ENDLIST\n",
                              "application/vnd.apple.mpegurl"),
                   "https://media.example.test/seg1.ts": (segment, "video/mp2t"),
                   "https://media.example.test/seg2.ts": (segment, "video/mp2t")}
        target = self.out / "hls.zip"
        code, frames, _ = self.run_convert(
            self.request(target, progress_bytes=True),
            lambda url, *_: ChunkedResponse(url, sources[url][0], sources[url][1]), step=0.5)
        self.assertEqual(code, 0, frames)
        reported = byte_frames(frames)
        self.assertTrue(reported)
        self.assertTrue(all(frame["total_bytes"] is None for frame in reported))
        counts = [frame["completed_bytes"] for frame in reported]
        self.assertEqual(counts, sorted(counts))
        self.assertGreater(counts[-1], len(segment))

    def test_progress_bytes_must_be_boolean_and_other_fields_stay_strict(self):
        target = self.out / "never.zip"
        base = self.request(target)
        bad = [{**base["params"], "progress_bytes": value} for value in ("true", 1, 0, None, [])]
        bad.append({**base["params"], "progress_bytes": True, "progress": True})
        bad.append({key: value for key, value in base["params"].items() if key != "output_path"} | {"progress_bytes": True})
        for params in bad:
            with patch.object(collector, "_media_request") as download:
                code, frames, _ = Wire.invoke({**base, "params": params})
            self.assertEqual(code, 1, params)
            self.assertEqual(frames, [{"type": "error", "code": "COLLECTOR_FAILED", "message": protocol.ERROR_MESSAGE}])
            download.assert_not_called()
        for method, params in (("inspect", {"source_path": str(self.source), "progress_bytes": True}),
                               ("identity", {"progress_bytes": True})):
            code, frames, _ = Wire.invoke({"protocol_version": 1, "method": method, "params": params})
            self.assertEqual((code, frames[-1]["type"]), (1, "error"))
        self.assertFalse(target.exists())

    def test_no_media_code_is_reported_by_convert_too(self):
        self.write_export("")
        digest = collector._sha256(self.source)
        with patch.object(collector, "_media_request") as download:
            code, frames, wire = Wire.invoke({"protocol_version": 1, "method": "convert", "params": {
                "source_path": str(self.source), "source_sha256": digest, "allowed_media_hosts": ["media.example.test"],
                "output_path": str(self.out / "none.zip"), "progress_bytes": True}})
        self.assertEqual(code, 1)
        self.assertEqual(frames, [{"type": "error", "code": "COLLECTOR_NO_MEDIA", "message": protocol.ERROR_MESSAGE}])
        download.assert_not_called()
        self.assertEqual(list(self.out.iterdir()), [])

    def test_failure_mid_download_leaves_no_staging_or_output(self):
        media = MP4_HEADER + b"0" * 50_000
        reads = []

        def broken(url, *_):
            def fail():
                reads.append(1)
                if len(reads) > 10:
                    raise OSError("connection reset; " + SECRET)
            return ChunkedResponse(url, media, on_read=fail)

        code, frames, wire = self.run_convert(self.request(self.out / "partial.zip", progress_bytes=True), broken)
        self.assertEqual(code, 1)
        self.assertEqual(frames[-1]["type"], "error")
        self.assertNotIn(SECRET, wire)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_cancel_signal_unwinds_and_removes_staging(self):
        media = MP4_HEADER + b"0" * 50_000
        reads = []

        def cancelled(url, *_):
            def cancel():
                reads.append(1)
                if len(reads) == 5:
                    # Only raise once the protocol handler is in place; the default
                    # action would end the test process instead.
                    self.assertIs(signal.getsignal(signal.SIGTERM), protocol._interrupted)
                    signal.raise_signal(signal.SIGTERM)
            return ChunkedResponse(url, media, on_read=cancel)

        before = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(SystemExit) as stopped:
            self.run_convert(self.request(self.out / "cancelled.zip", progress_bytes=True), cancelled)
        self.assertEqual(stopped.exception.code, 128 + signal.SIGTERM)
        self.assertEqual(list(self.out.iterdir()), [])
        self.assertIs(signal.getsignal(signal.SIGTERM), before)


class ByteProgressThrottleTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(protocol, "_progress_sent", 4)
        patcher.start()
        self.addCleanup(patcher.stop)

    def collect(self, calls, step, total):
        frames, times = [], []
        clock = FakeClock(step)

        def emit(frame):
            frames.append(frame)
            times.append(clock.now)
            protocol._progress_sent += 1

        reporter = protocol.ByteProgress(emit=emit, clock=clock)
        for completed in calls:
            reporter(completed, total)
        return frames, times

    def test_known_total_respects_interval_and_one_percent(self):
        total = 10_000_000
        frames, times = self.collect(range(1000, total + 1, 1000), 0.01, total)
        self.assertTrue(frames)
        self.assertLessEqual(len(frames), 100)
        self.assertTrue(all(b - a >= protocol.BYTE_FRAME_INTERVAL for a, b in zip(times, times[1:])))
        values = [0] + [frame["completed_bytes"] for frame in frames]
        self.assertTrue(all((b - a) * 100 >= total for a, b in zip(values, values[1:])))
        # Fast progress is still held to one frame per second.
        fast, fast_times = self.collect(range(1000, total + 1, 1000), 0.0001, total)
        self.assertTrue(all(b - a >= protocol.BYTE_FRAME_INTERVAL for a, b in zip(fast_times, fast_times[1:])))
        self.assertLessEqual(len(fast), 2)

    def test_unknown_total_over_hours_stays_inside_shared_budget(self):
        # 20 simulated hours, a chunk every half second.
        frames, times = self.collect(range(1, 144_001), 0.5, None)
        self.assertTrue(all(frame["total_bytes"] is None for frame in frames))
        self.assertTrue(all(b - a >= protocol.BYTE_FRAME_INTERVAL for a, b in zip(times, times[1:])))
        self.assertLessEqual(protocol._progress_sent, protocol.MAX_PROGRESS_FRAMES - protocol._RESERVED_STAGE_FRAMES)

    def test_no_frame_without_progress_or_with_invalid_numbers(self):
        frames, _ = self.collect([0, 0, 0, 0], 5, 100)
        self.assertEqual(frames, [])
        frames, _ = self.collect([1, 50, 200], 5, 100)  # more than announced: total becomes unknown
        self.assertEqual([frame["total_bytes"] for frame in frames], [100, None])

    def test_emit_enforces_progress_and_terminal_budgets(self):
        stdout = io.BytesIO()
        with patch.object(protocol.sys, "stdout", type("Out", (), {"buffer": stdout})()), \
             patch.object(protocol, "_progress_sent", 0), patch.object(protocol, "_frames_sent", 0):
            for _ in range(protocol.MAX_PROGRESS_FRAMES):
                protocol._emit({"type": "progress", "stage": "downloading"})
            with self.assertRaises(protocol.ProtocolError):
                protocol._emit({"type": "progress", "stage": "downloading"})
            protocol._emit({"type": "result", "output_path": "x"})
        self.assertEqual(len(stdout.getvalue().splitlines()), protocol.MAX_PROGRESS_FRAMES + 1)


class StagingCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def staging(self, name, files, age):
        path = self.root / name
        path.mkdir()
        stamp = time.time() - age
        for filename in files:
            (path / filename).write_bytes(b"partial")
            os.utime(path / filename, (stamp, stamp))
        os.utime(path, (stamp, stamp))
        return path

    def test_only_old_collector_staging_is_removed(self):
        day = collector.STALE_STAGING_SECONDS
        stale = self.staging(collector.STAGING_PREFIX + "old1", ["lecture.mp4", "standard.zip"], day + 60)
        empty = self.staging(collector.STAGING_PREFIX + "old2", [], day + 60)
        active = self.staging(collector.STAGING_PREFIX + "new", ["lecture.ts"], 60)
        foreign = self.staging(collector.STAGING_PREFIX + "user", ["notes.txt"], day + 60)
        other = self.staging("handouter-collection-x", ["lecture.mp4"], day + 60)
        collector.remove_stale_staging(self.root)
        self.assertFalse(stale.exists())
        self.assertFalse(empty.exists())
        for kept in (active, foreign, other):
            self.assertTrue(kept.exists(), kept)

    def test_convert_sweeps_stale_staging_beside_its_output(self):
        stale = self.staging(collector.STAGING_PREFIX + "killed", ["lecture.mp4"], collector.STALE_STAGING_SECONDS + 60)
        source = self.root / "src" / "browser.zip"
        source.parent.mkdir()
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("private/source.json", json.dumps({"videoUrl": "http://127.0.0.1/m.mp4"}))
            archive.writestr("course_info.json", json.dumps({"courseName": "合成课"}))
            archive.writestr("slides_meta.json", "[]")
        target = self.root / "standard.zip"
        with patch.object(collector, "_media_request",
                          side_effect=lambda url, *_: ChunkedResponse(url, MP4_HEADER + b"0" * 100)):
            collector.convert(source, target, allowed_media_host="127.0.0.1", development_loopback=True)
        self.assertFalse(stale.exists())
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["src", "standard.zip"])

    def test_publish_falls_back_to_exclusive_rename_without_hard_links_on_windows(self):
        staged = self.root / "staged.zip"
        staged.write_bytes(b"zip")
        target = self.root / "published.zip"
        with patch.object(collector.os, "link", side_effect=OSError(1, "Incorrect function")), \
             patch.object(collector, "_WINDOWS", True):
            collector._publish(staged, target)
        self.assertEqual(target.read_bytes(), b"zip")
        with patch.object(collector.os, "link", side_effect=OSError(1, "Incorrect function")), \
             patch.object(collector, "_WINDOWS", False):
            with self.assertRaises(OSError):
                collector._publish(target, self.root / "other.zip")
        staged.write_bytes(b"new")
        with self.assertRaises(FileExistsError):
            collector._publish(staged, target)
        self.assertEqual(target.read_bytes(), b"zip")

    @unittest.skipUnless(sys.platform == "win32", "Windows rename semantics")
    def test_windows_rename_fallback_never_replaces_existing_output(self):
        staged = self.root / "staged.zip"
        staged.write_bytes(b"new")
        target = self.root / "published.zip"
        target.write_bytes(b"old")
        with patch.object(collector.os, "link", side_effect=OSError(1, "Incorrect function")):
            with self.assertRaises(FileExistsError):
                collector._publish(staged, target)
        self.assertEqual(target.read_bytes(), b"old")


class DirectProgressTests(unittest.TestCase):
    """v2 collect shares the downloader and the same opt-in flag."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.adapter = direct.Adapter(_MemoryState())
        api = patch.object(direct, "_api", side_effect=self.api)
        api.start()
        self.addCleanup(api.stop)
        self.video = "https://vod.example.test/record.mp4?auth_key=" + SECRET
        self.image = "https://slides.example.test/one.png?auth_key=" + SECRET

    def api(self, host, path, token, params=None):
        if path.endswith("infosimple"):
            return {"params": {"id": 1, "tenant_id": 112, "phone": "123", "account": "user"}}
        if path.endswith("searchlist"):
            return {"total": {"list": [{"course_id": "9", "title": "合成课程"}], "total": 1}}
        if path.endswith("get-course-detail"):
            return {"data": {"sub_list": {"2026": {"10": {"1": [{"id": 1, "sub_title": "讲次1", "class_begin": 1790000000}]}}}}}
        if path.endswith("get-sub-info"):
            return {"data": {"content": {"save_playback": {"contents": self.video}}}}
        if path.endswith("search-ppt"):
            return {"total": 1, "list": [{"id": 1, "content": json.dumps({"pptimgurl": self.image})}]}
        raise AssertionError(path)

    def media(self, url, *_):
        if "slides." in url:
            return ChunkedResponse(url, b"\x89PNG\r\n\x1a\n" + b"i" * 40, "image/png", chunk=1 << 20)
        return ChunkedResponse(url, MP4_HEADER + b"v" * 400_000)

    def invoke(self, params):
        request = {"protocol_version": 2, "method": "collect", "params": params}
        stdout = io.BytesIO()
        with patch.object(protocol.sys, "stdin", type("In", (), {"buffer": io.BytesIO(json.dumps(request).encode() + b"\n")})()), \
             patch.object(protocol.sys, "stdout", type("Out", (), {"buffer": stdout})()), \
             patch.object(direct, "Adapter", return_value=self.adapter), \
             patch.object(direct, "_media_request", side_effect=self.media), \
             patch.object(collector, "_media_request", side_effect=self.media), \
             patch.object(collector, "_resolved_host", return_value="127.0.0.1"), \
             patch.object(protocol.time, "monotonic", FakeClock(0.05)):
            code = protocol.run()
        wire = stdout.getvalue().decode("utf-8")
        return code, [json.loads(line) for line in wire.splitlines()], wire

    def plan(self):
        course = self.adapter.catalog(None, None, "")["items"][0]["id"]
        resource = self.adapter.catalog(course, None, "")["items"][0]["id"]
        return self.adapter.plan(resource)

    def test_identity_declares_flag(self):
        self.assertIn("progress_bytes", direct.CAPABILITIES)
        self.assertEqual(direct.CAPABILITIES[:6], ["auth_status", "auth_login", "auth_logout", "catalog", "plan", "collect"])

    def test_collect_with_and_without_flag(self):
        for flag in (None, True):
            plan = self.plan()
            target = self.root / f"standard-{flag}.zip"
            params = {"plan_id": plan["plan_id"], "allowed_media_hosts": plan["media_hosts"], "output_path": str(target)}
            if flag is not None:
                params["progress_bytes"] = flag
            code, frames, wire = self.invoke(params)
            self.assertEqual(code, 0, frames)
            stages = [frame["stage"] for frame in frames if frame["type"] == "progress" and "completed_bytes" not in frame]
            self.assertEqual(stages, ["preparing", "downloading", "packaging", "publishing"])
            reported = byte_frames(frames)
            if flag:
                self.assertTrue(reported)
                self.assertTrue(all(frame["stage"] == "downloading" and frame["total_bytes"] == 400_000 + len(MP4_HEADER)
                                    for frame in reported))
            else:
                self.assertEqual(reported, [])
            self.assertNotIn(SECRET, wire)
            self.assertEqual(frames[-1], {"type": "result", "output_path": str(target)})

    def test_collect_rejects_bad_flag_and_other_methods_reject_it(self):
        plan = self.plan()
        params = {"plan_id": plan["plan_id"], "allowed_media_hosts": plan["media_hosts"],
                  "output_path": str(self.root / "x.zip"), "progress_bytes": "yes"}
        code, frames, _ = self.invoke(params)
        self.assertEqual((code, frames[-1]["code"]), (1, "COLLECTOR_FAILED"))
        code, frames, _ = self.invoke({**params, "progress_bytes": True, "extra": 1})
        self.assertEqual(code, 1)
        stdout = io.BytesIO()
        request = {"protocol_version": 2, "method": "plan", "params": {"resource_id": "x", "progress_bytes": True}}
        with patch.object(protocol.sys, "stdin", type("In", (), {"buffer": io.BytesIO(json.dumps(request).encode() + b"\n")})()), \
             patch.object(protocol.sys, "stdout", type("Out", (), {"buffer": stdout})()):
            self.assertEqual(protocol.run(), 1)
        self.assertFalse((self.root / "x.zip").exists())


class _MemoryState:
    def __init__(self):
        self.current = {"token": SECRET, "id": "session-a"}
        self.values = {}

    def session(self):
        return self.current

    def put(self, kind, value):
        identifier = f"{len(self.values) + 1:048x}"
        self.values[kind, identifier] = copy.deepcopy(value)
        return identifier

    def get(self, kind, identifier):
        return copy.deepcopy(self.values[kind, identifier])


class CliEncodingTests(unittest.TestCase):
    def test_json_output_survives_a_gbk_console(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="gbk", newline="\n")
        value = {"course_title": "合成课程 \U0001f4da", "output": "E:\\课程\\x.zip"}
        with contextlib.redirect_stdout(stream):
            cli._print_json(value)
        stream.flush()
        self.assertEqual(json.loads(raw.getvalue().decode("gbk")), value)
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="gbk", newline="\n")
        with contextlib.redirect_stdout(stream):
            cli._print_json({"course_title": "普通物理学Ⅱ（H）"})
        stream.flush()
        # Encodable text keeps readable characters.
        self.assertIn("普通物理学".encode("gbk"), raw.getvalue())


if __name__ == "__main__":
    unittest.main()
