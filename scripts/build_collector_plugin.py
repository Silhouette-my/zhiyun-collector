#!/usr/bin/env python3
"""Build a separately installed native collector plugin, never a desktop resource."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import venv
import zipfile

from build_collector import build, CERTIFI_VERSION, VERSION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    target = {('Darwin', 'arm64'): 'macos-arm64', ('Darwin', 'x86_64'): 'macos-x64',
              ('Windows', 'AMD64'): 'windows-x64'}.get((platform.system(), platform.machine()))
    if target is None:
        parser.error('Build on the native macOS or Windows target host')
    destination = args.output_dir.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    suffix = '.exe' if os.name == 'nt' else ''
    name = 'handouter-zhiyun-collector' + suffix
    env = {key: value for key, value in os.environ.items() if key not in {'PYTHONPATH', 'PYTHONHOME'}}
    with tempfile.TemporaryDirectory(prefix='collector-plugin-') as temporary:
        work = Path(temporary).resolve()
        wheel = build(work / 'wheel')
        venv.create(work / 'venv', with_pip=True)
        python = work / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        env['PYINSTALLER_CONFIG_DIR'] = str(work / 'cache')
        subprocess.run([str(python), '-I', '-m', 'pip', 'install', str(wheel), 'pyinstaller==6.16.0'],
                       check=True, cwd=work, env=env)
        ca_probe = subprocess.run([str(python), '-I', '-c',
            'import certifi,hashlib,importlib.metadata,json,pathlib; '
            'p=pathlib.Path(certifi.where()); '
            'print(json.dumps({"version":importlib.metadata.version("certifi"),'
            '"sha256":hashlib.sha256(p.read_bytes()).hexdigest()}))'],
            check=True, capture_output=True, text=True, cwd=work, env=env)
        ca_bundle = json.loads(ca_probe.stdout)
        if ca_bundle['version'] != CERTIFI_VERSION:
            raise RuntimeError('Unexpected collector CA bundle version')
        entry = work / 'entry.py'
        entry.write_text('from handouter_zhiyun_collector.__main__ import main\nraise SystemExit(main())\n', encoding='utf-8')
        subprocess.run([str(python), '-I', '-m', 'PyInstaller', '--clean', '--noconfirm', '--onefile',
                        '--collect-data=certifi', '--collect-all=webview',
                        '--collect-data=handouter_zhiyun_collector',
                        '--hidden-import=keyring.backends.macOS' if os.name != 'nt' else '--hidden-import=keyring.backends.Windows',
                        '--name', 'handouter-zhiyun-collector', '--distpath', str(work / 'dist'),
                        '--workpath', str(work / 'build'), '--specpath', str(work), str(entry)],
                       check=True, cwd=work, env=env)
        binary = work / 'dist' / name
        subprocess.run([str(python), '-I', '-c',
            'from PyInstaller.archive.readers import CArchiveReader; import sys; '
            'archive=CArchiveReader(sys.argv[1]); names=archive.toc; '
            'assert any(n.replace("\\\\","/").endswith("certifi/cacert.pem") for n in names),'
            '"CA bundle missing from frozen plugin"; '
            'assert any(n.replace("\\\\","/").endswith("handouter_zhiyun_collector/THIRD_PARTY_NOTICES.txt") for n in names),'
            '"Third-party license notice missing from frozen plugin"; '
            'modules=archive.open_embedded_archive("PYZ.pyz").toc; '
            'required={"webview", "keyring", "handouter_zhiyun_collector.direct", '
            '"webview.platforms.winforms" if sys.platform=="win32" else "webview.platforms.cocoa", '
            '"keyring.backends.Windows" if sys.platform=="win32" else "keyring.backends.macOS"}; '
            'assert required.issubset(modules), "Native login or vault module missing from frozen plugin"', str(binary)],
            check=True, cwd=work, env=env)
        request = json.dumps({'protocol_version': 1, 'method': 'identity', 'params': {}}).encode() + b'\n'
        run = subprocess.run([str(binary), 'protocol'], input=request, capture_output=True,
                             timeout=30, check=True, cwd=work, env=env)
        frames = [json.loads(line) for line in run.stdout.splitlines()]
        if (len(frames) != 1 or frames[0].get('type') != 'identity'
                or frames[0].get('tool_id') != 'handouter-zhiyun-collector'
                or frames[0].get('protocol_version') != 1 or frames[0].get('tool_version') != VERSION
                or frames[0].get('capabilities') != ['inspect', 'convert']):
            raise RuntimeError('Collector identity probe failed')
        request_v2 = json.dumps({'protocol_version': 2, 'method': 'identity', 'params': {}}).encode() + b'\n'
        probe_v2 = subprocess.run([str(binary), 'protocol'], input=request_v2, capture_output=True,
                                 timeout=30, check=True, cwd=work, env=env)
        identity_v2 = json.loads(probe_v2.stdout)
        if (identity_v2.get('protocol_version') != 2 or identity_v2.get('tool_version') != VERSION
                or identity_v2.get('tool_id') != 'handouter-zhiyun-collector'
                or identity_v2.get('capabilities') != ['auth_status', 'auth_login', 'auth_logout', 'catalog', 'plan', 'collect']):
            raise RuntimeError('Collector v2 identity probe failed')
        data = binary.read_bytes()
        with (destination / name).open('xb') as stream:
            stream.write(data)
        (destination / name).chmod(0o755)
        # Browser script is part of the separate plugin distribution only.
        with zipfile.ZipFile(wheel) as archive:
            script = archive.read('handouter_zhiyun_collector/zhiyun_exporter.user.js')
            source_manifest = json.loads(archive.read(f'handouter_zhiyun_collector-{VERSION}.dist-info/collector-manifest.json'))
        (destination / 'zhiyun_exporter.user.js').write_bytes(script)
        with zipfile.ZipFile(wheel) as archive:
            (destination / 'LICENSE').write_bytes(archive.read('handouter_zhiyun_collector/LICENSE'))
            (destination / 'THIRD_PARTY_NOTICES.txt').write_bytes(archive.read('handouter_zhiyun_collector/THIRD_PARTY_NOTICES.txt'))
        manifest = {'kind': 'independent-collector-plugin', 'tool_id': 'handouter-zhiyun-collector',
                    'protocol_version': 2, 'tool_version': VERSION, 'platform': target,
                    'executable': name, 'sha256': hashlib.sha256(data).hexdigest(),
                    'ca_bundle': ca_bundle,
                    'browser_script_sha256': hashlib.sha256(script).hexdigest(),
                    'source_files': source_manifest['files'],
                    'identity_probe': True, 'signed_release': False}
        manifest_path = destination / 'plugin-manifest.json'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        # A maintainer may publish this digest over a trusted channel. Its mere
        # presence beside a downloaded manifest does not establish trust.
        manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        (destination / 'plugin-manifest.sha256').write_text(manifest_digest + '  plugin-manifest.json\n', encoding='ascii')
        print(json.dumps(manifest))


if __name__ == '__main__':
    main()
