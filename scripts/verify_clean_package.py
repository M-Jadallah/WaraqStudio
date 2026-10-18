"""Launch a packaged app with no global tools or user configuration available."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, default=Path('Windows/WaraqStudio/WaraqStudio.exe'))
    args = parser.parse_args()
    executable = args.executable.resolve()
    with tempfile.TemporaryDirectory(prefix='waraq-clean-package-') as temporary:
        root = Path(temporary)
        system = Path(os.environ.get('SystemRoot', 'C:/Windows'))
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith(('OPENCODE', 'NODE_', 'NPM_', 'XDG_', 'WARAQ', 'OPENAI', 'ANTHROPIC'))}
        env.update(PATH=str(system / 'System32'), HOME=str(root), USERPROFILE=str(root),
                   APPDATA=str(root / 'roaming'), LOCALAPPDATA=str(root / 'local'))
        report_path = root / 'environment.json'
        try:
            subprocess.run([str(executable), '--data-dir', str(root / 'data'), '--check-environment', str(report_path)],
                           env=env, cwd=root, check=True, timeout=120)
        except subprocess.TimeoutExpired:
            progress = Path(str(report_path) + '.progress')
            print('Environment check stalled at: ' + (progress.read_text(encoding='utf-8') if progress.is_file() else 'startup'), flush=True)
            raise
        report = json.loads(report_path.read_text(encoding='utf-8'))
        assert report['embedded_integrity'], report
        assert report['embedded_engine_ready'], report
        assert report['opencode_version'] == '2.0.32', report
        assert report['node_version'] == '24.22.0', report
        assert report['plugin_version'] == '6.29.0', report
        assert report['ffmpeg_exists'] and report['mobile']['pwa_assets'], report
        for name in ('node', 'opencode'):
            assert Path(report[name]).resolve().is_relative_to(executable.parent), report
        if os.environ.get('VERSION'):
            assert report['version'] == os.environ['VERSION'], report
        subprocess.run([str(executable), '--data-dir', str(root / 'data'), '--smoke'],
                       env=env, cwd=root, check=True, timeout=120)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print('Packaged app passed with PATH limited to Windows System32 and an empty user home')


if __name__ == '__main__':
    main()
