#!/usr/bin/env python3
"""Package one native build directory as a release ZIP plus its SHA-256 lines.

Used by the tag-triggered release workflow. The input is the directory written by
``build_collector_plugin.py``; it is checked (manifest platform, version and the
executable digest) before anything is packaged. Output, in a new directory:

    handouter-zhiyun-collector-<version>-<platform>.zip
    SHA256SUMS-<platform>.txt

The checksum file uses ``sha256sum`` format and lists the ZIP itself and every
file inside it under the ZIP's top-level folder, so after extracting the ZIP
``sha256sum -c SHA256SUMS`` checks the executable and the manifest directly.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

TOOL_ID = 'handouter-zhiyun-collector'
EXPECTED_FILES = {'plugin-manifest.json', 'plugin-manifest.sha256', 'zhiyun_exporter.user.js',
                  'LICENSE', 'THIRD_PARTY_NOTICES.txt'}
PLATFORMS = {'windows-x64', 'macos-arm64', 'macos-x64'}
# Fixed member timestamp: re-packaging the same build yields the same ZIP bytes.
MEMBER_TIME = (1980, 1, 1, 0, 0, 0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def package(native_dir: Path, output_dir: Path, version: str) -> dict:
    native_dir = native_dir.resolve()
    manifest = json.loads((native_dir / 'plugin-manifest.json').read_text(encoding='utf-8'))
    platform = manifest.get('platform')
    executable = manifest.get('executable')
    if manifest.get('tool_id') != TOOL_ID or platform not in PLATFORMS:
        raise SystemExit('Unexpected plugin manifest identity or platform')
    if manifest.get('tool_version') != version:
        raise SystemExit(f'Manifest version {manifest.get("tool_version")!r} does not match release {version!r}')
    if executable != TOOL_ID + ('.exe' if platform.startswith('windows') else ''):
        raise SystemExit('Unexpected executable name in manifest')
    names = {path.name for path in native_dir.iterdir()}
    if names != EXPECTED_FILES | {executable} or any(not (native_dir / name).is_file() or (native_dir / name).is_symlink()
                                                      for name in names):
        raise SystemExit('Native build directory has unexpected contents')
    if _sha256(native_dir / executable) != manifest.get('sha256'):
        raise SystemExit('Executable digest does not match plugin-manifest.json')
    recorded = (native_dir / 'plugin-manifest.sha256').read_text(encoding='ascii').split()
    if not recorded or recorded[0] != _sha256(native_dir / 'plugin-manifest.json'):
        raise SystemExit('plugin-manifest.sha256 does not match plugin-manifest.json')

    output_dir.mkdir(parents=True, exist_ok=False)
    folder = f'{TOOL_ID}-{version}-{platform}'
    archive_path = output_dir / f'{folder}.zip'
    lines = []
    with zipfile.ZipFile(archive_path, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(names):
            info = zipfile.ZipInfo(f'{folder}/{name}', date_time=MEMBER_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if name == executable else 0o100644) << 16
            archive.writestr(info, (native_dir / name).read_bytes())
            lines.append(f'{_sha256(native_dir / name)}  {folder}/{name}')
    lines.append(f'{_sha256(archive_path)}  {archive_path.name}')
    sums = output_dir / f'SHA256SUMS-{platform}.txt'
    sums.write_text('\n'.join(sorted(lines, key=lambda line: line.split('  ', 1)[1])) + '\n',
                    encoding='ascii', newline='\n')
    return {'platform': platform, 'archive': archive_path.name, 'executable_sha256': manifest['sha256'],
            'manifest_sha256': recorded[0], 'checksums': sums.name}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--native-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--version', required=True, help='Release version without the leading v')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[0-9A-Za-z.+-]{1,64}', args.version):
        parser.error('Invalid version')
    print(json.dumps(package(args.native_dir, args.output_dir, args.version)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
