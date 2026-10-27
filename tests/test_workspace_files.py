import base64
import importlib.util
import os
import pathlib
import subprocess
import sys
import time
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path("/nonexistent-hermes-home")
    sys.modules["hermes_constants"] = _hermes_constants

_spec = importlib.util.spec_from_file_location("conduit_workspace_files_test", ROOT / "workspace_files.py")
wf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wf)

_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_workspace", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "project"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "guide.md").write_text("# Guide\nGrouping videos by topic\n")
    (root / "notes.txt").write_text("nothing here\n")
    (root / ".env").write_text("SECRET=1\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("grouping videos\n")
    (tmp_path / "outside.txt").write_text("grouping videos\n")
    return root


def ws():
    return wf.LocalWorkspace()


def names(listing):
    return [entry["name"] for entry in listing["entries"]]


# --- Reading -------------------------------------------------------------------


def test_a_listing_has_folders_first_with_sizes_and_dates_and_leaves_secrets_out(tree):
    listing = ws().list_dir(str(tree), str(tree))
    assert names(listing) == ["docs", "notes.txt"]
    notes = listing["entries"][1]
    assert notes["size"] == len("nothing here\n") and notes["mtime"] > 0 and notes["isDirectory"] is False
    assert listing["entries"][0]["size"] is None


def test_a_path_outside_the_root_is_refused(tree):
    with pytest.raises(wf.WorkspaceError) as exc:
        ws().list_dir(str(tree), str(tree.parent))
    assert exc.value.status == 403
    with pytest.raises(wf.WorkspaceError) as exc:
        ws().list_dir(str(tree), str(tree / "docs" / ".." / ".."))
    assert exc.value.status == 403


def test_a_symlink_out_of_the_root_is_refused(tree):
    (tree / "escape").symlink_to(tree.parent)
    with pytest.raises(wf.WorkspaceError) as exc:
        ws().list_dir(str(tree), str(tree / "escape"))
    assert exc.value.status == 403
    with pytest.raises(wf.WorkspaceError):
        ws().move(str(tree), str(tree / "notes.txt"), str(tree / "escape"), "notes.txt")


def test_search_finds_names_and_lines_inside_files(tree):
    found = ws().search(str(tree), "grouping", contents=True)
    assert [m["path"] for m in found["matches"]] == [str(tree / "docs" / "guide.md")]
    line = found["matches"][0]["lines"][0]
    assert line["line"] == 2 and line["text"] == "Grouping videos by topic"
    assert (line["matchStart"], line["matchLength"]) == (0, len("grouping"))
    by_name = ws().search(str(tree), "GUIDE", contents=False)
    assert [entry["name"] for entry in by_name["names"]] == ["guide.md"] and by_name["matches"] == []


def test_search_never_reads_secrets_or_skipped_folders(tree):
    found = ws().search(str(tree), "secret", contents=True)
    assert found["matches"] == [] and found["names"] == []
    assert ws().search(str(tree), "dep", contents=False)["names"] == []


def test_a_long_line_is_trimmed_around_its_match(tree):
    (tree / "long.txt").write_text("x" * 500 + "needle" + "y" * 500 + "\n")
    line = ws().search(str(tree), "needle", contents=True)["matches"][0]["lines"][0]
    assert len(line["text"]) <= wf.SNIPPET_CHARS + 2
    assert line["text"][line["matchStart"]:line["matchStart"] + 6] == "needle"


def test_a_search_out_of_time_says_so(tree):
    ticks = iter(range(0, 10_000, 5))
    workspace = wf.LocalWorkspace(clock=lambda: next(ticks))
    assert workspace.search(str(tree), "grouping", contents=True)["truncated"] is True


def test_recent_files_come_newest_first(tree):
    os.utime(tree / "notes.txt", (1_000, 1_000))
    os.utime(tree / "docs" / "guide.md", (2_000, 2_000))
    recent = ws().recent(str(tree))["entries"]
    assert [entry["name"] for entry in recent] == ["guide.md", "notes.txt"]


# --- Changing ------------------------------------------------------------------


def test_new_folders_and_files_never_replace_anything(tree):
    ws().mkdir(str(tree), str(tree), "drafts")
    assert (tree / "drafts").is_dir()
    entry = ws().new_file(str(tree), str(tree / "drafts"), "todo.md", "- one\n")["entry"]
    assert entry["path"] == str(tree / "drafts" / "todo.md") and (tree / "drafts" / "todo.md").read_text() == "- one\n"
    for call in (lambda: ws().mkdir(str(tree), str(tree), "drafts"),
                 lambda: ws().new_file(str(tree), str(tree / "drafts"), "todo.md")):
        with pytest.raises(wf.WorkspaceError) as exc:
            call()
        assert exc.value.status == 409


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "../x", "a\\b", "x" * 300])
def test_a_name_is_one_path_component(tree, name):
    with pytest.raises(wf.WorkspaceError) as exc:
        ws().mkdir(str(tree), str(tree), name)
    assert exc.value.status == 400


def test_secrets_cant_be_created_or_touched(tree):
    with pytest.raises(wf.WorkspaceError):
        ws().new_file(str(tree), str(tree), ".env.local")
    with pytest.raises(wf.WorkspaceError):
        ws().delete(str(tree), str(tree / ".env"))
    with pytest.raises(wf.WorkspaceError):
        ws().move(str(tree), str(tree / "notes.txt"), str(tree), ".env")
    assert (tree / ".env").exists() and (tree / "notes.txt").exists()


def test_a_save_refuses_a_file_changed_since_it_was_read(tree):
    target = tree / "notes.txt"
    read_at = target.stat().st_mtime
    entry = ws().save(str(tree), str(target), "edited\n", read_at)["entry"]
    assert target.read_text() == "edited\n"
    os.utime(target, (read_at + 50, read_at + 50))
    with pytest.raises(wf.WorkspaceError) as exc:
        ws().save(str(tree), str(target), "lost\n", entry["mtime"])
    assert exc.value.status == 409 and target.read_text() == "edited\n"


def test_a_save_keeps_the_files_mode(tree):
    target = tree / "run.sh"
    target.write_text("echo hi\n")
    target.chmod(0o755)
    ws().save(str(tree), str(target), "echo bye\n", None)
    assert target.stat().st_mode & 0o777 == 0o755
    assert sorted(os.listdir(tree)) == sorted([".env", "docs", "node_modules", "notes.txt", "run.sh"])


def test_rename_and_move_refuse_to_replace_unless_asked(tree):
    ws().move(str(tree), str(tree / "notes.txt"), str(tree), "renamed.txt")
    assert (tree / "renamed.txt").exists() and not (tree / "notes.txt").exists()
    ws().move(str(tree), str(tree / "renamed.txt"), str(tree / "docs"), "renamed.txt")
    assert (tree / "docs" / "renamed.txt").exists()
    (tree / "other.txt").write_text("other\n")
    with pytest.raises(wf.WorkspaceError) as exc:
        ws().move(str(tree), str(tree / "other.txt"), str(tree / "docs"), "renamed.txt")
    assert exc.value.status == 409 and (tree / "other.txt").exists()
    ws().move(str(tree), str(tree / "other.txt"), str(tree / "docs"), "renamed.txt", overwrite=True)
    assert (tree / "docs" / "renamed.txt").read_text() == "other\n"


def test_a_folder_cant_move_inside_itself_and_the_root_cant_move(tree):
    with pytest.raises(wf.WorkspaceError) as exc:
        ws().move(str(tree), str(tree / "docs"), str(tree / "docs"), "docs")
    assert exc.value.status == 400
    with pytest.raises(wf.WorkspaceError):
        ws().move(str(tree), str(tree), str(tree / "docs"), "moved")


def test_duplicates_take_the_next_free_copy_name(tree):
    first = ws().duplicate(str(tree), str(tree / "docs" / "guide.md"))["entry"]
    second = ws().duplicate(str(tree), str(tree / "docs" / "guide.md"))["entry"]
    assert (first["name"], second["name"]) == ("guide copy.md", "guide copy 2.md")
    folder = ws().duplicate(str(tree), str(tree / "docs"))["entry"]
    assert folder["name"] == "docs copy" and (tree / "docs copy" / "guide.md").exists()


def test_delete_removes_files_and_folders_but_never_the_root(tree):
    ws().delete(str(tree), str(tree / "docs"))
    assert not (tree / "docs").exists()
    with pytest.raises(wf.WorkspaceError) as exc:
        ws().delete(str(tree), str(tree))
    assert exc.value.status == 400 and tree.exists()


def test_deleting_a_link_leaves_what_it_points_to(tree):
    (tree / "link").symlink_to(tree / "docs")
    ws().delete(str(tree), str(tree / "link"))
    assert not (tree / "link").exists() and (tree / "docs" / "guide.md").exists()


def test_uploads_land_whole_and_ask_before_replacing(tree):
    ws().upload(str(tree), str(tree / "docs"), "photo.bin", b"\x00\x01")
    assert (tree / "docs" / "photo.bin").read_bytes() == b"\x00\x01"
    with pytest.raises(wf.WorkspaceError) as exc:
        ws().upload(str(tree), str(tree / "docs"), "photo.bin", b"new")
    assert exc.value.status == 409
    ws().upload(str(tree), str(tree / "docs"), "photo.bin", b"new", overwrite=True)
    assert (tree / "docs" / "photo.bin").read_bytes() == b"new"
    assert [n for n in os.listdir(tree / "docs") if "conduit" in n] == []


def test_a_resolver_maps_agent_paths_to_this_host(tree):
    mapped = wf.LocalWorkspace(resolve=lambda raw: raw.replace("/workspace", str(tree), 1))
    assert names(mapped.list_dir("/workspace", "/workspace/docs")) == ["guide.md"]


def test_saves_and_uploads_never_write_through_a_planted_link(tree):
    local = wf.LocalWorkspace()
    root = str(tree.resolve())
    victim = tree.parent / "victim.txt"
    victim.write_text("keep\n")
    for pid in (os.getpid(),):
        (tree / f".notes.txt.conduit-{pid}").symlink_to(victim)
    local.save(root, str(tree / "notes.txt"), "new\n", None)
    local.upload(root, root, "u.txt", b"up")
    assert victim.read_text() == "keep\n" and (tree / "notes.txt").read_text() == "new\n"
    assert not [name for name in os.listdir(tree) if name.startswith(".conduit-")]
    assert (tree / "u.txt").stat().st_mode & 0o777 == 0o666 & ~_umask()


def _umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


def test_a_name_search_cut_short_says_so(tree, monkeypatch):
    monkeypatch.setattr(wf, "SEARCH_MAX_RESULTS", 2)
    for number in range(4):
        (tree / f"match{number}.log").write_text("")
    found = wf.LocalWorkspace().search(str(tree), "match", contents=False)
    assert len(found["names"]) == 2 and found["truncated"] is True


def test_a_locked_install_keeps_roots_inside_its_folder(tree):
    local = wf.LocalWorkspace(boundary=str(tree / "docs"))
    with pytest.raises(wf.WorkspaceError) as exc:
        local.list_dir(str(tree), str(tree))
    assert exc.value.status == 403
    assert names(local.list_dir(str(tree / "docs"), str(tree / "docs"))) == ["guide.md"]


def test_a_very_long_path_is_refused(tree):
    with pytest.raises(wf.WorkspaceError) as exc:
        wf.LocalWorkspace().list_dir("/" + "a" * 5000, "/")
    assert exc.value.status == 400


# --- SSH -----------------------------------------------------------------------


class _LocalShell:
    """Hermes' SSH adapter, its commands run by this machine's sh."""

    def __init__(self, cwd):
        self.cwd = str(cwd)

    def _normalize(self, path):
        import posixpath
        raw = str(path).strip()
        if not raw.startswith("/"):
            raw = posixpath.join(self.cwd, raw)
        return posixpath.normpath(raw)

    def _execute(self, command, *, stdin_data=None, timeout=None):
        done = subprocess.run(["sh", "-c", command], input=stdin_data, capture_output=True, text=True,
                              cwd=self.cwd, timeout=timeout or 30)
        return {"output": done.stdout + done.stderr, "returncode": done.returncode}


def ssh(tree):
    return wf.SshWorkspace(_LocalShell(tree))


def test_ssh_lists_searches_and_changes_files_the_same_way(tree):
    remote = ssh(tree)
    root = str(tree.resolve())
    listing = remote.list_dir(root, root)
    assert names(listing) == ["docs", "notes.txt"] and listing["entries"][1]["size"] == len("nothing here\n")
    found = remote.search(root, "grouping", contents=True)
    assert [m["path"] for m in found["matches"]] == [str(tree / "docs" / "guide.md")]
    assert found["matches"][0]["lines"][0]["line"] == 2
    assert [entry["name"] for entry in remote.search(root, "guide", contents=False)["names"]] == ["guide.md"]
    remote.mkdir(root, root, "drafts")
    remote.new_file(root, str(tree / "drafts"), "a b.md", "hello\n")
    assert (tree / "drafts" / "a b.md").read_text() == "hello\n"
    with pytest.raises(wf.WorkspaceError) as exc:
        remote.new_file(root, str(tree / "drafts"), "a b.md", "again\n")
    assert exc.value.status == 409
    remote.move(root, str(tree / "drafts" / "a b.md"), root, "moved.md")
    assert (tree / "moved.md").exists()
    assert remote.duplicate(root, str(tree / "moved.md"))["entry"]["name"] == "moved copy.md"
    remote.upload(root, root, "data.bin", b"\x00\xff")
    assert (tree / "data.bin").read_bytes() == b"\x00\xff"
    remote.delete(root, str(tree / "drafts"))
    assert not (tree / "drafts").exists()


def test_ssh_refuses_paths_outside_and_stale_saves(tree):
    remote = ssh(tree)
    root = str(tree.resolve())
    with pytest.raises(wf.WorkspaceError) as exc:
        remote.list_dir(root, str(tree.parent))
    assert exc.value.status == 403
    (tree / "escape").symlink_to(tree.parent)
    with pytest.raises(wf.WorkspaceError):
        remote.mkdir(root, str(tree / "escape"), "x")
    assert not (tree.parent / "x").exists()
    target = tree / "notes.txt"
    os.utime(target, (5_000, 5_000))
    with pytest.raises(wf.WorkspaceError) as exc:
        remote.save(root, str(target), "lost\n", 4_000)
    assert exc.value.status == 409 and target.read_text() == "nothing here\n"
    remote.save(root, str(target), "kept\n", 5_000)
    assert target.read_text() == "kept\n"
    with pytest.raises(wf.WorkspaceError):
        remote.delete(root, root)


def test_ssh_refuses_links_that_lead_out_of_the_root(tree):
    remote = ssh(tree)
    root = str(tree.resolve())
    (tree / "escape").symlink_to(tree.parent)
    (tree / "leak.txt").symlink_to(tree.parent / "outside.txt")
    for call in (lambda: remote.list_dir(root, str(tree / "escape")),
                 lambda: remote.duplicate(root, str(tree / "escape")),
                 lambda: remote.duplicate(root, str(tree / "leak.txt")),
                 lambda: remote.save(root, str(tree / "leak.txt"), "x", None)):
        with pytest.raises(wf.WorkspaceError) as exc:
            call()
        assert exc.value.status == 403
    assert (tree.parent / "outside.txt").read_text() == "grouping videos\n"
    assert not any(name.startswith("escape copy") or name.startswith("leak copy") for name in os.listdir(tree))


def test_ssh_duplicate_leaves_secrets_out_and_keeps_modes(tree):
    remote = ssh(tree)
    root = str(tree.resolve())
    (tree / "docs" / ".env").write_text("SECRET=1\n")
    copy = remote.duplicate(root, str(tree / "docs"))["entry"]["path"]
    assert sorted(os.listdir(copy)) == ["guide.md"]
    script = tree / "run.sh"
    script.write_text("echo hi\n")
    script.chmod(0o755)
    remote.save(root, str(script), "echo bye\n", None)
    assert script.stat().st_mode & 0o777 == 0o755
    assert not [name for name in os.listdir(tree) if name.startswith(".conduit-")]


def test_ssh_says_a_missing_folder_or_file_is_missing(tree):
    remote = ssh(tree)
    root = str(tree.resolve())
    for call in (lambda: remote.mkdir(root, str(tree / "gone"), "x"),
                 lambda: remote.upload(root, str(tree / "gone"), "x", b"1"),
                 lambda: remote.save(root, str(tree / "gone.txt"), "x", 5_000)):
        with pytest.raises(wf.WorkspaceError) as exc:
            call()
        assert exc.value.status == 404


def test_ssh_search_reads_colons_in_paths(tree):
    (tree / "a:1:b.txt").write_text("needle\n")
    found = ssh(tree).search(str(tree.resolve()), "needle", contents=True)
    assert [m["name"] for m in found["matches"]] == ["a:1:b.txt"]


def test_listings_show_a_link_out_of_the_root_as_the_link_itself(tree):
    (tree / "big").mkdir()
    (tree.parent / "secret_dir").mkdir()
    (tree / "away").symlink_to(tree.parent / "secret_dir")
    (tree / "leak.txt").symlink_to(tree.parent / "outside.txt")
    (tree / "near").symlink_to(tree / "docs")
    root = str(tree.resolve())
    for listing in (ws().list_dir(root, root), ssh(tree).list_dir(root, root)):
        rows = {entry["name"]: entry for entry in listing["entries"]}
        assert rows["away"]["isDirectory"] is False
        assert rows["leak.txt"]["size"] != len("grouping videos\n")
        assert rows["near"]["isDirectory"] is True


def test_moving_a_folder_onto_a_file_or_back_is_a_conflict(tree):
    (tree / "folder").mkdir()
    for workspace in (ws(), ssh(tree)):
        root = str(tree.resolve())
        with pytest.raises(wf.WorkspaceError) as exc:
            workspace.move(root, str(tree / "folder"), root, "notes.txt", overwrite=True)
        assert exc.value.status == 409
        with pytest.raises(wf.WorkspaceError) as exc:
            workspace.move(root, str(tree / "notes.txt"), root, "folder", overwrite=True)
        assert exc.value.status == 409
    assert (tree / "notes.txt").read_text() == "nothing here\n" and (tree / "folder").is_dir()


def test_ssh_save_onto_a_folder_says_only_files_are_edited(tree):
    with pytest.raises(wf.WorkspaceError) as exc:
        ssh(tree).save(str(tree.resolve()), str(tree / "docs"), "x", None)
    assert exc.value.status == 400


def test_ssh_never_reads_or_copies_secrets_in_any_case(tree):
    remote = ssh(tree)
    root = str(tree.resolve())
    (tree / "docs" / ".ENV").write_text("grouping SECRET\n")
    (tree / "docs" / ".Env.local").write_text("grouping SECRET\n")
    (tree / "docs" / ".SSH").mkdir()
    (tree / "docs" / ".SSH" / "key").write_text("grouping SECRET\n")
    found = remote.search(root, "secret", contents=True)
    assert found["matches"] == []
    copy = remote.duplicate(root, str(tree / "docs"))["entry"]["path"]
    assert sorted(os.listdir(copy)) == ["guide.md"]


def test_a_link_to_a_secret_under_an_innocent_name_stays_out(tree):
    (tree / "innocent.txt").symlink_to(tree / ".env")
    (tree / ".ssh").mkdir()
    (tree / "keys").symlink_to(tree / ".ssh")
    root = str(tree.resolve())
    for listing in (ws().list_dir(root, root), ssh(tree).list_dir(root, root)):
        assert "innocent.txt" not in names(listing) and "keys" not in names(listing)


def test_a_secret_is_refused_whether_or_not_it_is_there(tree):
    for path in (tree / ".env", tree / ".envrc"):
        with pytest.raises(wf.WorkspaceError) as exc:
            ws().list_dir(str(tree), str(path))
        assert exc.value.status == 403


def test_a_file_link_is_duplicated_as_a_link(tree):
    (tree / "link.txt").symlink_to(tree / "notes.txt")
    copy = ws().duplicate(str(tree), str(tree / "link.txt"))["entry"]["path"]
    assert os.path.islink(copy)


class _BsdShell(_LocalShell):
    """A remote without realpath or readlink -f, as an older macOS."""

    def _execute(self, command, *, stdin_data=None, timeout=None):
        shim = ('realpath() { return 1; }\n'
                'readlink() { [ "$1" = -f ] && return 1; command readlink "$@"; }\n')
        return super()._execute(shim + command, stdin_data=stdin_data, timeout=timeout)


def test_ssh_follows_links_without_realpath(tree):
    remote = wf.SshWorkspace(_BsdShell(tree))
    root = str(tree.resolve())
    (tree / "docs" / "near.md").symlink_to("../notes.txt")
    (tree / "leak.txt").symlink_to(tree.parent / "outside.txt")
    remote.save(root, str(tree / "docs" / "near.md"), "through\n", None)
    assert (tree / "notes.txt").read_text() == "through\n" and (tree / "docs" / "near.md").is_symlink()
    with pytest.raises(wf.WorkspaceError) as exc:
        remote.save(root, str(tree / "leak.txt"), "x", None)
    assert exc.value.status == 403


def test_ssh_refuses_a_link_to_a_secret_inside_the_root(tree):
    remote = ssh(tree)
    root = str(tree.resolve())
    (tree / "innocent.txt").symlink_to(tree / ".env")
    (tree / ".ssh").mkdir()
    (tree / "keys").symlink_to(tree / ".ssh")
    for call in (lambda: remote.save(root, str(tree / "innocent.txt"), "x", None),
                 lambda: remote.mkdir(root, str(tree / "keys"), "x"),
                 lambda: remote.new_file(root, str(tree / "keys"), "authorized_keys", "x")):
        with pytest.raises(wf.WorkspaceError) as exc:
            call()
        assert exc.value.status == 403
    assert (tree / ".env").read_text() == "SECRET=1\n" and os.listdir(tree / ".ssh") == []


def test_ssh_new_files_never_replace_one_that_appears_meanwhile(tree):
    remote = ssh(tree)
    root = str(tree.resolve())
    remote._write(root, str(tree / "fresh.txt"), b"one", create=True)
    assert (tree / "fresh.txt").read_text() == "one"
    original = remote._run

    def racing(script, **kwargs):
        if "mv -n" in script:
            (tree / "raced.txt").write_text("theirs")
        return original(script, **kwargs)

    remote._run = racing
    with pytest.raises(wf.WorkspaceError) as exc:
        remote._write(root, str(tree / "raced.txt"), b"mine", create=True)
    assert exc.value.status == 409 and (tree / "raced.txt").read_text() == "theirs"
    assert not [name for name in os.listdir(tree) if name.startswith(".conduit-")]


def test_ssh_stops_listing_a_huge_folder_at_the_cap(tree, monkeypatch):
    monkeypatch.setattr(wf, "MAX_ENTRIES", 3)
    for index in range(10):
        (tree / f"f{index}.txt").write_text("x")
    root = str(tree.resolve())
    listing = ssh(tree).list_dir(root, root)
    assert len(listing["entries"]) == 3 and listing["truncated"] is True


def test_search_never_reads_through_a_link(tree):
    (tree / "docs" / "swapped.md").symlink_to(tree.parent / "outside.txt")
    assert wf._search_file(str(tree / "docs" / "swapped.md"), "grouping") == []


def test_ssh_name_search_leaves_out_a_link_to_a_secret(tree):
    (tree / "innocent-notes.txt").symlink_to(tree / ".env")
    found = ssh(tree).search(str(tree.resolve()), "notes", contents=False)
    assert [entry["name"] for entry in found["names"]] == ["notes.txt"]


def test_ssh_duplicate_finds_a_free_name_past_the_listing_cap(tree, monkeypatch):
    monkeypatch.setattr(wf, "MAX_ENTRIES", 1)
    (tree / "notes copy.txt").write_text("taken")
    copy = ssh(tree).duplicate(str(tree.resolve()), str(tree / "notes.txt"))["entry"]["name"]
    assert copy == "notes copy 2.txt" and (tree / "notes copy.txt").read_text() == "taken"


def test_moving_a_link_moves_the_link(tree):
    (tree / "link.txt").symlink_to(tree / "notes.txt")
    ws().move(str(tree), str(tree / "link.txt"), str(tree / "docs"), "moved.txt")
    assert os.path.islink(tree / "docs" / "moved.txt") and (tree / "notes.txt").is_file()


class _OldMacShell(_LocalShell):
    """A remote whose base64 decodes only with -D, as an older macOS."""

    def _execute(self, command, *, stdin_data=None, timeout=None):
        shim = 'base64() { if [ "$1" = -d ]; then return 64; elif [ "$1" = -D ]; then command base64 -d; else command base64 "$@"; fi; }\n'
        return super()._execute(shim + command, stdin_data=stdin_data, timeout=timeout)


def test_ssh_writes_on_a_mac_whose_base64_wants_capital_d(tree):
    remote = wf.SshWorkspace(_OldMacShell(tree))
    root = str(tree.resolve())
    remote.new_file(root, root, "mac.txt", "hello\n")
    assert (tree / "mac.txt").read_text() == "hello\n"


def test_an_upload_over_a_script_keeps_its_mode(tree):
    script = tree / "run.sh"
    script.write_text("echo hi\n")
    script.chmod(0o755)
    ws().upload(str(tree), str(tree), "run.sh", b"echo bye\n", overwrite=True)
    assert script.stat().st_mode & 0o777 == 0o755 and script.read_text() == "echo bye\n"


def test_ssh_name_search_is_cut_only_past_the_cap(tree, monkeypatch):
    monkeypatch.setattr(wf, "SEARCH_MAX_RESULTS", 2)
    (tree / "a-hit.txt").write_text("x")
    (tree / "b-hit.txt").write_text("x")
    root = str(tree.resolve())
    found = ssh(tree).search(root, "hit", contents=False)
    assert len(found["names"]) == 2 and found["truncated"] is False
    (tree / "c-hit.txt").write_text("x")
    found = ssh(tree).search(root, "hit", contents=False)
    assert len(found["names"]) == 2 and found["truncated"] is True


def test_ssh_content_search_never_reads_through_a_link(tree):
    (tree / "docs" / "away.txt").symlink_to(tree.parent / "outside.txt")
    (tree / "docs" / "innocent.txt").symlink_to(tree / ".env")
    root = str(tree.resolve())
    for needle in ("grouping videos", "SECRET"):
        found = ssh(tree).search(root, needle, contents=True)
        assert all(not m["path"].endswith(("away.txt", "innocent.txt")) for m in found["matches"]), found


def test_a_link_out_of_the_root_isnt_moved_on_either_backend(tree):
    (tree / "away").symlink_to(tree.parent / "outside.txt")
    root = str(tree.resolve())
    for workspace in (ws(), ssh(tree)):
        with pytest.raises(wf.WorkspaceError) as exc:
            workspace.move(root, str(tree / "away"), root, "renamed")
        assert exc.value.status == 403
    assert (tree / "away").is_symlink()


def test_a_move_across_disks_keeps_links_and_never_moves_into_a_folder(tree, monkeypatch):
    (tree / "link.txt").symlink_to(tree / "notes.txt")
    real_rename = wf._rename_no_replace
    calls = []

    def across(source, target):
        if not calls:
            calls.append(target)
            raise OSError(18, "Invalid cross-device link")
        return real_rename(source, target)

    monkeypatch.setattr(wf, "_rename_no_replace", across)
    ws().move(str(tree), str(tree / "link.txt"), str(tree / "docs"), "moved.txt")
    assert os.path.islink(tree / "docs" / "moved.txt")
    calls.clear()
    (tree / "docs" / "taken").mkdir()
    real_lexists = os.path.lexists
    monkeypatch.setattr(wf.os.path, "lexists", lambda p: False if p.endswith("taken") else real_lexists(p))
    with pytest.raises(wf.WorkspaceError) as exc:
        ws().move(str(tree), str(tree / "notes.txt"), str(tree / "docs"), "taken")
    assert exc.value.status == 409 and (tree / "notes.txt").is_file() and os.listdir(tree / "docs" / "taken") == []


def test_a_move_across_disks_copies_a_link_as_a_link_and_leaves_nothing_behind(tree, monkeypatch):
    (tree / "link.txt").symlink_to(tree / "notes.txt")
    real_rename, real_no_replace = os.rename, wf._rename_no_replace
    first = []

    def no_replace(source, target):
        if not first:
            first.append(target)
            raise OSError(18, "Invalid cross-device link")
        return real_no_replace(source, target)

    def rename(source, target):  # shutil.move's own rename: copy it, as across disks
        if ".conduit-" in str(target) or ".conduit-" in str(source):
            raise OSError(18, "Invalid cross-device link")
        return real_rename(source, target)

    monkeypatch.setattr(wf, "_rename_no_replace", no_replace)
    monkeypatch.setattr(os, "rename", rename)
    ws().move(str(tree), str(tree / "link.txt"), str(tree / "docs"), "moved.txt")
    assert os.path.islink(tree / "docs" / "moved.txt") and not os.path.lexists(tree / "link.txt")
    assert (tree / "notes.txt").is_file() and not [n for n in os.listdir(tree / "docs") if n.startswith(".conduit-")]
    first.clear()
    monkeypatch.setattr(wf, "_rename_no_replace", lambda source, target: (
        first.append(target), (_ for _ in ()).throw(OSError(18, "x") if len(first) == 1 else PermissionError(13, "no"))))
    with pytest.raises(wf.WorkspaceError) as exc:
        ws().move(str(tree), str(tree / "notes.txt"), str(tree / "docs"), "kept.txt")
    assert exc.value.status == 403 and (tree / "notes.txt").is_file()
    assert not [n for n in os.listdir(tree / "docs") if n.startswith(".conduit-")]


def test_a_move_across_disks_that_copies_only_part_says_where_the_part_is(tree, monkeypatch):
    (tree / "docs" / "part").mkdir()
    (tree / "docs" / "part" / "a.txt").write_text("a")
    monkeypatch.setattr(wf, "_rename_no_replace", lambda source, target: (_ for _ in ()).throw(OSError(18, "x")))

    def half(source, target):
        os.mkdir(target)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(wf.shutil, "move", half)
    with pytest.raises(wf.WorkspaceError) as exc:
        ws().move(str(tree), str(tree / "docs" / "part"), str(tree), "part")
    left = [n for n in os.listdir(tree) if n.startswith(".conduit-")]
    assert exc.value.status == 500 and left and left[0] in str(exc.value)
    assert (tree / "docs" / "part" / "a.txt").is_file()


def test_ssh_deletes_a_link_to_a_secret_but_not_the_secret(tree):
    (tree / "innocent.txt").symlink_to(tree / ".env")
    ssh(tree).delete(str(tree.resolve()), str(tree / "innocent.txt"))
    assert not os.path.lexists(tree / "innocent.txt") and (tree / ".env").read_text() == "SECRET=1\n"


# --- Routes --------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api, "_workspace_files", lambda: wf)
    monkeypatch.setattr(api, "_workspace_module", wf)
    monkeypatch.setattr(api, "_workspace", lambda profile: wf.LocalWorkspace())
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_routes_list_search_and_change(tree, client):
    root = str(tree)
    listing = client.get(f"{BASE}/workspace/list", params={"root": root}).json()
    assert listing["ok"] is True and names(listing) == ["docs", "notes.txt"]
    found = client.get(f"{BASE}/workspace/search", params={"root": root, "q": "grouping"}).json()
    assert len(found["matches"]) == 1
    assert client.get(f"{BASE}/workspace/recent", params={"root": root}).json()["entries"]
    made = client.post(f"{BASE}/workspace/new-file", json={"root": root, "path": root, "name": "n.md"}).json()
    assert made["entry"]["name"] == "n.md"
    data = base64.b64encode(b"bytes").decode()
    client.post(f"{BASE}/workspace/upload", json={"root": root, "path": root, "name": "u.bin", "data": data})
    assert (tree / "u.bin").read_bytes() == b"bytes"
    client.post(f"{BASE}/workspace/delete", json={"root": root, "path": str(tree / "u.bin")})
    assert not (tree / "u.bin").exists()


def test_routes_say_why_they_refuse(tree, client):
    root = str(tree)
    outside = client.get(f"{BASE}/workspace/list", params={"root": root, "path": str(tree.parent)})
    assert outside.status_code == 403 and outside.json()["detail"] == "That's outside this workspace"
    assert client.post(f"{BASE}/workspace/delete", json={"root": root}).status_code == 400
    assert client.post(f"{BASE}/workspace/explode", json={"root": root}).status_code == 404
    bad = client.post(f"{BASE}/workspace/upload", json={"root": root, "path": root, "name": "x", "data": "%%%"})
    assert bad.status_code == 400
    stale = client.post(f"{BASE}/workspace/save", json={"root": root, "path": str(tree / "notes.txt"),
                                                         "content": "x", "expected_mtime": 1})
    assert stale.status_code == 409
    nan = client.post(f"{BASE}/workspace/save", content='{"root": "%s", "path": "%s", "content": "x", '
                      '"expected_mtime": NaN}' % (root, tree / "notes.txt"),
                      headers={"Content-Type": "application/json"})
    assert nan.status_code == 400 and (tree / "notes.txt").read_text() == "nothing here\n"


def test_without_hermes_routing_the_routes_refuse(monkeypatch):
    monkeypatch.setattr(api, "_workspace_files", lambda: wf)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_routers.files", None)
    with pytest.raises(api.TokenError) as exc:
        api._workspace(None)
    assert exc.value.status == 501


def test_the_capability_is_listed():
    assert "workspace-files" in api.ROUTE_CAPABILITIES


def test_a_search_given_up_on_frees_its_slot_only_once_it_ends(tree, client, monkeypatch):
    import threading
    release = threading.Event()
    started = threading.Event()

    class Slow(wf.LocalWorkspace):
        def search(self, *args, **kwargs):
            started.set()
            release.wait(5)
            return {"names": [], "matches": [], "truncated": False}

    monkeypatch.setattr(api, "_workspace", lambda profile: Slow())
    monkeypatch.setattr(api, "WORKSPACE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(api, "_workspace_walks", threading.BoundedSemaphore(1))
    assert client.get(f"{BASE}/workspace/search", params={"root": str(tree), "q": "x"}).status_code == 504
    assert started.is_set()
    # The walk is still running: a second one waits its turn.
    assert client.get(f"{BASE}/workspace/search", params={"root": str(tree), "q": "x"}).status_code == 503
    release.set()
    for _ in range(50):
        if api._workspace_walks.acquire(blocking=False):
            api._workspace_walks.release()
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the walk never gave its slot back")


def test_a_flood_is_turned_away_before_its_body_is_read(tree, client, monkeypatch):
    read = []
    original = api._read_json_body

    async def counting(request, limit):
        read.append(limit)
        return await original(request, limit)

    monkeypatch.setattr(api, "_read_json_body", counting)
    monkeypatch.setattr(api, "_workspace_limiter",
                        api._MintLimiter(1, 60.0, message="Too many file requests; try again shortly"))
    body = {"root": str(tree), "path": str(tree), "name": "a"}
    assert client.post(f"{BASE}/workspace/mkdir", json=body).status_code == 200
    assert client.post(f"{BASE}/workspace/mkdir", json={**body, "name": "b"}).status_code == 429
    assert len(read) == 1
