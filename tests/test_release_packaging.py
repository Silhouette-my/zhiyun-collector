"""Release packaging and workflow shape; synthetic build output, no GitHub access."""

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_release", ROOT / "scripts/package_release.py")
package_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package_release)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReleasePackagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def native(self, platform="windows-x64", version="1.2.3", exe=b"MZ synthetic"):
        native = self.root / f"native-{platform}"
        native.mkdir()
        name = "handouter-zhiyun-collector" + (".exe" if platform.startswith("windows") else "")
        (native / name).write_bytes(exe)
        for extra in ("zhiyun_exporter.user.js", "LICENSE", "THIRD_PARTY_NOTICES.txt"):
            (native / extra).write_bytes(extra.encode())
        manifest = {"kind": "independent-collector-plugin", "tool_id": "handouter-zhiyun-collector",
                    "tool_version": version, "platform": platform, "executable": name, "sha256": sha(exe)}
        data = (json.dumps(manifest, indent=2) + "\n").encode()
        (native / "plugin-manifest.json").write_bytes(data)
        (native / "plugin-manifest.sha256").write_text(sha(data) + "  plugin-manifest.json\n", encoding="ascii")
        return native, name, data

    def test_package_lists_zip_executable_and_manifest_digests(self):
        native, name, manifest = self.native()
        result = package_release.package(native, self.root / "release", "1.2.3")
        folder = "handouter-zhiyun-collector-1.2.3-windows-x64"
        archive = self.root / "release" / f"{folder}.zip"
        self.assertEqual(result["archive"], archive.name)
        self.assertEqual(result["executable_sha256"], sha(b"MZ synthetic"))
        with zipfile.ZipFile(archive) as opened:
            self.assertEqual(sorted(opened.namelist()), sorted(f"{folder}/{item.name}" for item in native.iterdir()))
            self.assertEqual(opened.read(f"{folder}/{name}"), b"MZ synthetic")
        sums = (self.root / "release" / "SHA256SUMS-windows-x64.txt").read_bytes()
        self.assertNotIn(b"\r\n", sums)
        lines = dict(reversed(line.split("  ", 1)) for line in sums.decode("ascii").splitlines())
        self.assertEqual(lines[f"{folder}/{name}"], sha(b"MZ synthetic"))
        self.assertEqual(lines[f"{folder}/plugin-manifest.json"], sha(manifest))
        self.assertEqual(lines[archive.name], sha(archive.read_bytes()))
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", value) for value in lines.values()))
        # Same build, same bytes: packaging is reproducible.
        again = package_release.package(native, self.root / "release-again", "1.2.3")
        self.assertEqual((self.root / "release-again" / again["archive"]).read_bytes(), archive.read_bytes())

    def test_macos_build_has_no_exe_suffix(self):
        native, name, _ = self.native(platform="macos-arm64")
        self.assertEqual(name, "handouter-zhiyun-collector")
        result = package_release.package(native, self.root / "release", "1.2.3")
        self.assertEqual(result["archive"], "handouter-zhiyun-collector-1.2.3-macos-arm64.zip")

    def test_refuses_mismatched_version_digest_or_extra_files(self):
        native, name, _ = self.native()
        with self.assertRaises(SystemExit):
            package_release.package(native, self.root / "r1", "9.9.9")
        (native / name).write_bytes(b"tampered")
        with self.assertRaises(SystemExit):
            package_release.package(native, self.root / "r2", "1.2.3")
        other, _, _ = self.native(platform="macos-x64")
        (other / "private.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(SystemExit):
            package_release.package(other, self.root / "r3", "1.2.3")
        self.assertFalse(any((self.root / name).exists() for name in ("r1", "r2", "r3")))


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    def job(self, name):
        start = self.text.index(f"\n  {name}:\n")
        following = re.search(r"\n  [a-z][a-z0-9_-]*:\n", self.text[start + 1:])
        return self.text[start:start + 1 + following.start()] if following else self.text[start:]

    def test_runs_only_for_version_tags(self):
        self.assertRegex(self.text, r"on:\n  push:\n    tags:\n      - 'v\*'\n")
        self.assertNotIn("pull_request", self.text)
        self.assertNotIn("workflow_dispatch", self.text)

    def test_write_permission_only_in_release_job(self):
        self.assertIn("\npermissions:\n  contents: read\n", self.text)
        self.assertEqual(self.text.count("contents: write"), 1)
        self.assertIn("contents: write", self.job("release"))
        self.assertNotIn("contents: write", self.job("build"))
        self.assertIn("persist-credentials: false", self.job("build"))

    def test_builds_both_platforms_and_publishes_checksums(self):
        build = self.job("build")
        self.assertIn("windows-latest", build)
        self.assertIn("macos-latest", build)
        self.assertIn("scripts/build_collector_plugin.py", build)
        self.assertIn("scripts/package_release.py", build)
        self.assertIn("python -m unittest discover -s tests", build)
        release = self.job("release")
        self.assertIn("needs: build", release)
        self.assertIn("SHA256SUMS", release)
        self.assertIn("sha256sum -c SHA256SUMS", release)
        self.assertIn("gh release create", release)


if __name__ == "__main__":
    unittest.main()
