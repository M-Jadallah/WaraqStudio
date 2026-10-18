"""Hermetic payload integrity and build-pin checks; no binary download required."""
import json
import zipfile
from pathlib import Path
import pytest
from scripts.prepare_embedded_engine import (NODE_VERSION, OPENCODE_VERSION, extract_zip,
                                             validate_payload, write_manifest, prune_development_files)


def payload(tmp_path):
    for name in ('node/node.exe', 'opencode/opencode.exe', 'plugin/dist/src/index.js', 'plugin/index.js',
                 'plugin/node_modules/dependency/manifest.json', 'plugin/vendor/LICENSE'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic-' + name.encode())
    write_manifest(tmp_path)
    return tmp_path


def test_complete_payload_has_pinned_versions_and_nested_manifests(tmp_path):
    result = validate_payload(payload(tmp_path))
    assert result['node_version'] == NODE_VERSION == '24.22.0'
    assert result['opencode_version'] == OPENCODE_VERSION == '2.0.32'
    assert 'plugin/node_modules/dependency/manifest.json' in result['files']


@pytest.mark.parametrize('mutation', ('modified', 'missing', 'unexpected', 'wrong-version', 'traversal'))
def test_integrity_fails_closed_before_packaging(tmp_path, mutation):
    output = payload(tmp_path / 'payload')
    manifest = json.loads((output / 'manifest.json').read_text())
    if mutation == 'modified':
        (output / 'node/node.exe').write_bytes(b'modified')
    elif mutation == 'missing':
        (output / 'opencode/opencode.exe').unlink()
    elif mutation == 'unexpected':
        (output / 'credential.json').write_text('should never be packaged')
    elif mutation == 'wrong-version':
        manifest['opencode_version'] = 'latest'
        (output / 'manifest.json').write_text(json.dumps(manifest))
    else:
        manifest['files']['../outside'] = '00'
        (output / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        validate_payload(output)


def test_official_archive_extraction_rejects_parent_traversal(tmp_path):
    archive = tmp_path / 'unsafe.zip'
    with zipfile.ZipFile(archive, 'w') as stream:
        stream.writestr('../escape', 'bad')
    with pytest.raises(ValueError):
        extract_zip(archive, tmp_path / 'output')
    assert not (tmp_path / 'escape').exists()


def test_development_pruning_retains_runtime_modules_native_bindings_and_license(tmp_path):
    plugin=tmp_path/'plugin'
    for name in ('dist/vendor/index.js','dist/vendor/index.d.ts','dist/vendor/index.js.map',
                 'node_modules/crypto/binding.node','node_modules/crypto/index.js',
                 'node_modules/crypto/src/types.ts','vendor/LICENSE','vendor/dist/index.js'):
        path=plugin/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('fixture')
    prune_development_files(plugin)
    assert (plugin/'dist/vendor/index.js').is_file()
    assert (plugin/'node_modules/crypto/binding.node').is_file()
    assert (plugin/'node_modules/crypto/index.js').is_file()
    assert (plugin/'vendor/LICENSE').read_text()=='fixture'
    assert not (plugin/'vendor/dist').exists()
    assert not any(path.suffix in ('.ts','.map') for path in plugin.rglob('*'))
