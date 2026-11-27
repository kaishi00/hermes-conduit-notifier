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


def test_a_claimed_request_whose_tool_went_away_is_swept_with_its_result(tmp_path):
    now = [1000.0]
    store = store_mod.PcControlStore(tmp_path, clock=lambda: now[0])
    store.note_computer(PC, "Asuka", "windows")
    request = store.add_request(PC, SESSION, "A", "capture", {})
    assert store.claim(PC)
    assert store.answer(request["id"], {"ok": True, "image": "aGk="}, computer=PC)
    now[0] += store_mod.STALE_S + 1
    store.has_waiting(PC)
    assert not list((store.root / "requests").iterdir())
    assert not list((store.root / "results").iterdir())


def test_stop_takes_waiting_actions_off_the_queue_and_keeps_answers(store):
    store.note_computer(PC, "Asuka", "windows")
    waiting = store.add_request(PC, SESSION, "A", "click", {"element": 1})
    ran = store.add_request(PC, SESSION, "A", "capture", {})
    store.claim(PC)
    store.add_request(PC, SESSION, "A", "type", {"text": "x"})
    store.answer(ran["id"], {"ok": True}, computer=PC)
    assert store.stop(PC) == 2
    assert store.claim(PC) == []
    assert store.take_result(ran["id"]) == {"ok": True}
    assert store.take_result(waiting["id"]) == {"ok": False, "code": "stopped"}


def test_a_stopped_request_that_couldnt_move_still_never_runs(store, monkeypatch):
    store.note_computer(PC, "Asuka", "windows")
    store.add_request(PC, SESSION, "A", "click", {"element": 1})
    real = store_mod.os.replace

    def held(src, dst, *rest):
        if str(src).endswith(".json") and str(dst).endswith(".claimed"):
            raise PermissionError("in use")
        return real(src, dst, *rest)
    monkeypatch.setattr(store_mod.os, "replace", held)
    assert store.stop(PC) == 1
    monkeypatch.undo()
    assert store.claim(PC) == []


def test_an_answer_after_the_tool_gave_up_is_not_kept(store):
    store.note_computer(PC, "Asuka", "windows")
    request = store.add_request(PC, SESSION, "A", "capture", {})
    store.claim(PC)
    store.drop(request["id"])
    assert store.answer(request["id"], {"ok": True}, computer=PC) is False
    assert not list((store.root / "results").iterdir())


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
    ({"action": "wait", "seconds": "about 5"}, "seconds is a number"),
    ({"action": "wait", "seconds": -1}, "seconds is a number"),
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


# --- The ask on the phone (stage 2) -------------------------------------------------

present_mod = _load("conduit_push.present_store", ROOT / "present_store.py")
PHONE = "phone-install-1"


def _phone(present, features=("pc-asks",), results=()):
    return present.exchange({"installation_id": PHONE, "features": list(features), "results": list(results)})


def test_the_ask_goes_to_a_phone_that_shows_them_and_its_allow_lets_the_chat_in(store, tmp_path):
    present = present_mod.PresentStore(tmp_path)
    _phone(present)
    store.note_computer(PC, "Asuka", "windows")
    pushed = []

    def desktop_answer(request):
        # The computer follows the phone's copy: the phone answers first.
        prompt = _phone(present)["prompts"][0]
        assert prompt["pc"] == {"computer": PC, "name": "Asuka", "bot": ""}
        assert prompt["title"] == "Allow Hermes to control Asuka?" and "Tidy my downloads" in prompt["preview"]
        assert "session_id" not in prompt or not prompt["session_id"]
        _phone(present, results=[{"id": prompt["id"], "outcome": "confirmed"}])
        assert present.pc_ask(request["ask"])["status"] == "confirmed"
        # Its own dialog's late answer can't overturn it.
        assert present.settle_pc(request["ask"], False, computer=PC) == "confirmed"
        return {"ok": True, "grant": "allowed"}

    result, desktop = run_tool(store, {"action": "capture"}, answer=desktop_answer, present=present,
                               push=pushed.append)
    assert json.loads(result)["ok"] is True
    assert desktop.seen[0]["ask"] and pushed[0]["type"] == "phone.changes" and pushed[0]["mode"] == "alert"
    assert store.grant(PC)["state"] == "allowed"
    # Nothing waits on the phone afterwards, and the chat's next action doesn't ask.
    assert _phone(present)["prompts"] == []
    _, desktop = run_tool(store, {"action": "capture"}, answer=lambda r: {"ok": True}, present=present,
                          push=pushed.append)
    assert "ask" not in desktop.seen[0] and len(pushed) == 1


def test_a_phone_without_the_feature_gets_no_ask_and_the_computer_still_asks(store, tmp_path):
    present = present_mod.PresentStore(tmp_path)
    _phone(present, features=())
    store.note_computer(PC, "Asuka", "windows")
    pushed = []
    _, desktop = run_tool(store, {"action": "capture"}, answer=lambda r: {"ok": True, "grant": "allowed"},
                          present=present, push=pushed.append)
    assert desktop.seen[0]["needs_ok"] is True and "ask" not in desktop.seen[0] and pushed == []


def test_an_ask_nobody_answered_leaves_the_phone_when_the_tool_gives_up(store, tmp_path, monkeypatch):
    present = present_mod.PresentStore(tmp_path)
    _phone(present)
    store.note_computer(PC, "Asuka", "windows")
    monkeypatch.setattr(tool, "ASK_WAIT_S", 0.05)
    result, _ = run_tool(store, {"action": "capture"}, present=present, push=lambda e: None)
    assert json.loads(result)["error"] == "no_answer"
    assert _phone(present)["prompts"] == []


def test_a_phone_edit_or_decline_is_a_no_and_never_an_outcome_for_chats(tmp_path):
    present = present_mod.PresentStore(tmp_path)
    _phone(present)
    prompt = present.ask_pc("Allow?", "Chat: A", pc={"computer": PC, "request": "request-0001", "name": "Asuka"})
    _phone(present, results=[{"id": prompt["id"], "outcome": "edit", "edit_text": "no"}])
    assert present.pc_ask(prompt["id"])["status"] == "declined"
    assert present.status() == [] and present.unreported_outcomes() == []
    # Another computer can't settle it.
    other = present.ask_pc("Allow?", "Chat: A", pc={"computer": PC, "request": "request-0002", "name": "Asuka"})
    assert present.settle_pc(other["id"], True, computer="someone-else-1") == "gone"
    assert present.settle_pc(other["id"], True, computer=PC) == "confirmed"


def test_ask_routes_follow_and_settle_the_phones_copy(client, tmp_path):
    present = present_mod.PresentStore(tmp_path)
    _phone(present)
    prompt = present.ask_pc("Allow?", "Chat: A", pc={"computer": PC, "request": "request-0001", "name": "Asuka"})
    ask = lambda computer=PC: client.get(f"{BASE}/pc/ask", params={"computer": computer, "id": prompt["id"]}).json()
    assert ask()["status"] == "waiting"
    assert ask("someone-else-1")["status"] == "gone"
    settled = client.post(f"{BASE}/pc/ask", json={"computer": PC, "id": prompt["id"], "answer": "denied"}).json()
    assert settled["status"] == "denied" and ask()["status"] == "denied"
    # The first answer stands.
    again = client.post(f"{BASE}/pc/ask", json={"computer": PC, "id": prompt["id"], "answer": "allowed"}).json()
    assert again["status"] == "denied"
    assert client.post(f"{BASE}/pc/ask", json={"computer": PC, "id": prompt["id"], "answer": "maybe"}).status_code == 400


def test_state_says_who_is_controlling_and_stop_from_the_phone_reaches_the_computer(client):
    client.store.note_computer(PC, "Asuka", "windows")
    assert client.get(f"{BASE}/pc/state").json()["controlling"] == []
    client.store.record_grant(PC, SESSION, "allowed", "Tidy my downloads")
    controlling = client.get(f"{BASE}/pc/state").json()["controlling"]
    assert [(item["computer"], item["name"], item["session"], item["chat"]) for item in controlling] == [
        (PC, "Asuka", SESSION, "Tidy my downloads")]
    assert client.get(f"{BASE}/pc/next", params={"computer": PC, "wait": 0}).json()["stopped"] is False
    started = time.monotonic()
    threading.Timer(0.3, lambda: client.post(f"{BASE}/pc/stop", json={"computer": PC})).start()
    polled = client.get(f"{BASE}/pc/next", params={"computer": PC, "wait": 10}).json()
    # The open poll comes back once Stop is pressed elsewhere, so the computer's bar goes.
    assert polled["stopped"] is True and time.monotonic() - started < 3
    assert client.get(f"{BASE}/pc/state").json()["controlling"] == []


def test_a_chat_that_stopped_acting_isnt_controlling(tmp_path):
    now = [1000.0]
    store = store_mod.PcControlStore(tmp_path, clock=lambda: now[0])
    store.note_computer(PC, "Asuka", "windows")
    store.record_grant(PC, SESSION, "allowed", "A")
    assert len(store.controlling()) == 1
    now[0] += store_mod.CONTROLLING_S + 1
    assert store.controlling() == []


def test_stop_while_the_first_action_ran_lets_nobody_back_in(store):
    store.note_computer(PC, "Asuka", "windows")

    def answer(request):
        store.stop(PC)  # pressed on the phone while the action ran
        return {"ok": True, "grant": "allowed"}

    run_tool(store, {"action": "capture"}, answer=answer)
    assert store.grant(PC) is None


def test_a_long_name_still_reaches_the_phone_and_a_finished_ask_never_breaks_loads(store, tmp_path):
    present = present_mod.PresentStore(tmp_path)
    _phone(present)
    store.note_computer(PC, "A" * 64, "windows")
    _, desktop = run_tool(store, {"action": "capture"}, answer=lambda r: {"ok": True, "grant": "allowed"},
                          present=present, push=lambda e: None)
    assert desktop.seen[0].get("ask")
    # A finished computer's ask left unreported (a hand-edited file) loads fine.
    path = tmp_path / present_mod.FILE_NAME
    state = json.loads(path.read_text())
    for prompt in state["prompts"]:
        prompt["reported"] = False
    path.write_text(json.dumps(state))
    assert _phone(present)["prompts"] == []


def test_turn_note_names_online_computers_and_the_tool(tmp_path):
    clock = [1000.0]
    store = store_mod.PcControlStore(tmp_path, clock=lambda: clock[0])
    assert tool.note(store) is None
    store.note_computer(PC, "Asuka", "windows")
    store.note_computer("mac-computer-1", "Agriass-Air", "mac")
    text = tool.note(store)
    assert "Agriass-Air, Asuka" in text and "conduit_user_computer" in text and "computer_use" in text
    # Offline: no line, though the tool stays listed (known for 30 days).
    clock[0] += store_mod.ONLINE_S + 1
    assert tool.note(store) is None
    assert store.known()


def test_turn_note_never_raises(tmp_path):
    class Broken:
        def online(self):
            raise OSError("unreadable")

    assert tool.note(Broken()) is None
