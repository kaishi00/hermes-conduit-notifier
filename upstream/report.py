"""Turns a contract run's failures into upstream-break issues, one per failure.

    python3 upstream/report.py <junit.xml> --ref <hermes tag> [--changed <file>] [--dry-run]

Each failure is keyed by its test and failure signature (the first line of
its message, with paths and numbers that change between runs taken out).
The key is kept as a marker in the issue body, so a failure that is already
open gets a comment naming the new tag instead of a second issue, and only a
new contract or a new way of failing opens one. A known failure that starts
passing fails its test ("no longer fails"), which is a new signature and so
its own issue. Needs `gh` with issues: write, unless --dry-run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

LABEL = "upstream-break"
MARKER = "<!-- upstream-break:{key} -->"
MARKER_RE = re.compile(r"<!-- upstream-break:([0-9a-f]{12}) -->")


def signature(message: str) -> str:
    """The failure's first line without what changes between runs."""
    line = (message or "").strip().splitlines()[0] if (message or "").strip() else "failed"
    line = re.sub(r"(/[\w.@+-]+){2,}", "<path>", line)
    line = re.sub(r"\b0x[0-9a-f]+\b", "<addr>", line)
    line = re.sub(r"(?<![\w/])\d{6,}\b", "<n>", line)  # ids and timestamps, not status or close codes
    return line[:200]


def failures(junit: Path) -> List[Dict[str, str]]:
    found, keys = [], set()
    for case in ET.parse(junit).getroot().iter("testcase"):
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is None:
                continue
            # One contract per test function: its parametrized cases share an issue.
            test = f"{case.get('classname', '')}.{case.get('name', '').split('[')[0]}".lstrip(".")
            sig = signature(node.get("message") or node.text or "")
            if tag == "error":
                # A setup error (the dashboard didn't start) fails every test the same
                # way: one harness issue, not one per test.
                failure = harness_failure(sig, (node.text or "")[-3000:])
            else:
                key = hashlib.sha1(f"{test}\n{sig}".encode()).hexdigest()[:12]
                failure = {"test": test, "signature": sig, "key": key, "detail": (node.text or "")[-3000:]}
            key = failure["key"]
            if key in keys:
                continue
            keys.add(key)
            found.append(failure)
    return found


def harness_failure(sig: str, detail: str = "") -> Dict[str, str]:
    key = hashlib.sha1(f"harness\n{sig}".encode()).hexdigest()[:12]
    return {"test": "harness", "signature": sig, "key": key, "detail": detail or "See the run."}


def gh(*args: str, input: Optional[str] = None) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, input=input).stdout


def open_issues() -> Dict[str, Dict]:
    """Open upstream-break issues by key."""
    raw = gh("issue", "list", "--label", LABEL, "--state", "open", "--limit", "200", "--json", "number,body")
    by_key = {}
    for issue in json.loads(raw or "[]"):
        match = MARKER_RE.search(issue.get("body") or "")
        if match:
            by_key[match.group(1)] = issue
    return by_key


def issue_body(failure: Dict[str, str], ref: str, run_url: str, changed: str) -> str:
    parts = [
        MARKER.format(key=failure["key"]),
        f"The upstream contract test `{failure['test']}` fails on Hermes `{ref}`.",
        "",
        f"**Signature:** `{failure['signature']}`",
        f"**First seen:** `{ref}` ([run]({run_url}))",
        "",
        "Later Hermes tags that fail the same way are added below as comments; a different failure opens "
        "its own issue. `upstream/contract.yml` says what each integration point feeds in Conduit.",
    ]
    if changed:
        parts += ["", "<details><summary>Watched upstream files changed since the previous tag</summary>", "",
                  "```", changed.strip(), "```", "</details>"]
    parts += ["", "<details><summary>Failure</summary>", "", "```", failure["detail"].strip(), "```", "</details>"]
    return "\n".join(parts)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("junit", type=Path, nargs="?")
    parser.add_argument("--no-results", action="store_true",
                        help="the run left no results: Hermes didn't install or the tests never started")
    parser.add_argument("--ref", required=True)
    parser.add_argument("--run-url", default="")
    parser.add_argument("--changed", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv[1:])

    if args.no_results:
        found = [harness_failure("no test results: Hermes didn't install or the tests never started")]
    else:
        found = failures(args.junit)
    changed = args.changed.read_text() if args.changed and args.changed.exists() else ""
    if not found:
        print(f"No contract failures on {args.ref}.")
        return 0
    known = {} if args.dry_run else open_issues()
    for failure in found:
        issue = known.get(failure["key"])
        if issue is None:
            title = f"Upstream contract: {failure['test'].rsplit('.', 1)[-1]} fails on Hermes {args.ref}"
            print(f"new  {failure['key']}  {failure['test']}: {failure['signature']}")
            if not args.dry_run:
                gh("issue", "create", "--label", LABEL, "--title", title[:250], "--body-file", "-",
                   input=issue_body(failure, args.ref, args.run_url, changed))
            continue
        number = str(issue["number"])
        comments = json.loads(gh("issue", "view", number, "--json", "comments"))["comments"]
        noted = f"`{args.ref}`"
        if noted in (issue.get("body") or "") or any(noted in c.get("body", "") for c in comments):
            print(f"seen #{number}  {failure['key']}  already noted for {args.ref}")
            continue
        print(f"same #{number}  {failure['key']}  {failure['test']}")
        if not args.dry_run:
            gh("issue", "comment", number, "--body-file", "-",
               input=f"Still failing the same way on `{args.ref}`" + (f" ([run]({args.run_url}))." if args.run_url else "."))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
