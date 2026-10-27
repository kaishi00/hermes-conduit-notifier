"""The Folder tab's routes on a real dashboard (hermes-conduit#1389): Hermes'
own file rules reach them (its secret files stay out), and a change lands in
the folder the request named, on the profile it named."""

from __future__ import annotations

import base64


def _tree(tmp_path):
    root = tmp_path / "workspace"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "guide.md").write_text("# Guide\nGrouping videos by topic\n")
    (root / ".env").write_text("SECRET=1\n")
    (root / "auth.json").write_text("{}\n")
    return root


def test_listing_leaves_out_hermes_secret_files(dashboard, tmp_path):
    root = _tree(tmp_path)
    status, body = dashboard.get("workspace/list", root=str(root))
    assert status == 200, body
    assert [entry["name"] for entry in body["entries"]] == ["docs"], body


def test_search_finds_a_line_and_no_secret(dashboard, tmp_path):
    root = _tree(tmp_path)
    status, body = dashboard.get("workspace/search", root=str(root), q="grouping")
    assert status == 200, body
    assert [m["path"] for m in body["matches"]] == [str((root / "docs" / "guide.md").resolve())], body
    status, body = dashboard.get("workspace/search", root=str(root), q="SECRET")
    assert status == 200 and body["matches"] == [], body


def test_changes_land_in_the_named_folder_on_a_secondary_profile(dashboard, tmp_path):
    root = _tree(tmp_path)
    data = base64.b64encode(b"hello").decode()
    status, body = dashboard.request("POST", "workspace/upload", profile="second",
                                     body={"root": str(root), "path": str(root / "docs"), "name": "hi.txt",
                                           "data": data})
    assert status == 200, body
    assert (root / "docs" / "hi.txt").read_bytes() == b"hello"
    status, body = dashboard.request("POST", "workspace/delete", profile="second",
                                     body={"root": str(root), "path": str(tmp_path)})
    assert status == 403, body
    assert tmp_path.exists()
