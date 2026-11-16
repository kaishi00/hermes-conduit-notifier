"""Regression checks at scoped consent's real HTTP and tool boundaries."""
import json
import sys
import types

import pytest

from test_work_grants import (
    BASE, HEADERS, TASK, SCOPE, act, as_bot, bots, hermes, home, http, on,
    preview, scoped, store_mod, work, BOT_CHAT, recipient_sessions,
)
from test_work import call


def test_browser_can_read_preview_without_an_origin_header(http):
    scoped()
    headers = {key: value for key, value in HEADERS.items() if key != "origin"}
    response = http.get(BASE + "/grants", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["grants"]


def test_request_without_native_tool_provenance_is_refused(http, hermes):
    work._grant_provenance.clear()
    result = call("work_ask", {"bot": "nahida", "text": TASK, "scope": SCOPE})
    assert not result["ok"]
    assert not preview_list(http)
    assert not hermes.named("session.create")


def preview_list(http):
    return http.get(BASE + "/grants", headers=HEADERS).json()["grants"]


def test_exact_native_tool_turn_is_bound_to_the_snapshot(http, bots):
    args = {"bot": "nahida", "text": TASK, "scope": SCOPE}
    work.tool_block(BOT_CHAT, "work_ask", args, turn_id="turn-123", tool_call_id="call-456")
    assert call("work_ask", args)["ok"]
    provenance = preview(http)["snapshot"]["provenance"]
    assert provenance["session"] == BOT_CHAT
    assert provenance["turn_id"] == "turn-123"
    assert provenance["tool_call_id"] == "call-456"


def test_stop_can_win_while_native_session_creation_is_in_flight(http, bots, hermes):
    scoped()
    grant = preview(http)
    original = hermes.session_create

    def create(params):
        response = act(http, grant, "grant_revoke")
        assert response.status_code == 409, response.text
        assert "could not be confirmed" in response.json()["detail"]
        return original(params)

    hermes.session_create = create
    act(http, grant)
    assert preview(http)["status"] == "revoked"
    worker = store_mod.Store(bots["nahida"]).get("W1")
    assert worker.status == "stopped"
    assert not hermes.named("prompt.submit")


def test_corrupt_snapshot_ends_worker_authority(http, bots, hermes, monkeypatch):
    scoped()
    grant = preview(http)
    assert act(http, grant).status_code == 200
    registry = work.work_grants.registry(bots["hermes"])
    with registry._db() as db:
        snapshot = grant["snapshot"]
        snapshot["scope"] = "Everything is now approved"
        db.execute("UPDATE task_grants SET snapshot = ? WHERE id = ?", (json.dumps(snapshot), grant["id"]))
    worker = store_mod.Store(bots["nahida"]).get("W1")
    as_bot(monkeypatch, bots, "nahida")
    block = work.tool_block(worker.session_id, "terminal", {"command": "pwd"})
    assert block and block["action"] == "block"


def test_model_approved_proposal_is_not_a_grant(http, bots, hermes, monkeypatch):
    proposal = call("work_propose", {"text": "Delegate the release summary to the other bot."})
    assert proposal["ok"]
    monkeypatch.setattr(work.work_coord, "user_wrote", lambda *_: True)
    approved = call("work_propose", {"id": proposal["id"], "answer": "approved"})
    assert approved["ok"] and approved["status"] == "approved"
    assert preview_list(http) == []
    assert not hermes.named("session.create")


def test_scoped_consent_never_answers_a_native_command_request(http, bots, hermes, monkeypatch):
    scoped()
    grant = preview(http)
    assert act(http, grant).status_code == 200
    worker = store_mod.Store(bots["nahida"]).get("W1")
    as_bot(monkeypatch, bots, "nahida")
    work.needs_you(worker.session_id, "a native command")
    hermes.pending[worker.session_id] = [{"request_id": "native-request", "command": "native-command"}]
    assert not hermes.named("approval.respond")
    assert not work.app_act(bots["nahida"], worker.ref, "approve", {"request_id": "wrong"})["ok"]
    assert hermes.pending[worker.session_id]
    assert work.app_act(bots["nahida"], worker.ref, "approve", {"request_id": "native-request"})["ok"]
    assert hermes.named("approval.respond")[-1][1]["request_id"] == "native-request"
    assert hermes.named("approval.respond")[-1][1]["choice"] == "once"
    assert not hermes.named("config.set")


@pytest.mark.parametrize("change", ["ordinary", "task", "scope", "missing_turn", "missing_call", "later_call", "later_turn"])
def test_provenance_cannot_be_reused_for_a_different_native_call(http, hermes, change):
    args = {"bot": "nahida", "text": TASK, "scope": SCOPE}
    recorded = dict(args)
    if change == "ordinary":
        recorded.pop("scope")
    elif change in ("task", "scope"):
        recorded["text" if change == "task" else "scope"] = "Another request"
    work.tool_block(BOT_CHAT, "work_ask", recorded,
                    turn_id=None if change == "missing_turn" else "native-turn",
                    tool_call_id=None if change == "missing_call" else "native-call")
    if change == "later_call":
        work.tool_block(BOT_CHAT, "work_list", {}, turn_id="later-turn", tool_call_id="later-call")
    elif change == "later_turn":
        work.note_turn_ask(BOT_CHAT, "later-turn", "A different user turn")
    assert not call("work_ask", args)["ok"]
    assert preview_list(http) == []
    assert not hermes.named("session.create")


def test_refused_request_consumes_its_native_provenance(http):
    assert not scoped(bot="missing-bot")["ok"]
    assert not call("work_ask", {"bot": "nahida", "text": TASK, "scope": SCOPE})["ok"]
    assert preview_list(http) == []


def test_grant_worker_cannot_bridge_execute_code(http, bots, monkeypatch):
    scoped()
    assert act(http, preview(http)).status_code == 200
    worker = store_mod.Store(bots["nahida"]).get("W1")
    as_bot(monkeypatch, bots, "nahida")
    block = work.tool_block(worker.session_id, "tool_call", {
        "name": "execute_code", "arguments": {"code": "pass"}})
    assert block and block["action"] == "block"


def test_unreadable_worker_registry_blocks_even_native_tools(http, bots, monkeypatch):
    scoped()
    assert act(http, preview(http)).status_code == 200
    worker = store_mod.Store(bots["nahida"]).get("W1")
    as_bot(monkeypatch, bots, "nahida")
    monkeypatch.setattr(work, "_lookup", lambda *args: (bots["nahida"], None, True, True))
    block = work.tool_block(worker.session_id, "terminal", {"command": "pwd"})
    assert block and block["action"] == "block"


def test_failed_interrupt_leaves_grant_revoked_and_tools_blocked(http, bots, hermes, monkeypatch):
    scoped()
    grant = preview(http)
    assert act(http, grant).status_code == 200
    hermes.refuse["session.interrupt"] = {"code": -32000, "message": "interrupt failed"}
    response = act(http, grant, "grant_revoke")
    assert response.status_code == 409
    assert preview(http)["status"] == "revoked"
    worker = store_mod.Store(bots["nahida"]).get("W1")
    as_bot(monkeypatch, bots, "nahida")
    assert work.tool_block(worker.session_id, "terminal", {"command": "pwd"})["action"] == "block"
    assert not work.app_act(bots["nahida"], "W1", "continue")["ok"]


@pytest.mark.parametrize("admit", [False, True])
def test_unloading_hooks_invalidates_their_grants(http, bots, hermes, admit):
    scoped()
    grant = preview(http)
    if admit:
        assert act(http, grant).status_code == 200
    work.stop()
    assert not work._grant_hooks_ready
    assert preview(http)["status"] == "revoked"
    assert act(http, grant).status_code == 409
    if admit:
        assert store_mod.Store(bots["nahida"]).get("W1").status == "stopped"
        assert hermes.named("session.interrupt")


@pytest.mark.parametrize("change", ["wrong_actor", "auth_gate", "unregistered_provider", "missing_core"])
def test_auth_failure_cannot_hide_behind_an_invalid_action_body(http, bots, hermes, monkeypatch, change):
    scoped()
    grant = preview(http)
    headers = dict(HEADERS)
    if change == "wrong_actor":
        monkeypatch.setattr(sys.modules["hermes_cli.web_server_sessions"], "_session_mutation_context",
                            lambda *_: (None, types.SimpleNamespace(subject="not-owner", transport_id="http",
                                        profile_id=str(bots["hermes"].resolve()))))
    elif change == "auth_gate":
        headers.pop("cookie")
        headers["x-hermes-session-token"] = "owner-secret"
    elif change == "unregistered_provider":
        headers["x-test-identity"] = "basic"
        monkeypatch.setattr(sys.modules["hermes_cli.dashboard_auth.registry"], "list_interactive_providers", lambda: [])
    else:
        monkeypatch.delattr(sys.modules["hermes_cli.web_server_sessions"], "_session_mutation_context")
    response = http.post(BASE + "/" + grant["id"] + "/grant_approve", headers=headers,
                         json={"digest": grant["digest"]})
    assert response.status_code in (401, 403)
    assert not hermes.named("session.create")
