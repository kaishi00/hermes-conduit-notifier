"""Pulse's suggestions (pulse_proposals; designs/pulse-look-first.md,
proactivity.md 7.6, items 6 and 7): a Pulse turn's calendar add is checked
as the write checks it and kept as a suggestion, never made. Its card adds
it with one tap, once, always asking on the phone, and follows the change
there; Undo asks the phone to delete it."""

import itertools
import json
import types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from test_pulse_tools import block, follow_up_turn, tools
from test_pulse import ROOT, SID, _load, cli, control, pulse, store
from test_pulse_followup import CHAT, live  # noqa: F401
from test_phone import Phone, _batch, _event, _sharing, phone, tool

proposals = _load("conduit_push.pulse_proposals", ROOT / "pulse_proposals.py")

TOMORROW = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
LONDON = ZoneInfo("Europe/London")  # the phone's (test_phone)


def add_event(**args):
    return {"action": "add_event", "title": "Dinner at Nopa", "start": TOMORROW.isoformat(),
            "end": (TOMORROW + timedelta(hours=2)).isoformat(), **args}


LINE = (f"Add to Personal: Dinner at Nopa, {TOMORROW.astimezone(LONDON):%a %d %b, %H:%M} "
        f"to {(TOMORROW + timedelta(hours=2)).astimezone(LONDON):%H:%M}")


@pytest.fixture
def handset(live, monkeypatch):
    """The phone shares its calendar and reminders with this host, as of now."""
    monkeypatch.setattr(tool, "get_hermes_home", lambda: live.home)
    sent = []
    monkeypatch.setattr(tool.client, "enqueue", lambda event: sent.append(event) or True)
    phone_store = phone.PhoneStore(live.home)
    device = Phone()
    device.accept(phone_store.register_key(device.key_request()))
    calls = (f"call-{n:08d}" for n in itertools.count(1))

    def share(events=None, **sharing):
        """A sync: ``events`` the events it sends (a diff), else the usual full set."""
        # No Focus: one that holds calls holds Pulse's pushes too (pulse_hours.busy).
        batch = _batch(f"batch-{next(calls)[5:]}", sent_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       sharing=_sharing(**{"focus": False, **sharing}),
                       **({"events": {"full": False, "upsert": events, "delete": []}} if events else {}))
        phone_store.ingest(device.seal(batch))

    def waiting():
        """The changes waiting for the phone, as it gets them."""
        return device.changes(phone_store, call_id=next(calls))

    def answer(change_id, outcome, **result):
        """The phone takes its waiting changes, then says how one went."""
        waiting()
        device.changes(phone_store, [{"id": change_id, "outcome": outcome, **result}], call_id=next(calls))

    share()
    return types.SimpleNamespace(store=phone_store, sent=sent, share=share, waiting=waiting, answer=answer)


def suggest(live, args=None, turn="t1"):
    return pulse.pre_tool_call(SID, turn, "conduit_calendar", add_event() if args is None else args)


def deliver(live):
    """The Pulse turn's message is in."""
    [attempt] = live.store.in_flight()
    live.db.message("user", attempt["text"], display_kind="hidden")
    live.db.message("assistant", "The deploy finished. I suggested dinner at Nopa tomorrow.")
    pulse.tick()
    return attempt


def card_of(live, proposal_id):
    return live.store.card(live.store.proposal(proposal_id)["card_id"])


# Kept, not made ----------------------------------------------------------------------

def test_a_pulse_turns_add_is_kept_as_a_suggestion_and_never_made(live, handset):
    follow_up_turn(live)
    args = add_event()
    assert suggest(live, args) == block(proposals.PROPOSED)
    [kept] = live.store.proposals()
    assert (kept["kind"], kept["state"], kept["line"], kept["profile"]) == ("add_event", "waiting", LINE, "default")
    # The calendar the card names, not whatever the phone's default is by the time it's added.
    assert kept["fields"]["calendar_id"] == "c_personal" and kept["fields"]["title"] == "Dinner at Nopa"
    assert kept["expires_at"] == TOMORROW.timestamp()
    assert handset.store.changes(mark_reported=False) == [] and handset.sent == []
    # What's kept is a copy: a hook that changes the call afterwards changes nothing.
    args["title"] = "Wire the money"
    assert live.store.proposal(kept["id"])["fields"]["title"] == "Dinner at Nopa"
    assert pulse.looks_today(live.store) == {}  # not a look-up


def test_a_reminder_suggestion_names_its_list_and_runs_out_at_the_end_of_its_day(live, handset):
    follow_up_turn(live)
    due = (TOMORROW + timedelta(days=1)).date()
    assert suggest(live, {"action": "add_reminder", "title": "Renew passport", "due": due.isoformat()}) == \
        block(proposals.PROPOSED)
    [kept] = live.store.proposals()
    assert kept["line"] == f"Add to Reminders: Renew passport, due {due:%a %d %b}"
    assert kept["fields"]["list_id"] == "l_reminders"
    end_of_day = datetime.combine(due + timedelta(days=1), datetime.min.time(), LONDON)
    assert kept["expires_at"] == end_of_day.timestamp()


def test_the_cards_times_are_the_phones_whatever_offset_they_came_in(live, handset):
    follow_up_turn(live)
    pacific = timezone(timedelta(hours=-7))
    args = add_event(start=TOMORROW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     end=(TOMORROW + timedelta(hours=2)).astimezone(pacific).isoformat())
    assert suggest(live, args) == block(proposals.PROPOSED)
    [kept] = live.store.proposals()
    assert kept["line"] == LINE and kept["expires_at"] == TOMORROW.timestamp()


@pytest.mark.parametrize("args, code", [
    ({"start": None}, "malformed"),  # what the write would refuse, the suggestion does
    ({"calendar": "UK Holidays"}, "read_only"),
    ({"calendar": "Nope"}, "unknown_calendar"),
])
def test_a_change_the_write_would_refuse_gets_the_tools_own_error(live, handset, args, code):
    follow_up_turn(live)
    answer = suggest(live, {key: value for key, value in add_event(**args).items() if value is not None})
    assert answer["action"] == "block" and json.loads(answer["message"])["error"] == code
    assert live.store.proposals() == []


def test_without_a_phone_sharing_the_calendar_theres_nothing_to_suggest(live, monkeypatch):
    monkeypatch.setattr(tool, "get_hermes_home", lambda: live.home)
    follow_up_turn(live)
    assert json.loads(suggest(live)["message"])["error"] == "nothing_shared"


def test_a_time_thats_passed_is_not_suggested(live, handset):
    follow_up_turn(live)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert suggest(live, add_event(start=past.isoformat(), end=(past + timedelta(minutes=30)).isoformat())) == \
        block(proposals.PAST)
    assert live.store.proposals() == []


def test_two_suggestions_a_turn_and_five_a_day(live, handset):
    assert tools.MAX_PROPOSALS == proposals.MAX_PER_TURN  # the note says how many
    follow_up_turn(live)
    assert suggest(live) == suggest(live, add_event(title="Book a table")) == block(proposals.PROPOSED)
    assert suggest(live, add_event(title="Flowers")) == block(proposals.TURN_CAP)
    assert len(live.store.proposals()) == 2
    now = datetime.now(timezone.utc).timestamp()
    for n in range(3):
        live.store.add_proposal({"attempt_id": f"earlier:{n}", "kind": "add_event", "fields": {}, "device": "d",
                                 "profile": "", "line": "x", "created_at": now, "expires_at": now + 60},
                                per_turn=2, per_day=5, day_from=proposals.day_from(now))
    # Five today: a later turn suggests nothing more.
    assert proposals.propose(live.store, {"attempt_id": "later:1"}, add_event(), profile="default",
                             now=now) == proposals.DAILY_CAP


def test_suggestions_never_run_through_the_tool_search_bridge(live, handset):
    follow_up_turn(live)
    bridged = {"calls": [{"name": "conduit_calendar", "arguments": add_event()}]}
    assert pulse.pre_tool_call(SID, "t1", "tool_call", bridged) == \
        block(tools.BRIDGE_BLOCK.format(tool="conduit_calendar"))
    assert live.store.proposals() == []


# Cards ------------------------------------------------------------------------------------

def test_its_card_comes_once_the_turns_message_is_in(live, handset):
    follow_up_turn(live)
    assert pulse.pre_tool_call(SID, "t1", "conduit_calendar", {"action": "events"}) is None
    suggest(live)
    [kept] = live.store.proposals()
    pulse.tick()
    assert live.store.proposal(kept["id"])["card_id"] is None  # the turn is still on its way
    deliver(live)
    card = card_of(live, kept["id"])
    assert (card["kind"], card["level"], card["title"], card["why"], card["chat_id"]) == (
        "proposal", "screen", LINE, proposals.WHY["followup"], SID)
    assert store.looked_labels(card["looked"]) == ["calendar"]  # Why?: what its turn looked at first
    pulse.tick()
    assert [row["id"] for row in live.store.cards() if row["kind"] == "proposal"] == [card["id"]]  # once
    # Only an app that asks for suggestion cards gets them, or counts them.
    plain = control.feed(live.store, live.home)
    assert all(entry["kind"] != "proposal" for entry in plain["cards"])
    asked = control.feed(live.store, live.home, proposals=True)
    [entry] = [entry for entry in asked["cards"] if entry["kind"] == "proposal"]
    assert entry["proposal"] == {"id": kept["id"], "kind": "add_event", "line": LINE, "state": "waiting",
                                 "expires_at": kept["expires_at"]}
    assert asked["unseen"] == plain["unseen"] + 1


def test_a_turn_that_ended_without_its_message_leaves_no_card(live, handset):
    follow_up_turn(live)
    suggest(live)
    [attempt] = live.store.in_flight()
    live.store.update_attempt(attempt["attempt_id"], state=store.FAILED)
    pulse.tick()
    [kept] = live.store.proposals()
    assert kept["state"] == "expired" and kept["card_id"] is None


def test_a_suggestion_past_its_time_expires_and_cant_be_added(live, handset, monkeypatch):
    follow_up_turn(live)
    suggest(live)
    deliver(live)
    [kept] = live.store.proposals()
    later = kept["expires_at"] + 1
    assert proposals.entry(live.store.proposal(kept["id"]), live.home, later)["state"] == "expired"
    with pytest.raises(control.Refused, match="expired"):
        control.add_proposal(live.store, kept["id"], live.home, now=later)
    assert live.store.proposal(kept["id"])["state"] == "expired"
    assert handset.store.changes(mark_reported=False) == []


# One tap --------------------------------------------------------------------------------

def added(live, handset):
    follow_up_turn(live)
    suggest(live)
    deliver(live)
    [kept] = live.store.proposals()
    return kept, control.add_proposal(live.store, kept["id"], live.home)


def test_add_asks_the_phone_once_from_the_kept_fields_and_always_asks_there(live, handset):
    handset.share(auto_add=True)  # the user lets Hermes add without asking: not for a suggestion
    kept, entry = added(live, handset)
    [change] = handset.waiting()
    assert (change["kind"], change["auto"], change["reason"], change["session_title"]) == (
        "add_event", False, proposals.REASON, "Pulse")
    assert change["fields"] == kept["fields"]  # exactly what the card said
    assert entry == {"id": kept["id"], "kind": "add_event", "line": LINE, "state": "adding",
                     "expires_at": kept["expires_at"], "change_id": change["id"]}
    assert len(handset.sent) == 1  # the phone is nudged
    # A second tap, a retry or another device: already added, and nothing more is asked.
    for again in (control.add_proposal, control.dismiss_proposal):
        with pytest.raises(control.Refused, match="already_added"):
            again(live.store, kept["id"], live.home)
    assert len(handset.store.changes(mark_reported=False)) == 1


def test_the_card_follows_the_change_on_the_phone_and_undo_deletes_it(live, handset):
    kept, entry = added(live, handset)
    with pytest.raises(control.Refused, match="not_added"):
        control.undo_proposal(live.store, kept["id"], live.home)  # nothing on the phone yet
    handset.answer(entry["change_id"], "applied", item_id="e_nopa")
    assert proposals.entry(live.store.proposal(kept["id"]), live.home, 0)["state"] == "added"
    # Pulse reads the outcome without taking it from what the agent sees in `pending`.
    assert handset.store.unreported_outcomes()[0]["id"] == entry["change_id"]
    with pytest.raises(control.Refused, match="unknown_event"):
        control.undo_proposal(live.store, kept["id"], live.home)  # the phone hasn't sent the event yet
    handset.share(events=[_event("e_nopa", title="Dinner at Nopa", start=TOMORROW.isoformat(),
                                 end=(TOMORROW + timedelta(hours=2)).isoformat())])
    undone = control.undo_proposal(live.store, kept["id"], live.home)
    [delete] = handset.waiting()
    assert (delete["id"], delete["kind"], delete["target"]["event_id"]) == (undone["undo_id"], "delete_event", "e_nopa")
    assert delete["auto"] is False and delete["reason"] == proposals.UNDO_REASON
    assert undone["state"] == "removing"
    with pytest.raises(control.Refused, match="already_undone"):
        control.undo_proposal(live.store, kept["id"], live.home)
    handset.answer(undone["undo_id"], "applied")
    assert proposals.entry(live.store.proposal(kept["id"]), live.home, 0)["state"] == "removed"


@pytest.mark.parametrize("outcome, state, note", [
    ("edited", "changed", "Moved to 20:00"), ("dismissed", "not_added", None), ("failed", "failed", "No access"),
])
def test_what_the_phone_did_shows_on_the_card(live, handset, outcome, state, note):
    kept, entry = added(live, handset)
    handset.answer(entry["change_id"], outcome, **({"note": note} if note else {}),
                   **({"item_id": "e_nopa"} if outcome == "edited" else {}))
    shown = proposals.entry(live.store.proposal(kept["id"]), live.home, 0)
    assert shown["state"] == state and shown.get("note") == note


def test_a_change_the_phone_store_refuses_puts_the_suggestion_back(live, handset, monkeypatch):
    follow_up_turn(live)
    suggest(live)
    deliver(live)
    [kept] = live.store.proposals()
    handset.share(calendar="off", reminders=False)  # the phone stopped sharing its calendar
    with pytest.raises(control.Refused) as refused:
        control.add_proposal(live.store, kept["id"], live.home)
    assert refused.value.code == "not_shared" and refused.value.data["note"]
    assert live.store.proposal(kept["id"])["state"] == "waiting"
    handset.share()
    assert control.add_proposal(live.store, kept["id"], live.home)["state"] == "adding"


def stopped(*args, **kwargs):
    raise RuntimeError("the process stopped")


def test_an_add_stopped_before_the_phone_store_had_it_is_asked_for_again_as_the_same_change(live, handset,
                                                                                            monkeypatch):
    follow_up_turn(live)
    suggest(live)
    deliver(live)
    [kept] = live.store.proposals()
    now = datetime.now(timezone.utc).timestamp()
    with monkeypatch.context() as patch:
        patch.setattr(proposals.PhoneStore, "add_change", stopped)
        with pytest.raises(RuntimeError):
            control.add_proposal(live.store, kept["id"], live.home, now=now)
    claimed = live.store.proposal(kept["id"])["change_id"]
    assert claimed and proposals.entry(live.store.proposal(kept["id"]), live.home, now)["state"] == "adding"
    with pytest.raises(control.Refused, match="already_added"):
        control.add_proposal(live.store, kept["id"], live.home, now=now + 1)  # it may still be on its way
    later = now + proposals.STALE_S + 1
    assert proposals.entry(live.store.proposal(kept["id"]), live.home, later)["state"] == "waiting"
    assert control.add_proposal(live.store, kept["id"], live.home, now=later)["change_id"] == claimed
    assert [change["id"] for change in handset.store.changes(mark_reported=False)] == [claimed]


def test_an_add_stopped_after_the_phone_store_had_it_is_never_asked_for_twice(live, handset, monkeypatch):
    follow_up_turn(live)
    suggest(live)
    deliver(live)
    [kept] = live.store.proposals()
    now = datetime.now(timezone.utc).timestamp()
    real = live.store.swap_proposal
    with monkeypatch.context() as patch:
        patch.setattr(live.store, "swap_proposal",
                      lambda proposal_id, was, **values: stopped() if "asked_at" in values else
                      real(proposal_id, was, **values))
        with pytest.raises(RuntimeError):
            control.add_proposal(live.store, kept["id"], live.home, now=now)
    later = now + proposals.STALE_S + 1
    assert proposals.entry(live.store.proposal(kept["id"]), live.home, later)["state"] == "adding"
    with pytest.raises(control.Refused, match="already_added"):
        control.add_proposal(live.store, kept["id"], live.home, now=later)
    assert len(handset.store.changes(mark_reported=False)) == 1


def on_the_phone(live, handset):
    """An added suggestion whose event the phone has sent: Undo can delete it."""
    kept, entry = added(live, handset)
    handset.answer(entry["change_id"], "applied", item_id="e_nopa")
    handset.share(events=[_event("e_nopa", title="Dinner at Nopa", start=TOMORROW.isoformat(),
                                 end=(TOMORROW + timedelta(hours=2)).isoformat())])
    return kept


def deletes(handset):
    return [change["id"] for change in handset.store.changes(mark_reported=False) if change["kind"] == "delete_event"]


def test_two_undos_at_once_ask_the_phone_for_one_delete(live, handset, monkeypatch):
    kept = on_the_phone(live, handset)
    seen = live.store.proposal(kept["id"])  # what a second tap read before the first asked
    undone = control.undo_proposal(live.store, kept["id"], live.home)
    with monkeypatch.context() as patch:
        patch.setattr(live.store, "proposal", lambda proposal_id: dict(seen))
        with pytest.raises(control.Refused, match="already_undone"):
            control.undo_proposal(live.store, kept["id"], live.home)
    assert deletes(handset) == [undone["undo_id"]]


def test_an_undo_stopped_before_the_phone_store_had_it_is_asked_for_again_as_the_same_delete(live, handset,
                                                                                             monkeypatch):
    kept = on_the_phone(live, handset)
    now = datetime.now(timezone.utc).timestamp()
    with monkeypatch.context() as patch:
        patch.setattr(proposals.PhoneStore, "add_change", stopped)
        with pytest.raises(RuntimeError):
            control.undo_proposal(live.store, kept["id"], live.home, now=now)
    claimed = live.store.proposal(kept["id"])["undo_id"]
    assert claimed and proposals.entry(live.store.proposal(kept["id"]), live.home, now)["state"] == "removing"
    with pytest.raises(control.Refused, match="already_undone"):
        control.undo_proposal(live.store, kept["id"], live.home, now=now + 1)  # it may still be on its way
    later = now + proposals.STALE_S + 1
    assert proposals.entry(live.store.proposal(kept["id"]), live.home, later)["state"] == "added"
    assert control.undo_proposal(live.store, kept["id"], live.home, now=later)["undo_id"] == claimed
    assert deletes(handset) == [claimed]


def test_an_undo_the_phone_store_refuses_can_be_tapped_again_at_once(live, handset):
    kept = on_the_phone(live, handset)
    handset.share(calendar="off", reminders=False)  # the phone stopped sharing its calendar
    with pytest.raises(control.Refused) as refused:
        control.undo_proposal(live.store, kept["id"], live.home)
    assert refused.value.code == "not_shared" and deletes(handset) == []
    handset.share(events=[_event("e_nopa", title="Dinner at Nopa", start=TOMORROW.isoformat(),
                                 end=(TOMORROW + timedelta(hours=2)).isoformat())])
    assert control.undo_proposal(live.store, kept["id"], live.home)["state"] == "removing"


def test_not_now_takes_the_card_away_and_it_cant_be_added_after(live, handset):
    follow_up_turn(live)
    suggest(live)
    deliver(live)
    [kept] = live.store.proposals()
    assert control.dismiss_proposal(live.store, kept["id"], live.home)["state"] == "not_added"
    assert card_of(live, kept["id"])["dismissed_at"] is not None
    with pytest.raises(control.Refused, match="dismissed"):
        control.add_proposal(live.store, kept["id"], live.home)
    with pytest.raises(control.Refused, match="not_found"):
        control.add_proposal(live.store, kept["id"] + 1, live.home)
    assert handset.store.changes(mark_reported=False) == []


def test_nothing_is_added_or_put_away_without_its_card(live, handset):
    # Kept but not yet on the screen (its turn is on its way): Add and Not now can't take it.
    follow_up_turn(live)
    suggest(live)
    [kept] = live.store.proposals()
    for tap in (control.add_proposal, control.dismiss_proposal):
        with pytest.raises(control.Refused, match="not_found"):
            tap(live.store, kept["id"], live.home)
    assert live.store.proposal(kept["id"])["state"] == "waiting"
    assert handset.store.changes(mark_reported=False) == []


# The write from the Pulse chat ---------------------------------------------------------------

def test_the_phone_tools_refuse_a_change_from_the_pulse_chat_while_a_pulse_turn_is_in_flight(live, handset):
    # So a call that got past Pulse's hook, or was changed after it, still can't write.
    handset.share(alarms=True)
    follow_up_turn(live)
    args = add_event()
    assert json.loads(tool.handle_calendar(args, session_id=SID))["error"] == "pulse_turn"
    timer = {"action": "set_timer", "seconds": 60}
    assert json.loads(tool.handle_phone(timer, session_id=SID))["error"] == "pulse_turn"
    assert json.loads(tool.handle_calendar(args, session_id=CHAT))["ok"]  # another chat is the user's own
    assert json.loads(tool.handle_calendar(args))["error"] == "pulse_turn"  # no chat named: it may be Pulse's
    deliver(live)
    assert json.loads(tool.handle_calendar(args, session_id=SID))["ok"]  # the message is in: the chat's theirs
    assert json.loads(tool.handle_phone(timer, session_id=SID))["ok"]
    assert json.loads(tool.handle_calendar(args))["ok"]


# The routes and the command -------------------------------------------------------------------

def test_the_suggestion_routes(live, handset, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    import importlib.util
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_proposals", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    modules = {"pulse_control": control, "pulse": pulse}
    monkeypatch.setattr(api, "_plugin_package_module", lambda name: modules[name])
    monkeypatch.setattr(api, "_calls_home", lambda: live.home)
    app = fastapi.FastAPI()
    app.include_router(api.router, prefix="/api/plugins/conduit_push")
    http = TestClient(app)
    base = "/api/plugins/conduit_push/pulse"
    follow_up_turn(live)
    suggest(live)
    deliver(live)
    [kept] = live.store.proposals()
    feed = http.get(f"{base}/feed", params={"proposals": "1"}).json()
    assert [entry["proposal"]["id"] for entry in feed["cards"] if entry["kind"] == "proposal"] == [kept["id"]]
    assert all(entry["kind"] != "proposal" for entry in http.get(f"{base}/feed").json()["cards"])
    tapped = http.post(f"{base}/proposals/{kept['id']}/add")
    assert tapped.status_code == 200 and tapped.headers["cache-control"] == "no-store"
    assert tapped.json()["proposal"]["state"] == "adding" and tapped.json()["proposal"]["change_id"]
    again = http.post(f"{base}/proposals/{kept['id']}/add")
    assert again.status_code == 409 and again.json()["detail"] == {"code": "already_added"}
    assert http.post(f"{base}/proposals/{kept['id']}/undo").json()["detail"] == {"code": "not_added"}
    assert http.post(f"{base}/proposals/nope/dismiss").status_code == 422


def test_pulse_look_lists_the_suggestions(live, handset, capsys):
    follow_up_turn(live)
    suggest(live)
    assert cli._pulse_look(live.store, live.home, "") == 0
    out = capsys.readouterr().out
    assert "Suggestions: 1 of 5 today (UTC)" in out and f"{LINE}: waiting" in out
