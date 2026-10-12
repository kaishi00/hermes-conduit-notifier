"""Notifications carry the session's title, so a push says which chat it came from."""
import importlib.util
import itertools
import pathlib
import sys
import tempfile
import time
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants
if "tools.clarify_tool" not in sys.modules:
    # The clarify middleware formats answers with the runtime's strip_recommended.
    _tools = sys.modules.setdefault("tools", types.ModuleType("tools"))
    _tools.__path__ = getattr(_tools, "__path__", [])
    _clarify_tool = types.ModuleType("tools.clarify_tool")

    def _strip_recommended(text):
        # Same semantics as test_clarify_loop's stub, whichever module loads first.
        stripped = str(text).strip()
        suffix = "(Recommended)"
        if stripped.casefold().endswith(suffix.casefold()):
            return stripped[: -len(suffix)].strip()
        return stripped

    _clarify_tool.strip_recommended = _strip_recommended
    sys.modules["tools.clarify_tool"] = _clarify_tool
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


plugin = _load("conduit_push.__init__", ROOT / "__init__.py")
titles = _load("conduit_push.session_titles", ROOT / "session_titles.py")
loop = _load("conduit_push.clarify_loop", ROOT / "clarify_loop.py")


class FakeSessionDB:
    titles = {"s1": "Infra"}
    by_key = {"agent:main:telegram:group:-100:7": "Infra"}
    opened = []

    def __init__(self, db_path=None):
        self.closed = False
        FakeSessionDB.opened.append(self)

    def get_session_title(self, session_id):
        return self.titles.get(session_id)

    def list_sessions_rich(self, session_key=None, limit=20, order_by_last_active=False):
        title = self.by_key.get(session_key)
        return [{"title": title}] if title else []

    def close(self):
        self.closed = True


@pytest.fixture
def sent(monkeypatch):
    events = []
    state = types.ModuleType("hermes_state")
    state.SessionDB = FakeSessionDB
    monkeypatch.setitem(sys.modules, "hermes_state", state)
    monkeypatch.setattr(plugin, "enqueue", events.append)
    FakeSessionDB.opened.clear()
    titles._cache.clear()
    return events


def test_a_ready_push_is_titled_with_the_session(sent):
    plugin._post_llm_call(session_id="s1", turn_id="t1", assistant_response="Deployed.")
    assert [e.get("title") for e in sent] == ["Infra"]
    assert all(db.closed for db in FakeSessionDB.opened)


def test_an_approval_finds_its_session_by_gateway_key(sent):
    plugin._pre_approval_request(session_key="agent:main:telegram:group:-100:7", description="rm -rf build", turn_id="t2")
    assert sent[0]["title"] == "Approval needed · Infra"


def test_an_untitled_session_keeps_the_relay_copy(sent):
    plugin._post_llm_call(session_id="unknown", turn_id="t3", assistant_response="Hi.")
    assert "title" not in sent[0]


def test_a_clarify_question_is_titled_on_the_middleware_path(sent, monkeypatch):
    queued = []
    monkeypatch.setattr(loop.client, "load_state", lambda: {"credential": "x"})
    monkeypatch.setattr(loop.client, "enqueue", lambda event: queued.append(event) or True)
    monkeypatch.setattr(loop.client, "poll_decision", lambda request_id: {"status": "answered", "answer": "Red"})
    monkeypatch.setattr(loop.client, "cancel_decision", lambda request_id: True)
    monkeypatch.setattr(loop, "POLL_INTERVAL_SECONDS", 0.01)
    loop.middleware(tool_name="clarify", args={"question": "Which color?", "choices": ["Red", "Blue"]},
                    next_call=lambda args: time.sleep(5), session_id="s1")
    assert queued[0]["type"] == "input.needed"
    assert queued[0]["title"] == "Input needed · Infra"


def test_events_in_one_turn_share_one_session_store_read(sent):
    plugin._post_llm_call(session_id="s1", turn_id="t5", assistant_response="Deployed.")
    plugin._post_llm_call(session_id="s1", turn_id="t6", assistant_response="Again.")
    assert [e["title"] for e in sent] == ["Infra", "Infra"]
    assert len(FakeSessionDB.opened) == 1


def test_a_failing_close_does_not_escape(sent, monkeypatch):
    def broken_close(self):
        raise RuntimeError("already closed")

    monkeypatch.setattr(FakeSessionDB, "close", broken_close)
    plugin._post_llm_call(session_id="s1", turn_id="t7", assistant_response="Deployed.")
    assert sent[0]["title"] == "Infra"


def test_no_session_store_keeps_the_relay_copy(monkeypatch):
    titles._cache.clear()
    events = []
    monkeypatch.setitem(sys.modules, "hermes_state", None)
    monkeypatch.setattr(plugin, "enqueue", events.append)
    plugin._post_llm_call(session_id="s1", turn_id="t4", assistant_response="Hi.")
    assert "title" not in events[0]


def test_a_failed_read_is_retried_on_the_next_event(sent, monkeypatch):
    def locked(self, session_id):
        raise RuntimeError("database is locked")

    with monkeypatch.context() as patch:
        patch.setattr(FakeSessionDB, "get_session_title", locked)
        plugin._post_llm_call(session_id="s1", turn_id="t8", assistant_response="Deployed.")
    plugin._post_llm_call(session_id="s1", turn_id="t9", assistant_response="Again.")
    assert "title" not in sent[0]
    assert sent[1]["title"] == "Infra"


def test_a_session_titled_after_its_first_reply_shows_the_title_next_time(sent, monkeypatch):
    plugin._post_llm_call(session_id="s2", turn_id="t10", assistant_response="Hi.")
    monkeypatch.setitem(FakeSessionDB.titles, "s2", "Groceries")
    plugin._post_llm_call(session_id="s2", turn_id="t11", assistant_response="Again.")
    assert "title" not in sent[0]
    assert sent[1]["title"] == "Groceries"


def test_without_an_id_lookup_the_gateway_key_still_resolves(sent, monkeypatch):
    monkeypatch.delattr(FakeSessionDB, "get_session_title")
    plugin._pre_approval_request(session_key="agent:main:telegram:group:-100:7", description="rm -rf build", turn_id="t12")
    assert sent[0]["title"] == "Approval needed · Infra"


def test_profiles_do_not_share_cached_titles(sent, monkeypatch):
    homes = itertools.cycle([pathlib.Path("/profiles/a"), pathlib.Path("/profiles/b")])
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: next(homes))

    class PerProfileDB(FakeSessionDB):
        def get_session_title(self, session_id):
            return "Work" if str(self.home).startswith("/profiles/a") else "Home"

        def __init__(self, db_path=None):
            super().__init__(db_path)
            self.home = db_path

    monkeypatch.setattr(sys.modules["hermes_state"], "SessionDB", PerProfileDB)
    assert titles.title("s1") == "Work"
    assert titles.title("s1") == "Home"
