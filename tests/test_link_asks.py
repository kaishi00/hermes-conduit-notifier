"""Asks between linked agents (designs/cross-owner-agents, N9): one linked Hermes asks another whether its owner is
free on a date, or a question. The asked side's rules answer it, or its owner does from their phone; no agent there
reads it.
"""
import json

import pytest

from test_phone import NOW, Phone, _batch, _sharing, phone
from test_present import PHONE, PresentStore
from test_room_rules import ROOT, _load, rules  # noqa: F401 — loads the plugin's package

room_links = _load("conduit_push.room_links", ROOT / "room_links.py")
link_asks = _load("conduit_push.link_asks", ROOT / "link_asks.py")

MONA, LAIFU = "lnk_mona_0001", "lnk_laifu_001"


class Clock:
    def __init__(self):
        self.now = float(NOW)

    def __call__(self):
        return self.now


class Pair:
    """Two linked installations, each with its asks, its phone's calendar and its present store; what one sends
    lands in the other's handler."""

    def __init__(self, tmp_path, clock):
        self.clock = clock
        self.sent = []
        self.down = False
        self.homes = {"asker": tmp_path / "asker", "asked": tmp_path / "asked"}
        for home in self.homes.values():
            home.mkdir()
        # The asker's Link to the asked side is LAIFU; the asked side's Link back is MONA.
        self.asker = self._asks("asker", LAIFU, "Laifu", "asked", MONA)
        self.asked = self._asks("asked", MONA, "Mona", "asker", LAIFU)

    def _asks(self, side, link_id, name, other, back, module=None):
        def send(link, kind, body, message_id=None):
            if self.down:
                raise room_links.LinkError("unreachable", "the mailbox is down", 503)
            if link != link_id:
                raise room_links.LinkError("not_linked", "no such Link", 404)
            self.sent.append((side, kind, body))
            target = getattr(self, other)
            target.handle(room_links.Received(link_id=back, kind=kind, message_id=message_id or "m", at=NOW,
                                              body=json.loads(json.dumps(body)), root=self.homes[other]))
            return message_id

        def link(wanted):
            return {"id": link_id, "name": name, "status": "linked"} if wanted == link_id else None

        return (module or link_asks).Asks(self.homes[side], send=send, link=link,
                              links=lambda: [{"id": link_id, "name": name, "status": "linked"}], clock=self.clock,
                              present=lambda profile: PresentStore(self.homes[side], clock=self.clock),
                              phone=lambda profile: phone.PhoneStore(self.homes[side], clock=self.clock))

    def share_calendar(self, **sharing):
        store = phone.PhoneStore(self.homes["asked"], clock=self.clock)
        handset = Phone()
        handset.accept(store.register_key(handset.key_request()))
        store.ingest(handset.seal(_batch(sharing=_sharing(**sharing))))

    def phone_shows_link_asks(self, features=("link-asks",)):
        return self.present().exchange({"installation_id": PHONE, "features": list(features), "results": []})

    def present(self):
        return PresentStore(self.homes["asked"], clock=self.clock)

    def ask(self, asked, session="s1"):
        ask = self.asker.ask(LAIFU, asked, profile="default", session_id=session, agent="Furina")
        self.asker.flush()
        self.asked.flush()
        return self.asker.sent(ask["id"])

    def settle(self):
        """The asked side's loop, then the asker's."""
        self.asked.pass_once()
        self.asker.pass_once()


@pytest.fixture
def pair(tmp_path):
    return Pair(tmp_path, Clock())


def rules_for(pair, **changes):
    pair.asked.set_rules(MONA, changes)


def test_a_link_without_rules_asks_its_owner_for_everything(pair):
    assert pair.asked.rules()["links"][0]["rules"] == {"free_on": "ask", "questions": "ask", "profile": "default"}
    pair.share_calendar()
    pair.phone_shows_link_asks()
    ask = pair.ask({"free_on": {"date": "2026-10-16"}})
    assert ask["status"] == "sent"
    (held,) = pair.asked.held()
    assert held["from"] == "Mona" and held["asked"] == {"free_on": {"date": "2026-10-16"}}
    prompt = [p for p in pair.phone_shows_link_asks()["prompts"] if p.get("link")][0]
    assert prompt["link"] == {"id": MONA, "name": "Mona", "share": True}
    assert prompt["preview"] == "Busy on 2026-10-16."


def test_yes_or_no_answers_without_asking_and_only_free_or_busy(pair):
    pair.share_calendar()
    rules_for(pair, free_on="yes_no")
    busy = pair.ask({"free_on": {"date": "2026-10-16", "from": "15:30", "to": "17:00"}})
    assert busy["status"] == "answered" and busy["free"] is False and "busy" not in busy
    assert busy["answer"] == "Busy on 2026-10-16, 15:30-17:00."
    free = pair.ask({"free_on": {"date": "2026-10-16", "from": "17:00", "to": "18:00"}})
    assert free["free"] is True and free["by"] == "rule"
    text = json.dumps(pair.sent)
    for private in ("Dentist", "High St", "Personal", "Sam"):
        assert private not in text
    assert not pair.asked.held()


def test_times_also_says_when_but_never_what(pair):
    pair.share_calendar()
    rules_for(pair, free_on="times")
    answer = pair.ask({"free_on": {"date": "2026-10-14"}})
    assert answer["busy"] == [{"start": "09:30", "end": "11:00"}] and answer["free"] is False
    assert "Standup" not in json.dumps(pair.sent) and "Design review" not in json.dumps(pair.sent)


def test_off_refuses(pair):
    rules_for(pair, free_on="off", questions="off")
    assert pair.ask({"free_on": {"date": "2026-10-16"}})["status"] == "not_allowed"
    assert pair.ask({"question": "Is Mona coming to the zoo?"})["status"] == "not_allowed"


def test_the_owner_shares_says_instead_or_declines_from_the_phone(pair):
    pair.share_calendar()
    pair.phone_shows_link_asks()
    shared = pair.ask({"free_on": {"date": "2026-10-16"}})
    instead = pair.ask({"question": "Is Mona coming to the zoo?"})
    declined = pair.ask({"question": "What's Mona's address?"})
    prompts = {p["title"]: p for p in pair.phone_shows_link_asks()["prompts"]}
    results = [{"id": prompts["Free on 2026-10-16?"]["id"], "outcome": "confirmed"},
               {"id": prompts["Is Mona coming to the zoo?"]["id"], "outcome": "edit", "edit_text": "Yes, at 10."},
               {"id": prompts["What's Mona's address?"]["id"], "outcome": "declined"}]
    pair.present().exchange({"installation_id": PHONE, "features": ["link-asks"], "results": results})
    pair.settle()
    assert pair.asker.sent(shared["id"])["answer"] == "Busy on 2026-10-16."
    assert pair.asker.sent(shared["id"])["by"] == "owner"
    assert pair.asker.sent(instead["id"])["answer"] == "Yes, at 10."
    assert pair.asker.sent(declined["id"])["status"] == "declined"
    assert not pair.asked.held()


def test_a_question_has_nothing_to_share(pair):
    pair.phone_shows_link_asks()
    pair.ask({"question": "Is Mona coming to the zoo?"})
    (prompt,) = [p for p in pair.phone_shows_link_asks()["prompts"] if p.get("link")]
    assert prompt["link"]["share"] is False
    pair.present().exchange({"installation_id": PHONE, "features": ["link-asks"],
                             "results": [{"id": prompt["id"], "outcome": "confirmed"}]})
    assert pair.present().link_ask(prompt["id"])["status"] == "waiting"


def test_no_conduit_that_shows_them_answers_unavailable_at_once(pair):
    pair.phone_shows_link_asks(features=("room-asks",))
    assert pair.ask({"question": "Is Mona coming to the zoo?"})["status"] == "unavailable"
    assert not pair.asked.held()


def test_an_older_conduit_never_sees_or_answers_a_links_ask(pair):
    pair.phone_shows_link_asks()
    pair.ask({"question": "Is Mona coming to the zoo?"})
    old = pair.present().exchange({"installation_id": "phone-old-0001", "features": [], "results": []})
    assert not any(p.get("link") for p in old["prompts"])
    assert not pair.present().status()


def test_an_unanswered_ask_says_so_after_a_day(pair):
    pair.phone_shows_link_asks()
    ask = pair.ask({"question": "Is Mona coming to the zoo?"})
    pair.clock.now += link_asks.ASK_TTL_MINUTES * 60 + 1
    pair.settle()
    assert pair.asker.sent(ask["id"])["status"] == "unanswered"


def test_budgets_on_the_asked_side(pair):
    pair.phone_shows_link_asks()
    for n in range(link_asks.WAITING_PER_LINK):
        assert pair.ask({"question": f"Question {n}?"})["status"] == "sent"
    assert pair.ask({"question": "One more?"})["status"] == "too_many"
    rules_for(pair, questions="off")
    for n in range(link_asks.PER_HOUR - link_asks.WAITING_PER_LINK - 1):
        pair.asked.on_ask(room_links.Received(link_id=MONA, kind=link_asks.ASK, message_id="x", at=NOW,
                                              body={"v": 1, "ask_id": f"direct-ask-{n:04d}", "question": "Hi?"}))
    over = room_links.Received(link_id=MONA, kind=link_asks.ASK, message_id="x", at=NOW,
                               body={"v": 1, "ask_id": "direct-ask-over", "question": "Hi?"})
    pair.asked.on_ask(over)
    assert pair.asked.store.read()["received"][f"{MONA}:direct-ask-over"]["status"] == "too_many"


def test_an_ask_handled_twice_is_answered_once(pair):
    rules_for(pair, questions="off")
    received = room_links.Received(link_id=MONA, kind=link_asks.ASK, message_id="x", at=NOW,
                                   body={"v": 1, "ask_id": "again-ask-0001", "question": "Hi?"})
    pair.asked.on_ask(received)
    pair.asked.on_ask(received)
    assert [item["kind"] for item in pair.asked.store.read()["outbox"]] == [link_asks.ANSWER]


def test_an_answer_from_another_link_or_again_is_dropped(pair):
    pair.phone_shows_link_asks()
    ask = pair.ask({"question": "Is Mona coming?"})
    assert ask["status"] == "sent"
    forged = room_links.Received(link_id="lnk_other_001", kind=link_asks.ANSWER, message_id="y", at=NOW,
                                 body={"v": 1, "ask_id": ask["id"], "status": "answered", "text": "Yes!"})
    pair.asker.on_answer(forged)
    assert pair.asker.sent(ask["id"])["status"] == "sent"
    real = room_links.Received(link_id=LAIFU, kind=link_asks.ANSWER, message_id="y", at=NOW,
                               body={"v": 1, "ask_id": ask["id"], "status": "declined", "text": "No."})
    pair.asker.on_answer(real)
    pair.asker.on_answer(room_links.Received(link_id=LAIFU, kind=link_asks.ANSWER, message_id="y", at=NOW,
                                             body={"v": 1, "ask_id": ask["id"], "status": "answered", "text": "Yes"}))
    assert pair.asker.sent(ask["id"])["status"] == "declined"


def test_malformed_asks_are_refused(pair):
    for body in ({"v": 1, "ask_id": "bad-ask-00001", "question": "x" * 301},
                 {"v": 1, "ask_id": "bad-ask-00002", "free_on": {"date": "22nd"}},
                 {"v": 1, "ask_id": "bad-ask-00003", "free_on": {"date": "2026-10-16", "from": "17:00", "to": "16:00"}},
                 {"v": 1, "ask_id": "bad-ask-00004", "question": "Hi?", "free_on": {"date": "2026-10-16"}}):
        pair.asked.on_ask(room_links.Received(link_id=MONA, kind=link_asks.ASK, message_id="z", at=NOW, body=body))
        assert pair.asked.store.read()["received"][f"{MONA}:{body['ask_id']}"]["status"] == "bad_ask"


def test_no_shared_calendar_or_a_far_date_cant_tell(pair):
    rules_for(pair, free_on="yes_no")
    assert pair.ask({"free_on": {"date": "2026-10-16"}})["status"] == "unavailable"
    pair.share_calendar()
    assert pair.ask({"free_on": {"date": "2027-03-01"}})["status"] == "unavailable"


def test_the_cli_answers_a_held_ask(pair):
    pair.share_calendar()
    pair.phone_shows_link_asks()
    ask = pair.ask({"free_on": {"date": "2026-10-16"}})
    (held,) = pair.asked.held()
    assert held["share"] == "Busy on 2026-10-16."
    pair.asked.answer_held(held["id"], text="Busy that afternoon only.")
    pair.asked.flush()
    assert pair.asker.sent(ask["id"])["answer"] == "Busy that afternoon only."
    assert not [p for p in pair.phone_shows_link_asks()["prompts"] if p.get("link")]


def test_an_ask_waits_while_the_mailbox_is_down(pair):
    rules_for(pair, questions="off")
    pair.down = True
    ask = pair.ask({"question": "Is Mona coming?"})
    assert ask["status"] == "sending"
    pair.down = False
    pair.asker.flush()
    pair.asked.flush()
    assert pair.asker.sent(ask["id"])["status"] == "not_allowed"


def test_rules_are_checked(pair):
    with pytest.raises(link_asks.AsksError):
        pair.asked.set_rules(MONA, {"free_on": "always"})
    with pytest.raises(link_asks.AsksError):
        pair.asked.set_rules(MONA, {"profile": "nobody"})
    with pytest.raises(link_asks.AsksError):
        pair.asked.set_rules("lnk_nope_0001", {"free_on": "yes_no"})
    assert pair.asked.set_rules(MONA, {"free_on": "times"})["rules"]["free_on"] == "times"


def test_the_store_is_owner_only(pair):
    pair.asked.set_rules(MONA, {"free_on": "times"})
    assert oct(pair.asked.store.path.stat().st_mode & 0o777) == "0o600"


def test_the_routes_show_and_change_the_rules_and_list_the_asks(pair, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from test_room_invites import BASE, api

    module = api.core._plugin_package_module("link_asks")
    # The routes catch their own copy's AsksError, so the asked side is built from that copy.
    pair.asked = pair._asks("asked", MONA, "Mona", "asker", LAIFU, module=module)
    monkeypatch.setattr(api.core, "_calls_home", lambda: pair.homes["asked"])
    monkeypatch.setattr(api.linked_rooms, "_start_room_handlers", lambda root: None)
    monkeypatch.setattr(module, "Asks", lambda root: pair.asked)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    client = TestClient(app)

    shown = client.get(f"{BASE}/links/rules")
    assert shown.status_code == 200 and shown.headers["cache-control"] == "no-store"
    assert shown.json()["links"] == [{"id": MONA, "name": "Mona", "status": "linked", "saved": False,
                                      "rules": {"free_on": "ask", "questions": "ask", "profile": "default"}}]
    changed = client.put(f"{BASE}/links/rules", json={"link_id": MONA, "free_on": "yes_no"})
    assert changed.json() == {"ok": True, "id": MONA,
                              "rules": {"free_on": "yes_no", "questions": "ask", "profile": "default"}}
    refused = client.put(f"{BASE}/links/rules", json={"link_id": MONA, "free_on": "always"})
    assert refused.status_code == 400 and refused.json()["detail"]
    assert client.put(f"{BASE}/links/rules", json={"free_on": "off"}).status_code == 400
    pair.share_calendar()
    pair.ask({"free_on": {"date": "2026-10-16"}})
    (ask,) = client.get(f"{BASE}/links/asks").json()["asks"]
    assert ask["from"] == "Mona" and ask["status"] == "answered" and ask["answer"] == "Busy on 2026-10-16."


# --- CLI ------------------------------------------------------------------------


@pytest.fixture
def cli(pair, monkeypatch):
    import argparse

    module = _load("conduit_push.cli", ROOT / "cli.py")
    monkeypatch.setattr(module, "_link_asks_for", lambda root: pair.asked)
    monkeypatch.setattr(module, "LINKS_READING_WAIT_S", 0)

    def run(action, target="", **values):
        return module.dispatch(argparse.Namespace(
            conduit_push_action="links", links_action=action, target=target,
            **{"name": "", "yes": False, "free_on": None, "questions": None, "answers_from": None, "text": None,
               "share": False, "decline": False, **values}))

    return run


def test_cli_shows_and_changes_a_links_rules(cli, pair, capsys):
    assert cli("rules") == 0
    assert "Mona: free on asks you, questions ask you, answers from default (Link lnk_mona_0001)" in \
        capsys.readouterr().out
    assert cli("rules", MONA, free_on="yes_no", questions="off") == 0
    assert "Mona: free on says free or busy, questions off" in capsys.readouterr().out
    assert pair.asked.rules()["links"][0]["rules"]["free_on"] == "yes_no"
    assert cli("rules", MONA) == 0 and "says free or busy" in capsys.readouterr().out
    assert cli("rules", "lnk_nobody_01", free_on="off") == 1
    assert capsys.readouterr().out.startswith("Couldn't change the rules:")


def test_cli_lists_and_answers_held_asks(cli, pair, capsys):
    pair.share_calendar()
    pair.phone_shows_link_asks()
    ask = pair.ask({"free_on": {"date": "2026-10-16"}})
    assert cli("asks") == 0
    listed = capsys.readouterr().out
    assert "Mona's Hermes (Furina) asks: free on 2026-10-16" in listed and "waits for you" in listed
    assert "Busy on 2026-10-16." in listed and ask["id"] in listed
    assert cli("answer") == 2
    assert cli("answer", ask["id"]) == 2
    assert cli("answer", ask["id"], share=True) == 0
    assert "Answered Mona" in capsys.readouterr().out
    pair.asked.flush()
    assert pair.asker.sent(ask["id"])["answer"] == "Busy on 2026-10-16."
    assert cli("answer", ask["id"], decline=True) == 1
    assert capsys.readouterr().out.startswith("Couldn't answer it:")


def test_cli_with_no_asks_says_so(cli, capsys):
    assert cli("asks") == 0
    assert "No asks from linked people" in capsys.readouterr().out


def test_the_ask_commands_parse():
    import argparse

    module = _load("conduit_push.cli", ROOT / "cli.py")
    parser = argparse.ArgumentParser()
    module.register_cli(parser)
    args = parser.parse_args(["links", "rules", MONA, "--free-on", "times", "--questions", "ask",
                              "--answers-from", "work"])
    assert (args.links_action, args.target, args.free_on, args.questions, args.answers_from) == (
        "rules", MONA, "times", "ask", "work")
    args = parser.parse_args(["links", "answer", "ask_abcdefgh", "--text", "Yes, after 6."])
    assert (args.links_action, args.text, args.share, args.decline) == ("answer", "Yes, after 6.", False, False)
    assert parser.parse_args(["links", "answer", "ask_abcdefgh", "--decline"]).decline is True


def test_an_unreadable_ask_lists_and_answers_without_failing(cli, pair, capsys):
    pair.asked.on_ask(room_links.Received(link_id=MONA, kind=link_asks.ASK, message_id="z", at=NOW,
                                          body={"v": 1, "ask_id": "bad-ask-00009", "free_on": "soon"}))
    assert cli("asks") == 0
    assert "an ask that couldn't be read" in capsys.readouterr().out


def test_a_calendar_answer_says_when_the_phone_last_shared_it(pair):
    import sqlite3

    pair.share_calendar()
    rules_for(pair, free_on="yes_no")
    assert "last shared" not in pair.ask({"free_on": {"date": "2026-10-16"}})["answer"]
    store = phone.PhoneStore(pair.homes["asked"], clock=pair.clock)
    db = sqlite3.connect(store.path)
    db.execute("UPDATE devices SET last_seen=?", (NOW - 3 * 86400,))
    db.commit()
    db.close()
    assert pair.ask({"free_on": {"date": "2026-10-17"}})["answer"].endswith(
        "Their phone last shared its calendar 3 days ago.")


def test_a_calendar_that_fails_to_read_answers_unavailable(pair, monkeypatch):
    rules_for(pair, free_on="yes_no")
    pair.share_calendar()
    monkeypatch.setattr(phone.PhoneStore, "busy_blocks", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    assert pair.ask({"free_on": {"date": "2026-10-16"}})["status"] == "unavailable"


def test_an_ask_for_a_deleted_profile_writes_nothing_there(pair):
    (pair.homes["asked"] / "profiles" / "work").mkdir(parents=True)
    rules_for(pair, profile="work")
    (pair.homes["asked"] / "profiles" / "work").rmdir()
    opened = []
    pair.asked.present = lambda profile: opened.append(profile)
    pair.asked.phone = lambda profile: opened.append(profile)
    assert pair.ask({"question": "Is Mona coming?"})["status"] == "unavailable"
    assert pair.ask({"free_on": {"date": "2026-10-16"}})["status"] == "unavailable"
    assert opened == []


def test_a_question_shows_on_the_phone_as_plain_text(pair):
    pair.phone_shows_link_asks()
    pair.ask({"question": "Coming? [Tap here](https://x.example) **now**"})
    prompt = [p for p in pair.phone_shows_link_asks()["prompts"] if p.get("link")][0]
    assert prompt["preview"] == r"Coming? \[Tap here\]\(https://x.example\) \*\*now\*\*"


def test_cli_answer_says_when_nothing_sends_it(cli, pair, capsys, monkeypatch):
    pair.share_calendar()
    pair.phone_shows_link_asks()
    ask = pair.ask({"free_on": {"date": "2026-10-16"}})
    monkeypatch.setattr(room_links, "reading", lambda root: False)
    assert cli("answer", ask["id"], decline=True) == 0
    assert "Nothing on this Hermes reads its rooms mailbox" in capsys.readouterr().out
