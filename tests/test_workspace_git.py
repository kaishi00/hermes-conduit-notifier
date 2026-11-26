"""Git for the coding workspace's Changes view (workspace_git.py, desktop issue 452)."""

import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(os.environ["CONDUIT_TESTS_NO_HOME"])
    sys.modules["hermes_constants"] = _hermes_constants


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wg = _load("conduit_workspace_git_test", ROOT / "workspace_git.py")
wf = _load("conduit_workspace_files_git_test", ROOT / "workspace_files.py")
api = _load("conduit_plugin_api_git", ROOT / "dashboard" / "plugin_api.py")


@pytest.fixture(autouse=True)
def identity(monkeypatch, tmp_path):
    # A clean, known Git for every test: no user config, a fixed identity.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (tmp_path / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n")
    for key, value in (("GIT_AUTHOR_NAME", "Furina"), ("GIT_AUTHOR_EMAIL", "furina@example.com"),
                       ("GIT_COMMITTER_NAME", "Furina"), ("GIT_COMMITTER_EMAIL", "furina@example.com")):
        monkeypatch.setenv(key, value)


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "site"
    (root / "src").mkdir(parents=True)
    (root / "src" / "index.astro").write_text("<h1>Conduit for Hermes Agent</h1>\n<p>old</p>\n")
    (root / "README.md").write_text("# Site\n")
    sh(root, "init", "-q")
    sh(root, "add", "-A")
    sh(root, "commit", "-q", "-m", "First")
    return root


def git(root):
    return wg.Git(os.path.realpath(root), wg.LocalRunner(), wf.default_sensitive)


def by_path(status):
    return {item["path"]: item for item in status["files"]}


# --- Reading ---------------------------------------------------------------------


def test_a_folder_outside_git_says_so_and_can_start_one(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    assert git(folder).status()["repo"] is False
    started = git(folder).change("init", {})
    assert started["status"]["repo"] is True and started["status"]["branch"]["head"] == "main"
    with pytest.raises(wg.GitError) as exc:
        git(folder).change("init", {})
    assert exc.value.status == 409


def test_status_lists_staged_unstaged_untracked_and_secret_files_with_counts(repo):
    (repo / "src" / "index.astro").write_text("<h1>Your Hermes, everywhere.</h1>\n<p>old</p>\n<p>new</p>\n")
    (repo / "README.md").write_text("# Site\n\nMore.\n")
    sh(repo, "add", "README.md")
    (repo / "badge.svg").write_text("<svg/>\n")
    (repo / ".env").write_text("SECRET=1\n")
    status = git(repo).status()
    assert status["repo"] and status["top"] == os.path.realpath(repo)
    assert status["branch"]["head"] == "main" and status["branch"]["detached"] is False
    files = by_path(status)
    assert files["src/index.astro"]["unstaged"] == "modified" and files["src/index.astro"]["staged"] is None
    assert (files["src/index.astro"]["added"], files["src/index.astro"]["removed"]) == (2, 1)
    assert files["README.md"]["staged"] == "modified" and files["README.md"]["stagedAdded"] == 2
    assert files["badge.svg"]["unstaged"] == "untracked"
    assert files[".env"]["secret"] is True and files["badge.svg"]["secret"] is False


def test_a_files_diff_tracked_untracked_and_staged(repo):
    (repo / "src" / "index.astro").write_text("<h1>Your Hermes, everywhere.</h1>\n<p>old</p>\n")
    (repo / "new.txt").write_text("hello\n")
    changed = git(repo).diff("src/index.astro")
    assert "-<h1>Conduit for Hermes Agent</h1>" in changed["diff"] and "+<h1>Your Hermes" in changed["diff"]
    assert changed["binary"] is False and changed["truncated"] is False
    new = git(repo).diff(str(repo / "new.txt"))
    assert "+hello" in new["diff"] and new["path"] == "new.txt"
    sh(repo, "add", "new.txt")
    assert git(repo).diff("new.txt")["diff"] == ""
    assert "+hello" in git(repo).diff("new.txt", staged=True)["diff"]


def test_a_secret_or_outside_path_is_refused(repo, tmp_path):
    (repo / ".env").write_text("SECRET=1\n")
    for path, status in ((".env", 403), ("../outside.txt", 403), (str(tmp_path / "x"), 403), ("", 400)):
        with pytest.raises(wg.GitError) as exc:
            git(repo).diff(path)
        assert exc.value.status == status, path


def test_history_with_refs_and_counts(repo):
    (repo / "README.md").write_text("# Site\n\nTwo\nThree\n")
    sh(repo, "commit", "-qam", "Second line")
    log = git(repo).log(10)
    assert [commit["subject"] for commit in log["commits"]] == ["Second line", "First"]
    latest = log["commits"][0]
    assert latest["author"] == "Furina" and latest["time"] > 0 and len(latest["short"]) >= 7
    assert (latest["files"], latest["added"], latest["removed"]) == (1, 3, 0)
    assert any("main" in ref for ref in latest["refs"])
    assert [c["subject"] for c in git(repo).log(10, "src/index.astro")["commits"]] == ["First"]


def test_branches_local_and_remote(repo, tmp_path):
    sh(repo, "branch", "hero-copy")
    bare = tmp_path / "remote.git"
    sh(tmp_path, "init", "-q", "--bare", str(bare))
    sh(repo, "remote", "add", "origin", str(bare))
    sh(repo, "push", "-q", "origin", "main")
    listed = git(repo).branches()
    assert {b["name"] for b in listed["local"]} == {"main", "hero-copy"}
    assert [b["name"] for b in listed["local"] if b["current"]] == ["main"]
    assert [b["name"] for b in listed["remote"]] == ["origin/main"]


# --- Changes ---------------------------------------------------------------------


def test_stage_unstage_and_commit(repo):
    (repo / "README.md").write_text("# Site\n\nMore.\n")
    (repo / "new.txt").write_text("hello\n")
    staged = git(repo).change("stage", {"paths": ["README.md", "new.txt"]})["status"]
    assert by_path(staged)["new.txt"]["staged"] == "added"
    unstaged = git(repo).change("unstage", {"paths": ["new.txt"]})["status"]
    assert by_path(unstaged)["new.txt"]["unstaged"] == "untracked"
    done = git(repo).change("commit", {"message": "Readme says more"})
    assert done["commit"] and by_path(done["status"]).keys() == {"new.txt"}
    assert sh(repo, "log", "-1", "--format=%s").strip() == "Readme says more"


def test_commit_with_nothing_staged_asks_or_takes_everything(repo):
    (repo / "README.md").write_text("# Site\n\nMore.\n")
    with pytest.raises(wg.GitError) as exc:
        git(repo).change("commit", {"message": "x"})
    assert exc.value.status == 409
    with pytest.raises(wg.GitError) as exc:
        git(repo).change("commit", {"message": "  "})
    assert exc.value.status == 400
    done = git(repo).change("commit", {"message": "All of it", "all": True})
    assert done["status"]["files"] == []


def test_a_fresh_repository_with_nothing_staged_is_asked_not_failed(tmp_path):
    folder = tmp_path / "fresh"
    folder.mkdir()
    git(folder).change("init", {})
    for body in ({"message": "x"}, {"message": "x", "all": True}):
        with pytest.raises(wg.GitError) as exc:
            git(folder).change("commit", body)
        assert exc.value.status == 409


def test_counts_line_up_when_the_folder_is_inside_the_repository(repo):
    # The chat's folder is src/, the repository's top is its parent: both lists use top paths.
    (repo / "README.md").write_text("# Site\n\nOne.\nTwo.\n")
    (repo / "src" / "index.astro").write_text("<h1>Conduit for Hermes Agent</h1>\n")
    sh(repo, "add", "README.md")
    files = by_path(git(repo / "src").status())
    assert files["README.md"]["stagedAdded"] == 3 and files["README.md"]["added"] is None
    assert (files["src/index.astro"]["added"], files["src/index.astro"]["removed"]) == (0, 1)


def test_a_secret_renamed_away_from_its_name_stays_closed(repo):
    (repo / ".env").write_text("TOKEN=hunter2\n")
    sh(repo, "add", ".env")
    sh(repo, "commit", "-q", "-m", "env")
    sh(repo, "mv", ".env", "settings.txt")
    row = by_path(git(repo).status())["settings.txt"]
    assert row["from"] == ".env" and row["secret"] is True
    with pytest.raises(wg.GitError) as exc:
        git(repo).diff("settings.txt", staged=True)
    assert exc.value.status == 403
    # An ordinary rename still shows its diff.
    sh(repo, "mv", "README.md", "READ.md")
    assert git(repo).diff("READ.md", staged=True)["path"] == "READ.md"


def test_an_inherited_ssh_command_is_kept_in_batch_mode(monkeypatch, repo):
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /keys/deploy")
    wg.LocalRunner(run=run)(str(repo), ["status"], 5)
    assert seen["GIT_SSH_COMMAND"] == "ssh -i /keys/deploy -o BatchMode=yes"


def test_unstage_before_the_first_commit(tmp_path):
    folder = tmp_path / "fresh"
    folder.mkdir()
    (folder / "a.txt").write_text("a\n")
    git(folder).change("init", {})
    git(folder).change("stage", {"all": True})
    assert by_path(git(folder).status())["a.txt"]["staged"] == "added"
    git(folder).change("unstage", {"all": True})
    assert by_path(git(folder).status())["a.txt"]["unstaged"] == "untracked"
    first = git(folder).change("commit", {"message": "Start", "all": True})
    assert first["commit"] and first["status"]["files"] == []


def test_commit_without_an_identity_says_what_to_set(repo, monkeypatch):
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(key)
    monkeypatch.setenv("EMAIL", "")
    sh(repo, "config", "user.useConfigOnly", "true")
    (repo / "README.md").write_text("changed\n")
    with pytest.raises(wg.GitError) as exc:
        git(repo).change("commit", {"message": "x", "all": True})
    assert exc.value.status == 409 and "user.name" in str(exc.value)


def test_discard_restores_tracked_files_and_removes_new_ones(repo):
    (repo / "README.md").write_text("broken\n")
    (repo / "scratch.txt").write_text("tmp\n")
    git(repo).change("discard", {"paths": ["README.md", "scratch.txt"]})
    assert (repo / "README.md").read_text() == "# Site\n" and not (repo / "scratch.txt").exists()
    with pytest.raises(wg.GitError) as exc:
        git(repo).change("discard", {"paths": ["../../etc/passwd"]})
    assert exc.value.status == 403


def test_discard_takes_back_staged_additions_deletions_and_renames(repo):
    (repo / "new.txt").write_text("new\n")
    sh(repo, "add", "new.txt")
    sh(repo, "rm", "-q", "README.md")
    sh(repo, "mv", "src/index.astro", "src/home.astro")
    git(repo).change("discard", {"paths": ["new.txt", "README.md", "src/home.astro"]})
    assert not (repo / "new.txt").exists()
    assert (repo / "README.md").read_text() == "# Site\n"
    assert (repo / "src" / "index.astro").exists() and not (repo / "src" / "home.astro").exists()
    assert git(repo).status()["files"] == []


def test_paths_are_paths_never_patterns(repo):
    (repo / ".env").write_text("TOKEN=old\n")
    sh(repo, "add", ".env")
    sh(repo, "commit", "-q", "-m", "env")
    (repo / ".env").write_text("TOKEN=new\n")
    (repo / "keep.txt").write_text("mine\n")
    for pattern in ("*", ":(glob)**/.env", "?env"):
        try:
            shown = git(repo).diff(pattern)["diff"]
        except wg.GitError:
            shown = ""
        assert "TOKEN" not in shown
    git(repo).change("discard", {"paths": ["*"]})
    assert (repo / "keep.txt").exists() and (repo / ".env").read_text() == "TOKEN=new\n"


def test_a_folders_path_shows_no_diff(repo):
    (repo / "src" / "index.astro").write_text("changed\n")
    (repo / "README.md").write_text("changed\n")
    with pytest.raises(wg.GitError) as exc:
        git(repo).diff("src")
    assert exc.value.status == 400


def test_a_secret_moved_without_git_stays_closed(repo):
    (repo / ".env").write_text("TOKEN=hunter2\n")
    sh(repo, "add", ".env")
    sh(repo, "commit", "-q", "-m", "env")
    (repo / ".env").rename(repo / "notes.txt")
    with pytest.raises(wg.GitError) as exc:
        git(repo).diff("notes.txt")
    assert exc.value.status == 403
    (repo / "other.txt").write_text("plain\n")
    assert "+plain" in git(repo).diff("other.txt")["diff"]


def test_a_push_the_remote_moved_past_says_pull_first(repo, tmp_path):
    bare = tmp_path / "remote.git"
    sh(tmp_path, "init", "-q", "--bare", str(bare))
    sh(repo, "remote", "add", "origin", str(bare))
    git(repo).change("push", {})
    other = tmp_path / "other"
    sh(tmp_path, "clone", "-q", str(bare), str(other))
    (other / "README.md").write_text("theirs\n")
    sh(other, "commit", "-qam", "Theirs")
    sh(other, "push", "-q")
    (repo / "README.md").write_text("ours\n")
    sh(repo, "commit", "-qam", "Ours")
    with pytest.raises(wg.GitError) as exc:
        git(repo).change("push", {})
    assert exc.value.status == 409


def test_switch_and_make_branches(repo):
    made = git(repo).change("switch", {"branch": "hero-copy", "create": True})["status"]
    assert made["branch"]["head"] == "hero-copy"
    assert git(repo).change("switch", {"branch": "main"})["status"]["branch"]["head"] == "main"
    for name, status in (("bad name..", 400), ("-x", 400), ("hero-copy", 409)):
        with pytest.raises(wg.GitError) as exc:
            git(repo).change("switch", {"branch": name, "create": True})
        assert exc.value.status == status, name
    sh(repo, "checkout", "-q", "hero-copy")
    (repo / "README.md").write_text("on hero\n")
    sh(repo, "commit", "-qam", "Hero")
    sh(repo, "checkout", "-q", "main")
    (repo / "README.md").write_text("local edit\n")
    with pytest.raises(wg.GitError) as exc:
        git(repo).change("switch", {"branch": "hero-copy"})
    assert exc.value.status == 409 and "commit or discard" in str(exc.value)


def test_push_sets_its_upstream_then_pull_fast_forwards(repo, tmp_path):
    with pytest.raises(wg.GitError) as exc:
        git(repo).change("push", {})
    assert exc.value.status == 409
    bare = tmp_path / "remote.git"
    sh(tmp_path, "init", "-q", "--bare", str(bare))
    sh(repo, "remote", "add", "origin", str(bare))
    pushed = git(repo).change("push", {})["status"]
    assert pushed["branch"]["upstream"] == "origin/main" and pushed["branch"]["ahead"] == 0
    other = tmp_path / "other"
    sh(tmp_path, "clone", "-q", str(bare), str(other))
    (other / "README.md").write_text("from elsewhere\n")
    sh(other, "commit", "-qam", "Elsewhere")
    sh(other, "push", "-q")
    fetched = git(repo).change("fetch", {})["status"]
    assert fetched["branch"]["behind"] == 1
    git(repo).change("pull", {})
    assert (repo / "README.md").read_text() == "from elsewhere\n"


def test_an_unknown_action_is_not_found(repo):
    with pytest.raises(wg.GitError) as exc:
        git(repo).change("rebase", {})
    assert exc.value.status == 404


# --- SSH -------------------------------------------------------------------------


class _LocalShell:
    """Hermes' SSH adapter, its commands run by this machine's sh."""

    def __init__(self, cwd):
        self.cwd = str(cwd)

    def _normalize(self, path):
        import posixpath
        return posixpath.normpath(str(path))

    def _execute(self, command, *, stdin_data=None, timeout=None):
        done = subprocess.run(["sh", "-c", command], input=stdin_data, capture_output=True, text=True,
                              cwd=self.cwd, timeout=timeout or 30)
        return {"output": done.stdout + done.stderr, "returncode": done.returncode}


def test_ssh_reads_and_changes_the_same_way(repo):
    workspace = wf.SshWorkspace(_LocalShell(repo))
    remote = wg.for_workspace(workspace, str(repo), wf.default_sensitive)
    (repo / "README.md").write_text("# Site\n\nMore.\n")
    status = remote.status()
    assert status["repo"] and by_path(status)["README.md"]["added"] == 2
    assert "+More." in remote.diff("README.md")["diff"]
    done = remote.change("commit", {"message": "Over SSH\n\nBody line", "all": True})
    assert done["commit"] and done["status"]["files"] == []
    assert sh(repo, "log", "-1", "--format=%B").strip() == "Over SSH\n\nBody line"
    with pytest.raises(wg.GitError) as exc:
        remote.change("switch", {"branch": "main", "create": True})
    assert exc.value.status == 409


def test_ssh_without_git_says_so(repo):
    class NoGit(_LocalShell):
        def _execute(self, command, *, stdin_data=None, timeout=None):
            return {"output": "", "returncode": 98}

    remote = wg.Git(str(repo), wg.SshRunner(NoGit(repo)), wf.default_sensitive, posix=True, machine="ssh")
    with pytest.raises(wg.GitError) as exc:
        remote.status()
    assert exc.value.status == 501


def test_local_without_git_says_so(repo):
    remote = wg.Git(str(repo), wg.LocalRunner(git="conduit-no-such-git"), wf.default_sensitive)
    with pytest.raises(wg.GitError) as exc:
        remote.status()
    assert exc.value.status == 501 and "isn't installed" in str(exc.value)


# --- Routes ----------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api.workspace, "_workspace_files", lambda: wf)
    monkeypatch.setattr(api.workspace, "_workspace_module", wf)
    monkeypatch.setattr(api.workspace, "_workspace", lambda profile: wf.LocalWorkspace())
    monkeypatch.setattr(api.source_control, "_git_module", lambda: wg)
    monkeypatch.setattr(api.core, "_plugin_package_module",
                        lambda name: wg if name == "workspace_git" else pytest.fail(name))
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_routes_read_and_change(repo, client):
    root = str(repo)
    (repo / "README.md").write_text("# Site\n\nMore.\n")
    status = client.get(f"{BASE}/workspace/git/status", params={"root": root}).json()
    assert status["ok"] and [f["path"] for f in status["files"]] == ["README.md"]
    diff = client.get(f"{BASE}/workspace/git/diff", params={"root": root, "path": "README.md"}).json()
    assert "+More." in diff["diff"]
    staged = client.post(f"{BASE}/workspace/git/stage", json={"root": root, "paths": ["README.md"]}).json()
    assert staged["status"]["files"][0]["staged"] == "modified"
    done = client.post(f"{BASE}/workspace/git/commit", json={"root": root, "message": "More"}).json()
    assert done["commit"] and done["status"]["files"] == []
    log = client.get(f"{BASE}/workspace/git/log", params={"root": root, "limit": 5}).json()
    assert [c["subject"] for c in log["commits"]] == ["More", "First"]
    branches = client.get(f"{BASE}/workspace/git/branches", params={"root": root}).json()
    assert branches["local"][0]["name"] == "main"


def test_routes_say_why_they_refuse(repo, tmp_path, client):
    root = str(repo)
    assert client.get(f"{BASE}/workspace/git/status").status_code == 400
    missing = client.get(f"{BASE}/workspace/git/status", params={"root": str(tmp_path / "gone")})
    assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"
    secret = client.get(f"{BASE}/workspace/git/diff", params={"root": root, "path": ".env"})
    assert secret.status_code == 403
    assert client.post(f"{BASE}/workspace/git/rebase", json={"root": root}).status_code == 404
    nothing = client.post(f"{BASE}/workspace/git/commit", json={"root": root, "message": "x"})
    assert nothing.status_code == 409 and nothing.json()["detail"] == "Nothing is staged to commit"
    assert client.post(f"{BASE}/workspace/git/stage", json=["x"]).status_code == 400


def test_the_plugin_says_it_serves_git():
    assert "workspace-git" in api.capabilities.ROUTE_CAPABILITIES
