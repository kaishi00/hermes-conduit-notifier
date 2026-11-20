#!/usr/bin/env python3
"""Check a prepared PR body and release metadata against a fetched base.

Usage: python .github/scripts/pr_preflight.py --body-file /tmp/pr.md --base upstream/main
Fetch the target branch first; this command never fetches or edits files.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from check_pr_desktop import problem


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--body-file', required=True)
    parser.add_argument('--base', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    errors = []
    body = Path(args.body_file).read_text(encoding='utf-8')
    issue = problem(body)
    if issue:
        errors.append(issue)
    for title in ('Summary', 'Siblings', 'Testing'):
        if not re.search(r'^##\s+' + title + r'\s*\n\s*\S', body, re.M):
            errors.append(f'Missing or empty {title} section')
    def base_file(name):
        return subprocess.check_output(['git', 'show', f'{args.base}:{name}'], cwd=root, text=True)
    def version(text):
        return re.search(r'^version:\s*(\S+)', text, re.M).group(1)
    current = version((root / 'plugin.yaml').read_text())
    previous = version(base_file('plugin.yaml'))
    if tuple(map(int, current.split('.'))) <= tuple(map(int, previous.split('.'))):
        errors.append(f'Plugin version {current} must be newer than base {previous}')
    if json.loads((root / 'dashboard/manifest.json').read_text())['version'] != current:
        errors.append('Plugin and dashboard manifest versions differ')
    changelog = (root / 'CHANGELOG.md').read_text()
    if not re.search(r'^## ' + re.escape(current) + r' \(\d{4}-\d{2}-\d{2}\)', changelog, re.M):
        errors.append('Current version missing from changelog')
    old = base_file('CHANGELOG.md')
    history = old[re.search(r'^## \d', old, re.M).start():].strip()
    if history not in changelog:
        errors.append('Published upstream changelog history was removed or modified')
    if errors:
        print('\n'.join('ERROR: ' + error for error in errors), file=sys.stderr)
        return 1
    print('PR description, next version, manifests and upstream changelog history verified.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
