"""The bridge's envelopes and store (designs/cross-owner-agents, N4)."""
import base64
import importlib.util
import json
import pathlib
import stat
import sys
import tempfile
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bridge = _load("conduit_push.room_bridge", ROOT / "room_bridge.py")

SCOPE = {"home_install_id": "gw-furina", "room_id": "room-zoo", "member_id": "m-laifu", "target_profile": "default"}


def grant(scope=None, **payload):
    """A grant shaped like Hermes' (hosted_room_peer.issue_room_grant): base64url JSON, a dot, a signature."""
    fields = {"version": 2, "grant_id": "grant-1", "authority_gateway_id": "gw-furina", "authority_epoch": 1,
              "target_install_id": "gw-laifu", "execution_policy_digest": "0" * 64,
              "permissions": ["approve", "dispatch", "status", "stop"], "issued_at": 1.0, "expires_at": 2.0,
              "status_expires_at": 3.0, **(SCOPE if scope is None else scope), **payload}
    encoded = base64.urlsafe_b64encode(json.dumps(fields).encode()).rstrip(b"=").decode()
    return f"{encoded}.{base64.urlsafe_b64encode(b'signature' * 4).rstrip(b'=').decode()}"


def request(**changes):
    body = {"v": 1, "profile": "default", "method": "POST", "path": "/v1/runs", "grant": grant(),
            "key": "room:t1:1", "body": {"hosted_room_dispatch": {}}}
    body.update(changes)
    return {name: value for name, value in body.items() if value is not ...}


def test_a_request_is_read_with_its_fields():
    read = bridge.request_from(request())

    assert (read.profile, read.method, read.path, read.key) == ("default", "POST", "/v1/runs", "room:t1:1")
    assert read.grant == grant() and read.body == {"hosted_room_dispatch": {}}
    assert "grant" not in repr(read) or grant() not in repr(read)
    assert bridge.request_from(request(key=..., body=...)).key is None


@pytest.mark.parametrize("changes", [
    {"v": 2}, {"v": True}, {"v": ...}, {"extra": 1}, {"grant": ...},
    {"profile": "Laifu"}, {"profile": "../default"}, {"profile": ""},
    {"method": "DELETE"}, {"method": "get"},
    {"path": "v1/runs"}, {"path": "/v1/runs with space"}, {"path": "/" + "a" * 600},
    {"grant": "a" * (16 * 1024 + 1)}, {"grant": "with space.x"}, {"grant": 7},
    {"key": "with space"}, {"key": 5}, {"body": []}, {"body": "{}"},
])
def test_what_isnt_a_request_is_none(changes):
    assert bridge.request_from(request(**changes)) is None
    assert bridge.request_from("not a body") is None


def test_a_grants_scope_is_read_from_its_payload():
    assert bridge.grant_scope(grant()) == SCOPE
    assert bridge.grant_scope(grant(dict(SCOPE, target_profile="work"))) == dict(SCOPE, target_profile="work")


@pytest.mark.parametrize("value", [
    "no-dot", ".signature", "!!!.sig", base64.urlsafe_b64encode(b"[1, 2]").decode() + ".sig",
    grant(dict(SCOPE, room_id=None)), grant(dict(SCOPE, room_id="room zoo")), grant(dict(SCOPE, member_id=7)),
    grant(dict(SCOPE, target_profile="Work")), grant({k: v for k, v in SCOPE.items() if k != "member_id"}),
    None, 12,
])
def test_a_grant_it_cant_read_has_no_scope(value):
    assert bridge.grant_scope(value) is None


def test_bodies_it_sends():
    shown = {"run_id": "run_1", "status": "completed", "output": "Yes!"}

    assert bridge.response_body("m1", 202, {"run_id": "run_1"}) == {"v": 1, "re": "m1", "status": 202,
                                                                   "body": {"run_id": "run_1"}}
    assert bridge.response_body("m1", 202, {}, run=shown)["run"] == shown
    assert bridge.status_body("run_1", 3, shown) == {"v": 1, "run_id": "run_1", "seq": 3, "status": "completed",
                                                     "output": "Yes!"}
    assert bridge.status_body("run_1", 4, {"run_id": "run_1", "status": "failed", "error": "No."}) == {
        "v": 1, "run_id": "run_1", "seq": 4, "status": "failed", "error": "No."}


def test_the_store_is_owner_only_and_keeps_a_section_it_doesnt_know(tmp_path):
    store = bridge.Store(tmp_path)
    with store.update() as state:
        state["member"]["bindings"]["link1"] = [SCOPE]
        state["home"]["port"] = 8700
        state["later"] = {"rooms": 1}

    assert stat.S_IMODE((tmp_path / "conduit-rooms" / "bridge.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "conduit-rooms").stat().st_mode) == 0o700
    read = store.read()
    assert read["member"]["bindings"] == {"link1": [SCOPE]} and read["home"]["port"] == 8700
    assert read["later"] == {"rooms": 1}
    assert bridge.Store(tmp_path / "empty").read()["member"] == {"bindings": {}, "runs": {}, "days": {}}


HOME = {"port": 8700, "runs": {}, "kept": {}, "outbox": []}


def test_a_store_with_one_sides_section_has_the_others_empty(tmp_path):
    for side, section in (("furina", {"home": HOME}), ("laifu", {"member": {"bindings": {}, "runs": {}, "days": {}}})):
        (tmp_path / side / "conduit-rooms").mkdir(parents=True)
        (tmp_path / side / "conduit-rooms" / "bridge.json").write_text(json.dumps({"version": 1, **section}))

    furina, laifu = bridge.Store(tmp_path / "furina").read(), bridge.Store(tmp_path / "laifu").read()

    assert furina["member"] == {"bindings": {}, "runs": {}, "days": {}} and furina["home"] == HOME
    # As the member's side alone wrote it, before the home's had a section.
    assert laifu["home"] == {"port": None, "runs": {}, "kept": {}, "outbox": []}


@pytest.mark.parametrize("content", [
    "not json", "[]", '{"version": 2}', '{"version": true}', '{"version": 1, "member": []}',
    '{"version": 1, "member": {"bindings": {"l": [{"room_id": "x"}]}, "runs": {}, "days": {}}}',
    '{"version": 1, "member": {"bindings": {}, "runs": {"r": {"kind": "run"}}, "days": {}}}',
    '{"version": 1, "home": []}', '{"version": 1, "home": {"port": 8700}}',
    '{"version": 1, "home": {"port": "8700", "runs": {}, "kept": {}, "outbox": []}}',
    '{"version": 1, "home": {"port": 0, "runs": {}, "kept": {}, "outbox": []}}',
    '{"version": 1, "home": {"port": null, "runs": {"l/r": {"link": "l"}}, "kept": {}, "outbox": []}}',
    '{"version": 1, "home": {"port": null, "runs": {}, "kept": {"l/k": {}}, "outbox": []}}',
    '{"version": 1, "home": {"port": null, "runs": {}, "kept": {}, "outbox": [{"id": "m1", "link": "l"}]}}',
])
def test_a_store_it_didnt_write_is_unreadable(tmp_path, content):
    (tmp_path / "conduit-rooms").mkdir()
    (tmp_path / "conduit-rooms" / "bridge.json").write_text(content)

    with pytest.raises(bridge.BridgeError) as refused:
        bridge.Store(tmp_path).read()
    assert refused.value.code == "unreadable"


# Each kind of entry as the two sides write it.
WATCHED = {"link": "link1", "kind": "run", "key": "room:t1:1", "profile": "default", "grant": "g", "admitted": 1.0,
           "home_stopped": False, "shown": {"status": "queued"}, "sent": {"status": "queued"}, "sent_at": 1.0,
           "seq": 0, "ended": None}
PASSED = {"link": "link1", "kind": "pass", "key": "room:t2:1", "output": "(pass)", "at": 1.0}
DAY = {"keys": ["room:t1:1"], "notice": False}
CARRIED = {"link": "link1", "run": "run_1", "profile": "default", "grant": "g", "grants": ["0" * 64], "name": "Laifu",
           "admitted": 1.0, "heard": 1.0, "seq": 0, "shown": {"status": "queued"}, "ended": None}
KEPT = {"run": "run_1", "status": "started", "at": 1.0, "profile": "default", "grants": ["0" * 64], "own": False}
OUTGOING = {"id": "m1", "link": "link1", "at": 1.0, "request": {"v": 1, "profile": "default", "method": "POST",
                                                               "path": "/v1/runs/run_1/stop", "grant": "g"}}


def _written(watched=WATCHED, passed=PASSED, day=DAY, carried=CARRIED, kept=KEPT, outgoing=OUTGOING):
    return {"version": 1,
            "member": {"bindings": {"link1": [SCOPE]}, "runs": {"run_1": watched, "pass_1": passed},
                       "days": {"2026-10-27": {"default/room-zoo": day}}},
            "home": {"port": 8700, "runs": {"link1/run_1": carried}, "kept": {"link1/room:t1:1": kept},
                     "outbox": [outgoing]}}


def test_a_store_with_every_kind_of_entry_reads_as_written(tmp_path):
    (tmp_path / "conduit-rooms").mkdir()
    (tmp_path / "conduit-rooms" / "bridge.json").write_text(json.dumps(_written()))

    assert bridge.Store(tmp_path).read() == _written()


def _without(entry, name):
    return {key: value for key, value in entry.items() if key != name}


@pytest.mark.parametrize("entries", [
    *({"watched": _without(WATCHED, name)} for name in
      ("link", "kind", "profile", "grant", "admitted", "shown", "sent_at", "seq")),
    {"watched": {**WATCHED, "kind": "later"}}, {"watched": {**WATCHED, "shown": {}}},
    {"watched": {**WATCHED, "seq": "0"}}, {"watched": {**WATCHED, "admitted": True}},
    {"watched": {**WATCHED, "ended": "now"}},
    *({"passed": _without(PASSED, name)} for name in ("output", "at")),
    {"day": {"keys": "room:t1:1", "notice": False}}, {"day": {"keys": []}},
    *({"carried": _without(CARRIED, name)} for name in
      ("profile", "grant", "grants", "name", "admitted", "heard", "seq", "shown", "ended")),
    {"carried": {**CARRIED, "shown": {"output": "Yes!"}}}, {"carried": {**CARRIED, "grants": "0" * 64}},
    {"carried": {**CARRIED, "grants": [1]}},
    *({"kept": _without(KEPT, name)} for name in ("status", "at", "profile", "grants", "own")),
    {"kept": {**KEPT, "grants": "0" * 64}}, {"kept": {**KEPT, "own": "yes"}},
    {"outgoing": _without(OUTGOING, "at")},
])
def test_an_entry_the_bridge_cant_use_makes_the_store_unreadable(tmp_path, entries):
    # One such entry would stop the watching of every run, each pass.
    (tmp_path / "conduit-rooms").mkdir()
    (tmp_path / "conduit-rooms" / "bridge.json").write_text(json.dumps(_written(**entries)))

    with pytest.raises(bridge.BridgeError) as refused:
        bridge.Store(tmp_path).read()
    assert refused.value.code == "unreadable"
