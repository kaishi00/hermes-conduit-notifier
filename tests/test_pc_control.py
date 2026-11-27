"""Control from Hermes: the conduit_user_computer tool, its queue and the
routes Conduit for desktop polls."""

import importlib.util
import json
import os
import pathlib
import sys
import threading
import time
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(os.environ["HERMES_HOME"])
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


store_mod = _load("conduit_push.pc_control_store", ROOT / "pc_control_store.py")
tool = _load("conduit_push.pc_control_tool", ROOT / "pc_control_tool.py")
_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_pc_control", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)

PC = "asuka-computer-1"
SESSION = "20261127_141500_abcdef"


@pytest.fixture(autouse=True)
def no_titles(monkeypatch):
    monkeypatch.setattr(tool.session_titles, "title", lambda key: "Tidy my downloads")


@pytest.fixture()
def store(tmp_path):
    return store_mod.PcControlStore(tmp_path)


class Desktop(threading.Thread):
    """Conduit on the computer: takes each request and answers it."""

    def __init__(self, store, answer, computer=PC):
        super().__init__(daemon=True)
        self.store, self.answer, self.computer = store, answer, computer
        self.seen = []
        self.done = threading.Event()

    def run(self):
        while not self.done.is_set():
            for request in self.store.claim(self.computer):
                self.seen.append(request)
                self.store.answer(request["id"], self.answer(request), computer=self.computer)
            time.sleep(0.01)


def run_tool(store, args, session=SESSION, answer=None, **kw):
    desktop = Desktop(store, answer) if answer else None
    if desktop:
        desktop.start()
    try:
        return tool.handle(args, session_id=session, store=store, sleep=lambda s: time.sleep(0.005), **kw), desktop
    finally:
        if desktop:
            desktop.done.set()
            desktop.join(1)


def test_offline_when_no_computer_asked(store):
    result, _ = run_tool(store, {"action": "capture"})
    assert json.loads(result)["error"] == "offline"
    assert not store.known()


def test_first_action_asks_then_the_chat_keeps_it(store):
    store.note_computer(PC, "Asuka", "windows")
    result, desktop = run_tool(store, {"action": "click", "element": 4},
                               answer=lambda r: {"ok": True, "grant": "allowed", "message": "clicked"})
    assert json.loads(result)["ok"] is True
    assert desktop.seen[0]["needs_ok"] is True and desktop.seen[0]["chat"] == "Tidy my downloads"
    assert desktop.seen[0]["args"] == {"element": 4}
    _, desktop = run_tool(store, {"action": "key", "keys": "enter"}, answer=lambda r: {"ok": True})
    assert desktop.seen[0]["needs_ok"] is False
    # Nothing of the round trip stays behind but the log.
    assert not list((store.root / "requests").iterdir()) and not list((store.root / "results").iterdir())
    log = (store.root / "log.jsonl").read_text().splitlines()
    assert [json.loads(line)["action"] for line in log] == ["click", "key"]
    assert "enter" not in "".join(log)


def test_another_chat_asks_and_takes_over(store):
    store.note_computer(PC, "Asuka", "windows")
    store.record_grant(PC, SESSION, "allowed", "Tidy my downloads")
    _, desktop = run_tool(store, {"action": "capture"}, session="other-session",
                          answer=lambda r: {"ok": True, "grant": "allowed"})
    assert desktop.seen[0]["needs_ok"] is True and desktop.seen[0]["takes_over"] == "Tidy my downloads"
    assert store.grant(PC)["session"] == "other-session"


def test_denied_and_stopped_tell_the_bot_not_to_retry(store):
    store.note_computer(PC, "Asuka", "windows")
    result, _ = run_tool(store, {"action": "capture"}, answer=lambda r: {"ok": False, "grant": "denied"})
    assert json.loads(result)["error"] == "denied"
    assert store.grant(PC)["state"] == "denied"
    result, _ = run_tool(store, {"action": "capture"}, answer=lambda r: {"ok": False, "code": "stopped"})
    assert json.loads(result)["error"] == "stopped"


def test_turning_a_chat_down_leaves_the_chat_that_has_it(store):
    store.record_grant(PC, SESSION, "allowed", "A")
    store.record_grant(PC, "other", "denied", "B")
    assert store.grant(PC)["session"] == SESSION


def test_stop_answers_what_waits_and_forgets_the_grant(store):
    store.note_computer(PC, "Asuka", "windows")
    store.record_grant(PC, SESSION, "allowed", "A")
    request = store.add_request(PC, SESSION, "A", "capture", {})
    assert store.stop(PC) == 1
    assert store.take_result(request["id"])["code"] == "stopped"
    assert store.grant(PC) is None and store.needs_ok(PC, SESSION)


def test_picks_a_computer_by_name_when_two_are_online(store):
    store.note_computer(PC, "Asuka", "windows")
    store.note_computer("mac-computer-1", "Agriass-Air", "mac")
    result, _ = run_tool(store, {"action": "capture"})
    assert json.loads(result)["error"] == "which_computer"
    result, _ = run_tool(store, {"action": "capture", "computer": "nope"})
    assert json.loads(result)["error"] == "unknown_computer"
    desktop = Desktop(store, lambda r: {"ok": True}, computer="mac-computer-1")
    desktop.start()
    try:
        result = tool.handle({"action": "capture", "computer": "agriass-air"}, session_id=SESSION, store=store,
                             sleep=lambda s: time.sleep(0.005))
    finally:
        desktop.done.set()
    assert json.loads(result)["computer"] == "Agriass-Air"


def test_a_computer_quiet_too_long_is_offline(tmp_path):
    now = [1000.0]
    store = store_mod.PcControlStore(tmp_path, clock=lambda: now[0])
    store.note_computer(PC, "Asuka", "windows")
    now[0] += store_mod.ONLINE_S + 1
    assert not store.online() and store.known()


def test_no_answer_in_time_drops_the_request(store, monkeypatch):
    store.note_computer(PC, "Asuka", "windows")
    monkeypatch.setattr(tool, "ASK_WAIT_S", 0.05)
    result, _ = run_tool(store, {"action": "capture"})
    assert json.loads(result)["error"] == "no_answer"
    assert not list((store.root / "requests").iterdir())


def test_screenshot_comes_back_as_an_image_with_elements(store):
    store.note_computer(PC, "Asuka", "windows")
    store.record_grant(PC, SESSION, "allowed", "A")
    elements = [{"index": 1, "role": "Button", "label": "Close", "bounds": [10, 0, 40, 30]}]
    result, _ = run_tool(store, {"action": "capture"},
                         answer=lambda r: {"ok": True, "image": "aGk=", "mime": "image/png", "elements": elements,
                                           "window_title": "Conduit", "width": 1394, "height": 1568})
    assert result["_multimodal"] is True
    assert result["content"][1]["image_url"]["url"] == "data:image/png;base64,aGk="
    assert "#1 Button 'Close' @ (10, 0, 40, 30)" in result["text_summary"]


@pytest.mark.parametrize("args, fragment", [
    ({"action": "click"}, "needs element"),
    ({"action": "type", "text": ""}, "needs text"),
    ({"action": "drag", "from_element": 1}, "drag needs"),
    ({"action": "launch_app"}, "needs app"),
    ({"action": "explode"}, "action must be"),
])
def test_bad_args_never_reach_the_computer(store, args, fragment):
    store.note_computer(PC, "Asuka", "windows")
    result, _ = run_tool(store, args)
    assert fragment in json.loads(result)["note"]
    assert not (store.root / "requests").exists() or not list((store.root / "requests").iterdir())


def test_a_subagent_cant_drive_it(store):
    store.note_computer(PC, "Asuka", "windows")
    result, _ = run_tool(store, {"action": "capture"}, is_child=lambda s: True)
    assert json.loads(result)["error"] == "subagent"


# --- Routes ---------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api.core, "_calls_home", lambda: tmp_path)
    monkeypatch.setattr(api.pc_control, "_pc_limiter", api.core._MintLimiter(1000, 60))
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    http = TestClient(app)
    http.store = store_mod.PcControlStore(tmp_path)
    return http


def test_next_marks_the_computer_online_and_hands_over_work(client):
    response = client.get(f"{BASE}/pc/next", params={"computer": PC, "name": "Asuka", "platform": "windows",
                                                      "wait": 0})
    assert response.status_code == 200 and response.json()["requests"] == []
    assert response.headers["cache-control"] == "no-store"
    assert client.store.online()[PC]["name"] == "Asuka"
    request = client.store.add_request(PC, SESSION, "A", "capture", {"mode": "som"})
    taken = client.get(f"{BASE}/pc/next", params={"computer": PC, "wait": 0}).json()["requests"]
    assert [item["id"] for item in taken] == [request["id"]]
    # Taken once: a second poll doesn't run it again.
    assert client.get(f"{BASE}/pc/next", params={"computer": PC, "wait": 0}).json()["requests"] == []
    answered = client.post(f"{BASE}/pc/result", json={"computer": PC, "id": request["id"], "result": {"ok": True}})
    assert answered.json()["taken"] is True
    assert client.store.take_result(request["id"]) == {"ok": True}


def test_next_waits_for_work_that_arrives(client):
    client.store.note_computer(PC, "Asuka", "windows")
    timer = threading.Timer(0.3, lambda: client.store.add_request(PC, SESSION, "A", "capture", {}))
    timer.start()
    started = time.monotonic()
    taken = client.get(f"{BASE}/pc/next", params={"computer": PC, "wait": 5}).json()["requests"]
    assert len(taken) == 1 and time.monotonic() - started < 3


def test_another_computers_answer_is_refused(client):
    request = client.store.add_request(PC, SESSION, "A", "capture", {})
    answered = client.post(f"{BASE}/pc/result",
                           json={"computer": "someone-else-1", "id": request["id"], "result": {"ok": True}})
    assert answered.json()["taken"] is False


def test_stop_and_turning_off(client):
    client.store.note_computer(PC, "Asuka", "windows")
    client.store.record_grant(PC, SESSION, "allowed", "A")
    assert client.post(f"{BASE}/pc/stop", json={"computer": PC}).json()["stopped"] == 0
    assert client.store.grant(PC) is None
    assert client.delete(f"{BASE}/pc/computer", params={"computer": PC}).status_code == 200
    assert client.store.computers() == {}


def test_a_bad_computer_id_is_a_400(client):
    response = client.get(f"{BASE}/pc/next", params={"computer": "../x", "wait": 0})
    assert response.status_code == 400
