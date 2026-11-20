"""Exercise PR preflight against a real disposable Git history."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize('fault', [None, 'desktop', 'manifest', 'version', 'history'])
def test_preflight_detects_submission_mistakes(tmp_path, fault):
    scripts = tmp_path / '.github/scripts'
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / '.github/scripts'
    for name in ('pr_preflight.py', 'check_pr_desktop.py'):
        shutil.copyfile(source / name, scripts / name)
    (tmp_path / 'dashboard').mkdir()
    def write(version, history=True, manifest=None):
        (tmp_path / 'plugin.yaml').write_text(f'version: {version}\n')
        (tmp_path / 'dashboard/manifest.json').write_text(json.dumps({'version': manifest or version}))
        (tmp_path / 'CHANGELOG.md').write_text(
            f'## {version} (2026-11-20)\n\n- New behavior.\n' +
            ('\n## 1.0.0 (2026-11-19)\n\n- Published behavior.\n' if history else ''))
    def git(*args):
        subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True)
    git('init')
    write('1.0.0', history=False)
    git('add', '.')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'base')
    write('1.0.0' if fault == 'version' else '1.0.1',
          history=fault != 'history', manifest='9.0.0' if fault == 'manifest' else None)
    # Preserve the exact committed history for the valid path.
    old = subprocess.check_output(['git', 'show', 'HEAD:CHANGELOG.md'], cwd=tmp_path, text=True)
    if fault != 'history':
        p = tmp_path / 'CHANGELOG.md'
        p.write_text(f'## 1.0.1 (2026-11-20)\n\n- New behavior.\n\n' + old)
    body = tmp_path / 'body.md'
    body.write_text('## Summary\nStartup memory.\n\n## Siblings\nServer API.\n\n' +
                    ('' if fault == 'desktop' else '## Desktop\nNot needed: existing server response.\n\n') +
                    '## Testing\nContract tests.\n')
    result = subprocess.run([sys.executable, str(scripts / 'pr_preflight.py'),
                             '--body-file', str(body), '--base', 'HEAD'],
                            cwd=tmp_path, capture_output=True, text=True)
    assert (result.returncode == 0) == (fault is None), result.stdout + result.stderr
