#!/usr/bin/env python3
"""Build a separate Zhiyun collector wheel; never include it in the GUI."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "handouter_zhiyun_collector"


def _project_metadata(root: Path = ROOT) -> tuple[str, list[str]]:
    """Version from the package (single source), pinned dependencies from pyproject."""
    init = (root / "src" / PACKAGE / "__init__.py").read_text(encoding="utf-8")
    version = re.search(r'^__version__ = "([^"]+)"$', init, re.M).group(1)
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return version, list(project["dependencies"])


VERSION, DEPENDENCIES = _project_metadata()
CERTIFI_VERSION = next(item.split("==", 1)[1] for item in DEPENDENCIES if item.startswith("certifi=="))
ALLOW = ("__init__.py", "__main__.py", "collector.py", "protocol.py", "direct.py", "THIRD_PARTY_NOTICES.txt")


def collect(root: Path = ROOT) -> dict[str, bytes]:
    base = root / "src" / PACKAGE
    files = {}
    for name in ALLOW:
        path = base / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing collector source {name}")
        files[f"{PACKAGE}/{name}"] = path.read_bytes()
    userscript = base / "zhiyun_exporter.user.js"
    if not userscript.is_file() or userscript.is_symlink():
        raise ValueError("Missing reviewed browser collector script")
    files[f"{PACKAGE}/zhiyun_exporter.user.js"] = userscript.read_bytes()
    for path, data in files.items():
        if b"handouter_server" in data or b"zhiyun-lecture-notes" in data:
            raise ValueError(f"Private runtime or rules in collector: {path}")
    return files


def build(output: Path, *, root: Path = ROOT) -> Path:
    files = collect(root)
    files[f"{PACKAGE}/LICENSE"] = (root / "LICENSE").read_bytes()
    info = f"{PACKAGE}-{VERSION}.dist-info"
    files[f"{info}/METADATA"] = ("Metadata-Version: 2.1\nName: handouter-zhiyun-collector\nVersion: " + VERSION +
                                   "\nSummary: Standalone authorized Zhiyun asset collector\nLicense: LicenseRef-Proprietary\nRequires-Python: >=3.11,<4\n"
                                   + "".join(f"Requires-Dist: {item}\n" for item in DEPENDENCIES)).encode()
    files[f"{info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: handouter-collector-builder\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files[f"{info}/entry_points.txt"] = b"[console_scripts]\nhandouter-zhiyun-collector = handouter_zhiyun_collector.__main__:main\n"
    files[f"{info}/collector-manifest.json"] = (json.dumps({"kind": "independent-collector",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}},
        sort_keys=True, indent=2) + "\n").encode()
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for name, data in sorted(files.items()):
        writer.writerow([name, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode(), len(data)])
    writer.writerow([f"{info}/RECORD", "", ""])
    files[f"{info}/RECORD"] = record.getvalue().encode()
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{PACKAGE}-{VERSION}-py3-none-any.whl"
    with tempfile.TemporaryDirectory(prefix=".collector-build-", dir=output) as temporary:
        staged = Path(temporary) / target.name
        with zipfile.ZipFile(staged, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(files.items()):
                archive.writestr(name, data)
        with target.open("xb") as stream:
            stream.write(staged.read_bytes())
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    artifact = build(args.output_dir)
    print(json.dumps({"wheel": str(artifact), "bytes": artifact.stat().st_size,
                      "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}))
