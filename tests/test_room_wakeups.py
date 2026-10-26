"""A room's pass that ends doesn't keep other rooms' passes going (room_wakeups)."""
import collections
import importlib.util
import logging
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# tui_gateway/hosted_room_driver.py at hermes-agent c73cbcc4 (main at 9b904dc
# has the same): HostedRoomRuntime's scheduling, from start to _run_room_once,
# verbatim. Its constructor, the room pass and the leases are stand-ins.
HERMES_DRIVER = '''
from __future__ import annotations

import threading
import time
import types
from contextlib import suppress
from dataclasses import dataclass


class DriverStateError(RuntimeError):
    pass


class LeaseHeldError(DriverStateError):
    pass


class StaleLeaseError(DriverStateError):
    pass


class RoomUnavailableError(RuntimeError):
    pass


state = types.SimpleNamespace(DriverStateError=DriverStateError, LeaseHeldError=LeaseHeldError,
                              StaleLeaseError=StaleLeaseError, RoomUnavailableError=RoomUnavailableError)


@dataclass(frozen=True)
class HostedRoomBinding:
    room_id: str
    gateway_id: str
    authority_epoch: int


class HostedRoomRuntime:
    def __init__(self, *, rooms, look, poll_interval_seconds=30.0, max_concurrent_rooms=4):
        self._rooms_provider = lambda bindings=tuple(rooms): bindings
        self.look = look
        self.poll_interval_seconds, self.active_poll_interval_seconds = poll_interval_seconds, 0.25
        self.max_concurrent_rooms = max_concurrent_rooms
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread = self._last_error = None
        self._room_threads = {}
        self._rooms_needing_reschedule = set()
        self._leases = {}
        self._blocked_rooms = set()
        self._status_lock = threading.Lock()
        self._room_schedule_cursor, self._cycles = 0, 0

    def start(self) -> None:
        """Start the bounded room-worker supervisor idempotently."""
        with self._status_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.set()
            self._thread = threading.Thread(
                target=self._worker_loop, name="hosted-room-driver-supervisor", daemon=True)
            self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> bool:
        """Request a bounded clean stop without interrupting accepted turns."""
        self._stop.set()
        self._wake.set()
        with self._status_lock:
            thread = self._thread
        if thread is None:
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        thread.join(max(0.0, deadline - time.monotonic()))
        with self._status_lock:
            room_threads = tuple(self._room_threads.values())
        for room_thread in room_threads:
            room_thread.join(max(0.0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in (thread, *room_threads))

    def wakeup(self) -> None:
        """Wake the worker after task admission or a room-state change."""
        with self._status_lock:
            # Rooms still owning a worker slot are revisited once that thread exits, closing
            # the terminal-publication/route-repair race without busy-looping idle rooms.
            self._rooms_needing_reschedule.update(self._room_threads)
        self._wake.set()

    def _worker_loop(self) -> None:
        try:
            while not self._stop.is_set():
                # Clear before work so a write racing the cycle forces a follow-up pass.
                self._wake.clear()
                try:
                    self._run_cycle()
                except Exception as exc:  # keep independent rooms serviceable
                    self._record_error(f"worker cycle failed: {exc}")
                with self._status_lock:
                    self._cycles += 1
                self._wake.wait(self.poll_interval_seconds)
        finally:
            while True:
                with self._status_lock:
                    room_threads = tuple(t for t in self._room_threads.values() if t.is_alive())
                if not room_threads:
                    break
                for room_thread in room_threads:
                    room_thread.join(self.active_poll_interval_seconds)
            self._release_idle_leases()

    def _run_cycle(self) -> None:
        with self._status_lock:
            supervisor = self._thread
        if threading.current_thread() is not supervisor:
            for binding in tuple(self._rooms_provider()):
                if self._stop.is_set():
                    return
                self._run_room_once(binding)
            return
        with self._status_lock:
            self._room_threads = {
                room_id: t for room_id, t in self._room_threads.items() if t.is_alive()}
            available = self.max_concurrent_rooms - len(self._room_threads)
            active_rooms = set(self._room_threads)
        if available <= 0:
            return
        bindings = tuple(self._rooms_provider())
        if not bindings:
            return
        start = self._room_schedule_cursor % len(bindings)
        self._room_schedule_cursor = (start + 1) % len(bindings)
        for binding in bindings[start:] + bindings[:start]:
            if self._stop.is_set() or available <= 0:
                return
            if binding.room_id in active_rooms:
                continue
            room_thread = threading.Thread(
                target=self._run_room_once, args=(binding,),
                name=f"hosted-room-{binding.room_id[:24]}", daemon=True)
            with self._status_lock:
                self._room_threads[binding.room_id] = room_thread
            active_rooms.add(binding.room_id)
            available -= 1
            room_thread.start()

    def _run_room_once(self, binding: HostedRoomBinding) -> None:
        try:
            self._process_room(binding)
        except state.LeaseHeldError:
            pass
        except Exception as exc:
            if isinstance(exc, (state.RoomUnavailableError, state.StaleLeaseError)):
                self._drop_lease(binding.room_id)
                self._set_blocked(binding.room_id, False)
            self._record_error(f"room {binding.room_id}: {exc}")
        finally:
            with self._status_lock:
                if self._room_threads.get(binding.room_id) is threading.current_thread():
                    self._room_threads.pop(binding.room_id, None)
                should_wake = binding.room_id in self._rooms_needing_reschedule
                self._rooms_needing_reschedule.discard(binding.room_id)
            if should_wake:
                self.wakeup()

    def _process_room(self, binding: HostedRoomBinding) -> None:
        self.look(binding)

    def _set_blocked(self, room_id: str, blocked: bool) -> None:
        with self._status_lock:
            (self._blocked_rooms.add if blocked else self._blocked_rooms.discard)(room_id)

    def _drop_lease(self, room_id: str) -> None:
        with self._status_lock:
            self._leases.pop(room_id, None)

    def _release_idle_leases(self) -> None:
        pass

    def _record_error(self, message: str) -> None:
        with self._status_lock:
            self._last_error = message
'''


def _module(name, source):
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(source, f"<hermes {name}>", "exec"), module.__dict__)
    return module


def _load(name="conduit_room_wakeups"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "room_wakeups.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def driver(monkeypatch):
    """Hermes' room driver, as far as its scheduling goes."""
    package = types.ModuleType("tui_gateway")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "tui_gateway", package)
    monkeypatch.delitem(sys.modules, "tui_gateway.hosted_room_driver", raising=False)
    yield _module("tui_gateway.hosted_room_driver", HERMES_DRIVER)
    sys.modules.pop("tui_gateway.hosted_room_driver", None)


@pytest.fixture
def guard(driver):
    room_wakeups = _load()
    assert room_wakeups.install()
    yield room_wakeups
    room_wakeups.uninstall()


class Rooms:
    """``count`` rooms on one driver, each pass a short look that's counted."""

    def __init__(self, driver, count, look=None):
        self.passes = collections.Counter()

        def counted(binding):
            self.passes[binding.room_id] += 1
            (look or (lambda binding: time.sleep(0.02)))(binding)

        bindings = [driver.HostedRoomBinding(room_id=f"room-{n}", gateway_id="gw", authority_epoch=1)
                    for n in range(count)]
        self.runtime = driver.HostedRoomRuntime(rooms=bindings, look=counted)

    @property
    def total(self):
        return sum(self.passes.values())

    def looking(self):
        with self.runtime._status_lock:
            return [t for t in self.runtime._room_threads.values() if t.is_alive()]

    def quiet(self, window=0.3, timeout=5.0):
        """Waits until no room is looked at for ``window`` seconds: False if
        they're still being looked at after ``timeout``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            seen = self.total
            time.sleep(window)
            if self.total == seen and not self.looking():
                return True
        return False

    def change_while_they_look(self):
        # A message to a room, then another while the passes it started run.
        self.runtime.wakeup()
        _until(self.looking)
        self.runtime.wakeup()


def _until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.001)


class Held:
    """Rooms A and B, whose passes end at once but A's first and B's second:
    those wait until they're let go."""

    def __init__(self, driver):
        self.passes = collections.Counter()
        self.looking = {("room-a", 1): threading.Event(), ("room-b", 2): threading.Event()}
        self.go_on = {key: threading.Event() for key in self.looking}

        def look(binding):
            self.passes[binding.room_id] += 1
            key = (binding.room_id, self.passes[binding.room_id])
            if key in self.looking:
                self.looking[key].set()
                self.go_on[key].wait(5)

        bindings = [driver.HostedRoomBinding(room_id=room_id, gateway_id="gw", authority_epoch=1)
                    for room_id in ("room-a", "room-b")]
        self.runtime = driver.HostedRoomRuntime(rooms=bindings, look=look)

    def passes_done(self, room_id, count):
        with self.runtime._status_lock:
            looked_at = room_id in self.runtime._room_threads
        return self.passes[room_id] == count and not looked_at

    def mark_a_then_end_its_pass(self):
        """A change while only A is looked at marks A, and B is looked at
        again. Then A's pass ends, and A is looked at again."""
        self.runtime.start()
        assert self.looking["room-a", 1].wait(5)
        _until(lambda: self.passes_done("room-b", 1))
        self.runtime.wakeup()
        assert self.looking["room-b", 2].wait(5)
        assert self.runtime._rooms_needing_reschedule == {"room-a"}
        self.go_on["room-a", 1].set()
        _until(lambda: self.passes_done("room-a", 2))

    def stop(self):
        for go_on in self.go_on.values():
            go_on.set()
        self.runtime.stop()


def test_in_hermes_alone_a_pass_that_ends_marks_the_rooms_still_being_looked_at(driver):
    held = Held(driver)
    try:
        held.mark_a_then_end_its_pass()

        # Nothing changed for B, but it's marked: its pass ends with another
        # wake, which marks whatever is being looked at then, and so on.
        assert held.runtime._rooms_needing_reschedule == {"room-b"}
        held.go_on["room-b", 2].set()
        _until(lambda: held.passes_done("room-a", 3) and held.passes_done("room-b", 3))
    finally:
        held.stop()


def test_a_pass_that_ends_leaves_the_rooms_still_being_looked_at_unmarked(driver, guard):
    held = Held(driver)
    try:
        held.mark_a_then_end_its_pass()

        assert held.runtime._rooms_needing_reschedule == set()
        held.go_on["room-b", 2].set()
        _until(lambda: held.passes_done("room-b", 2))
        time.sleep(0.3)
        assert held.passes == {"room-a": 2, "room-b": 2}
    finally:
        held.stop()


def test_rooms_go_quiet_once_a_change_has_been_looked_at(driver, guard):
    rooms = Rooms(driver, 4)
    rooms.runtime.start()
    try:
        assert rooms.quiet()
        first = rooms.total
        rooms.change_while_they_look()

        assert rooms.quiet()
        assert first + 4 <= rooms.total <= first + 32
        assert rooms.runtime._rooms_needing_reschedule == set()
    finally:
        rooms.runtime.stop()


def test_more_rooms_than_it_looks_at_at_once_go_quiet_too(driver, guard):
    # As on the rig: 8 rooms, twice the four the driver looks at at once, so
    # some wait for a pass to end before they're looked at.
    rooms = Rooms(driver, 8)
    rooms.runtime.start()
    try:
        assert rooms.quiet()
        first = rooms.total
        rooms.change_while_they_look()

        assert rooms.quiet()
        assert first + 4 <= rooms.total <= first + 64
        assert rooms.runtime._rooms_needing_reschedule == set()
    finally:
        rooms.runtime.stop()


def test_a_change_while_a_room_is_looked_at_still_brings_it_back(driver, guard):
    looking, go_on = threading.Event(), threading.Event()

    def look(binding):
        looking.set()
        go_on.wait(5)

    rooms = Rooms(driver, 1, look=look)
    rooms.runtime.start()
    try:
        assert looking.wait(5)
        rooms.runtime.wakeup()  # a reply published while the room's pass runs
        go_on.set()

        assert rooms.quiet()
        assert rooms.passes["room-0"] == 2
    finally:
        go_on.set()
        rooms.runtime.stop()


def test_a_change_marks_every_room_being_looked_at_as_hermes_does(driver, guard):
    started, go_on = threading.Semaphore(0), threading.Event()

    def look(binding):
        started.release()
        go_on.wait(5)

    rooms = Rooms(driver, 2, look=look)
    rooms.runtime.start()
    try:
        assert started.acquire(timeout=5) and started.acquire(timeout=5)
        rooms.runtime.wakeup()

        assert rooms.runtime._rooms_needing_reschedule == {"room-0", "room-1"}
    finally:
        go_on.set()
        rooms.runtime.stop()


def test_installing_twice_wraps_once_and_uninstall_puts_hermes_own_back(driver):
    own = driver.HostedRoomRuntime.wakeup
    room_wakeups = _load()

    assert room_wakeups.install() and room_wakeups.install()
    assert driver.HostedRoomRuntime.wakeup.__wrapped__ is own
    room_wakeups.uninstall()
    assert driver.HostedRoomRuntime.wakeup is own


def test_a_second_load_wraps_hermes_own_wakeup_not_the_first_guard(driver):
    # The plugin's gateway load and its dashboard routes each install one.
    own = driver.HostedRoomRuntime.wakeup
    first, second = _load("conduit_room_wakeups_first"), _load("conduit_room_wakeups_second")

    assert first.install() and second.install()
    assert driver.HostedRoomRuntime.wakeup.__wrapped__ is own
    newer = driver.HostedRoomRuntime.wakeup
    first.uninstall()
    assert driver.HostedRoomRuntime.wakeup is newer
    second.uninstall()
    assert driver.HostedRoomRuntime.wakeup is own


@pytest.mark.parametrize("method", ["wakeup", "_run_room_once"])
def test_a_hermes_driver_without_what_it_guards_is_left_alone(driver, method, caplog):
    delattr(driver.HostedRoomRuntime, method)
    before = dict(vars(driver.HostedRoomRuntime))

    with caplog.at_level(logging.INFO):
        assert _load().install() is False
    assert " ".join(caplog.messages).count(method) == 1
    assert dict(vars(driver.HostedRoomRuntime)) == before


def test_a_pass_that_doesnt_wake_the_driver_is_left_alone(driver, caplog):
    def _run_room_once(self, binding):
        self._process_room(binding)

    driver.HostedRoomRuntime._run_room_once = _run_room_once
    before = dict(vars(driver.HostedRoomRuntime))

    with caplog.at_level(logging.INFO):
        assert _load().install() is False
    assert "a _run_room_once that doesn't call wakeup" in caplog.text
    assert dict(vars(driver.HostedRoomRuntime)) == before


def test_a_driver_the_guard_cant_read_is_left_alone(driver, caplog):
    def _run_room_once(self, binding):
        self.wakeup()

    _run_room_once.__wrapped__ = _run_room_once  # a wrapper that never ends
    driver.HostedRoomRuntime._run_room_once = _run_room_once
    before = dict(vars(driver.HostedRoomRuntime))

    with caplog.at_level(logging.INFO):
        assert _load().install() is False
    assert caplog.messages == [
        "conduit_push: couldn't read Hermes' room driver, so its group rooms are scheduled as Hermes schedules them"]
    assert dict(vars(driver.HostedRoomRuntime)) == before


def test_no_hermes_rooms_at_all_is_not_an_error(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "tui_gateway.hosted_room_driver", None)

    with caplog.at_level(logging.WARNING):
        assert _load().install() is False
    assert caplog.text == ""


def test_the_dashboard_routes_guard_it_as_they_load(driver, monkeypatch):
    # A dashboard or `hermes serve` that hosts rooms itself imports these routes before it starts them.
    own = driver.HostedRoomRuntime.wakeup
    if "hermes_constants" not in sys.modules:
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: Path("/nonexistent-hermes-home")
        monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_room_wakeups",
                                                  ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        assert driver.HostedRoomRuntime.wakeup.__wrapped__ is own
    finally:
        module._room_wakeups_module.uninstall()
    assert driver.HostedRoomRuntime.wakeup is own
