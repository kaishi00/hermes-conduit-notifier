"""Hermes scans the whole checkout before it installs or updates this plugin,
and refuses one with a "dangerous" verdict, --force or not. A literal that
looks like a token is enough: upstream/conftest.py's fixed session token
kept every host on 0.84.1 when 0.95.0 came out. This applies the scan's
hardcoded_secret pattern (hermes-agent tools/skills_guard.py) to the files
the scan reads at full severity; test folders are demoted there, so they
are left out here too."""

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARDCODED_SECRET = re.compile(
    r'(?:api[_-]?key|token|secret|password)\s*[=:]\s*["\']'
    r'(?!(?-i:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)["\'])'
    r'[A-Za-z0-9+/=_-]{20,}',
    re.IGNORECASE,
)
TEST_FOLDERS = {"tests", "test"}
SKIP_FOLDERS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".venv", "venv"}


def _scanned_files():
    try:
        listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
        paths = [ROOT / name for name in listed.decode("utf-8").split("\0") if name]
    except (OSError, subprocess.CalledProcessError):
        paths = [path for path in ROOT.rglob("*") if not SKIP_FOLDERS & set(path.relative_to(ROOT).parts)]
    for path in paths:
        if path.is_file() and not TEST_FOLDERS & set(path.relative_to(ROOT).parts[:-1]):
            yield path


def test_no_literal_hermes_reads_as_a_secret():
    found = []
    for path in _scanned_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if HARDCODED_SECRET.search(line):
                found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not found, ("Hermes' install scan would refuse the plugin (hardcoded_secret); build these "
                       "at run time instead:\n" + "\n".join(found))
