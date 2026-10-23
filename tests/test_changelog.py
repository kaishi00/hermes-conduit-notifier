"""CHANGELOG.md is what Conduit's update notice shows: the entries between
the host's version and the one an update installs. Conduit reads it from
main with a fixed parser, so a version bump without an entry, or an entry
it can't read, shows nothing for that version."""

import datetime
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSION = re.compile(r"^version:\s*[\"']?([0-9]+(?:\.[0-9]+)*)", re.MULTILINE)
HEADING = re.compile(r"^## ([0-9]+\.[0-9]+\.[0-9]+) \((\d{4}-\d{2}-\d{2})\)$")
SECTIONS = ["Security", "Fixes", "New and improved"]
# A version bump changes these together, with a new entry on top of CHANGELOG.md.
VERSION_FILES = "plugin.yaml, events.py, dashboard/manifest.json and tests/test_clarify_loop.py"


def _parts(version):
    return tuple(int(part) for part in version.split("."))


def _entries():
    """[(version, date, [(section, [lines])])] in file order."""
    entries = []
    for line in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            match = HEADING.match(line)
            assert match, f"heading must read `## X.Y.Z (YYYY-MM-DD)`: {line!r}"
            entries.append((match.group(1), match.group(2), []))
        elif not entries:
            continue
        elif line.startswith("### "):
            entries[-1][2].append((line[4:], []))
        elif line.startswith("- "):
            assert entries[-1][2], f"{entries[-1][0]}: a change outside a section: {line!r}"
            entries[-1][2][-1][1].append(line)
        elif line.startswith("  ") and entries[-1][2] and entries[-1][2][-1][1]:
            continue  # a wrapped change
        else:
            assert not line.strip(), f"{entries[-1][0]}: unexpected line {line!r}"
    return entries


def test_plugin_version_has_an_entry_on_top():
    version = VERSION.search((ROOT / "plugin.yaml").read_text(encoding="utf-8")).group(1)
    entries = _entries()
    top = entries[0][0] if entries else "missing"
    assert top == version, (
        f"plugin.yaml is {version} but the top CHANGELOG.md entry is {top}: a version bump "
        f"changes {VERSION_FILES} and adds `## {version} (YYYY-MM-DD)` at the top of CHANGELOG.md")


def test_entries_are_newest_first_and_dated():
    entries = _entries()
    versions = [version for version, _, _ in entries]
    # Two PRs that bump from the same main pick the same number; the second
    # one's run (the PR merged into main) then has that entry twice.
    taken = sorted({version for version in versions if versions.count(version) > 1}, key=_parts)
    assert not taken, (
        f"CHANGELOG.md has {', '.join(taken)} more than once. If another PR took that version on "
        f"main, take the next free one: {VERSION_FILES}, plus its entry here. If this PR added "
        "its entry twice, drop one.")
    parts = [_parts(version) for version in versions]
    assert parts == sorted(parts, reverse=True), "CHANGELOG.md entries go newest first"
    for version, date, _ in entries:
        datetime.date.fromisoformat(date)


def test_sections_are_known_in_order_and_not_empty():
    for version, _, sections in _entries():
        names = [name for name, _ in sections]
        assert names, f"{version} has no changes"
        assert all(name in SECTIONS for name in names), f"{version}: sections are {SECTIONS}, got {names}"
        assert names == sorted(names, key=SECTIONS.index) and len(set(names)) == len(names), (
            f"{version}: sections go {SECTIONS} in that order, once each")
        for name, lines in sections:
            assert lines, f"{version} {name} is empty: leave the section out"
