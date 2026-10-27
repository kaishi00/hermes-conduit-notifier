"""The upstream watch's own pieces (upstream/): the generated contract and the issue keys."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(f"upstream_{name}", ROOT / "upstream" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


contract = _load("contract")
report = _load("report")


def test_contract_matches_the_code():
    _, current = contract.split_contract()
    assert current == contract.generated(), "Run `python3 upstream/contract.py write` and commit contract.yml"


def test_contract_names_the_socket_auth_and_the_routes():
    imports = contract.hermes_imports()
    assert "_ws_auth_ok" in imports["hermes_cli.web_server_chat"]["optional"]
    assert "WS /local-live/socket" in contract.plugin_routes()


def test_watched_paths():
    modules = ["hermes_cli/web_server_chat", "hermes_cli/dashboard_auth", "hermes_cli"]
    patterns = ["hermes_cli/web_server*.py", "hermes_cli/web_routers/"]
    assert contract.watched("hermes_cli/web_server_chat.py", modules, [])
    assert contract.watched("hermes_cli/dashboard_auth/base.py", modules, [])
    assert contract.watched("hermes_cli/web_server_plugin_profiles.py", [], patterns)
    assert contract.watched("hermes_cli/web_routers/files.py", [], patterns)
    # A top-level package the plugin imports a name from isn't the whole package.
    assert not contract.watched("hermes_cli/kanban.py", modules, patterns)
    assert not contract.watched("hermes_cli/web_server/deep/x.py", [], patterns)


def test_signature_drops_what_changes_between_runs():
    a = report.signature("RuntimeError: the dashboard exited (1) at /tmp/pytest-of-runner/pytest-12/x\nlog...")
    b = report.signature("RuntimeError: the dashboard exited (1) at /tmp/pytest-of-runner/pytest-99/x\nother")
    assert a == b == "RuntimeError: the dashboard exited (1) at <path>"


def _junit(tmp_path, cases):
    body = "".join(
        f'<testcase classname="{c}" name="{n}"><{tag} message="{m}">detail</{tag}></testcase>'
        for c, n, tag, m in cases)
    path = tmp_path / "junit.xml"
    path.write_text(f'<testsuites><testsuite name="pytest">{body}</testsuite></testsuites>')
    return path


def test_parametrized_cases_and_setup_errors_share_an_issue(tmp_path):
    junit = _junit(tmp_path, [
        ("t", "test_socket[local-live/socket]", "failure", "HTTP 501: a"),
        ("t", "test_socket[speech-server/socket]", "failure", "HTTP 501: a"),
        ("t", "test_socket[speech-server/socket]", "failure", "close 4503: b"),
        ("t", "test_a", "error", "RuntimeError: the dashboard did not start"),
        ("t", "test_b", "error", "RuntimeError: the dashboard did not start"),
    ])
    found = report.failures(junit)
    assert [(f["test"], f["signature"]) for f in found] == [
        ("t.test_socket", "HTTP 501: a"),
        ("t.test_socket", "close 4503: b"),
        ("harness", "RuntimeError: the dashboard did not start"),
    ]
    assert len({f["key"] for f in found}) == 3


def test_the_key_survives_another_tag(tmp_path):
    first = report.failures(_junit(tmp_path, [("t", "test_x", "failure", "HTTP 501: refused")]))
    again = report.failures(_junit(tmp_path, [("t", "test_x", "failure", "HTTP 501: refused")]))
    assert first[0]["key"] == again[0]["key"]
    assert report.MARKER.format(key=first[0]["key"]) in report.issue_body(first[0], "v1", "", "")
