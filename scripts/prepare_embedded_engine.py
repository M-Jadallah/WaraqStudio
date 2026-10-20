"""Prepare the immutable Windows x64 runtime. Network/npm are BUILD-TIME only.

Official binary artifacts and the engine lockfile are pinned. Run with Python on
Windows x64; no globally installed Node, npm or OpenCode is used. Outputs contain
no user state. Credentials live under the app data directory, outside this tree.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE_VERSION = "24.22.0"
OPENCODE_VERSION = "2.0.32"
NODE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip"
NODE_SHA256 = "f9683a19877e12a9b167305c8db71040d812876359de21f0d2e97c3f321f80c3"
OPENCODE_URL = "https://registry.npmjs.org/@opencode/cli-windows-x64/-/cli-windows-x64-2.0.32.tgz"
OPENCODE_SHA512 = "5VdKolj2/NkSk3BuM4qeiYpnNnGRT7szmd2RIv3+aSDPMwD9yRySYkJ2f1WIM7dTDfO5lZHAXVoipt6+i6AD4A=="


def remove_build_tree(path):
    """Remove only a checked build tree, including read-only Git archive files."""
    resolved = path.resolve()
    build = (ROOT / 'build').resolve()
    if not resolved.is_relative_to(build) or resolved == build:
        raise ValueError('Cleanup must stay inside the build directory')
    def writable(function, name, error):
        os.chmod(name, stat.S_IWRITE)
        function(name)
    shutil.rmtree(path, onexc=writable)


def digest(path, algorithm="sha256"):
    value = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def download(url, path, algorithm, expected):
    if not path.exists() or digest(path, algorithm) != expected:
        partial = path.with_suffix(path.suffix + ".partial")
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        if digest(partial, algorithm) != expected:
            partial.unlink(missing_ok=True)
            raise ValueError("Official runtime artifact integrity mismatch: " + path.name)
        partial.replace(path)
    return path


def extract_zip(path, target):
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            destination = (target / member.filename).resolve()
            if not destination.is_relative_to(target.resolve()) or member.filename.startswith(('/', '\\')):
                raise ValueError("Unsafe archive member")
        archive.extractall(target)


def write_manifest(output):
    files = {str(path.relative_to(output)).replace('\\', '/'): digest(path)
             for path in sorted(output.rglob('*')) if path.is_file() and path != output / 'manifest.json'}
    manifest = {"schema": 1, "platform": "win32-x64", "node_version": NODE_VERSION,
                "opencode_version": OPENCODE_VERSION, "plugin_entry": "plugin/dist/src/index.js",
                "files": files}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return manifest


def validate_payload(output):
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('node_version') != NODE_VERSION or manifest.get('opencode_version') != OPENCODE_VERSION:
        raise ValueError('Embedded runtime versions differ from the build pins')
    for name in ('node/node.exe', 'opencode/opencode.exe', 'plugin/dist/src/index.js', 'plugin/index.js'):
        if name not in manifest.get('files', {}):
            raise ValueError('Embedded runtime missing ' + name)
    actual_files = {str(path.relative_to(output)).replace('\\', '/') for path in output.rglob('*')
                    if path.is_file() and path != output / 'manifest.json'}
    if actual_files != set(manifest['files']):
        raise ValueError('Embedded runtime file set differs from integrity manifest')
    for name, expected in manifest['files'].items():
        path = (output / name).resolve()
        if not path.is_relative_to(output.resolve()) or not path.is_file() or digest(path) != expected:
            raise ValueError('Embedded runtime integrity mismatch: ' + name)
    return manifest


def prune_development_files(plugin):
    """Ship executable modules/native assets/licenses, without development copies."""
    plugin = plugin.resolve()
    for path in plugin.rglob('*'):
        if path.is_file() and path.suffix in ('.ts', '.tsx', '.cts', '.mts', '.map'):
            if not path.resolve().is_relative_to(plugin):
                raise ValueError('Development file escaped the plugin directory')
            path.unlink()
    # tsc's vendor/dist is a build intermediate; runtime imports dist/vendor.
    vendor = plugin / 'vendor'
    if vendor.is_dir():
        license_text = (vendor / 'LICENSE').read_bytes()
        if vendor.resolve().parent != plugin:
            raise ValueError('Unsafe vendor intermediate directory')
        shutil.rmtree(vendor)
        vendor.mkdir()
        (vendor / 'LICENSE').write_bytes(license_text)


def prepare(output, cache):
    if os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise RuntimeError('Only a native Windows x64 build is supported and tested')
    cache.mkdir(parents=True, exist_ok=True)
    node_archive = download(NODE_URL, cache / 'node-win-x64.zip', 'sha256', NODE_SHA256)
    cli_archive = download(OPENCODE_URL, cache / 'opencode-win-x64.tgz', 'sha512', base64.b64decode(OPENCODE_SHA512).hex())
    staging = output.parent / (output.name + '.staging')
    # All recursive removals are confined to explicitly resolved build directories.
    if staging.exists():
        if staging.resolve().parent != output.resolve().parent:
            raise ValueError('Unsafe staging directory')
        remove_build_tree(staging)
    staging.mkdir(parents=True)
    extract_zip(node_archive, staging / '_node')
    shutil.move(str(staging / '_node' / f'node-v{NODE_VERSION}-win-x64'), str(staging / 'node'))
    shutil.rmtree(staging / '_node')
    (staging / 'opencode').mkdir()
    with tarfile.open(cli_archive, 'r:gz') as archive:
        executable = next(member for member in archive.getmembers() if member.name.endswith('/opencode.exe') and member.isfile())
        with archive.extractfile(executable) as source, (staging / 'opencode' / 'opencode.exe').open('wb') as destination:
            shutil.copyfileobj(source, destination)
    license_url = 'https://raw.githubusercontent.com/anomalyco/opencode/v2.0.32/LICENSE'
    license_file = download(license_url, cache / 'opencode-LICENSE', 'sha256',
                            '625f0f619133f89bbbb2abe37369613dfa1885eba1e50d02170deb62bb42cb6b')
    shutil.copy2(license_file, staging / 'opencode' / 'LICENSE')
    source = ROOT / 'engine'
    if not (source / 'package-lock.json').is_file():
        raise ValueError('engine/package-lock.json is required; never resolve runtime dependencies during installation')
    plugin = staging / 'plugin'
    shutil.copytree(source, plugin, ignore=shutil.ignore_patterns('node_modules', 'dist', '.build', '.cache', '__pycache__', '*.pyc'))
    node = staging / 'node' / 'node.exe'
    npm = staging / 'node' / 'node_modules' / 'npm' / 'bin' / 'npm-cli.js'
    env = dict(os.environ)
    env['PATH'] = str(node.parent) + os.pathsep + env.get('PATH', '')
    for args in (['ci', '--ignore-scripts', '--no-audit', '--no-fund'], ['run', 'build'], ['test'],
                 ['prune', '--omit=dev', '--ignore-scripts', '--no-audit', '--no-fund']):
        subprocess.run([str(node), str(npm), *args], cwd=plugin, env=env, check=True)
    # Keep licenses and the lock/SBOM, drop source/tests from the shipped package.
    for name in ('test', 'tests', 'src', 'scripts', '.build'):
        directory = plugin / name
        if directory.is_dir():
            shutil.rmtree(directory)
    prune_development_files(plugin)
    lock = json.loads((plugin / 'package-lock.json').read_text(encoding='utf-8'))
    sbom = {"format": "waraq-runtime-sbom-v1", "components": [
        {"name": name, "version": data.get('version'), "license": data.get('license'), "integrity": data.get('integrity')}
        for name, data in sorted(lock.get('packages', {}).items()) if name and not data.get('dev')]}
    (staging / 'SBOM.json').write_text(json.dumps(sbom, indent=2) + '\n', encoding='utf-8')
    notices = staging / 'THIRD_PARTY_NOTICES.txt'
    notices.write_text('Node.js ' + NODE_VERSION + ': see node/LICENSE.\nOpenCode ' + OPENCODE_VERSION +
                       ': MIT, https://github.com/anomalyco/opencode.\nPlugin and dependencies: see plugin/vendor/LICENSE and dependency LICENSE files.\n', encoding='utf-8')
    subprocess.run([str(node), '--version'], check=True)
    subprocess.run([str(staging / 'opencode' / 'opencode.exe'), '--version'], check=True)
    # npm is a build tool only. Ship the executable and its complete license.
    for member in (staging / 'node').iterdir():
        if member.name not in ('node.exe', 'LICENSE'):
            if member.is_dir():
                shutil.rmtree(member)
            else:
                member.unlink()
    write_manifest(staging)
    validate_payload(staging)
    if output.exists():
        if output.resolve() == ROOT or output.resolve().parent != staging.resolve().parent:
            raise ValueError('Unsafe output directory')
        remove_build_tree(output)
    staging.replace(output)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build' / 'embedded-runtime')
    parser.add_argument('--cache', type=Path, default=ROOT / 'build' / 'runtime-downloads')
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.verify:
        validate_payload(args.output.resolve())
    else:
        print(prepare(args.output.resolve(), args.cache.resolve()))
