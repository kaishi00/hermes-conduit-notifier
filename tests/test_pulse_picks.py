"""Pulse picks (Eric, 2026-11-09: every card looked alike, and a free-text
report could come out as a buttonless card about something a follow-up
already showed). A pick is checked JSON: its line, a shape with facts, at
most two actions, the subject it's about and what it does to a card."""

import json

from test_pulse import control, pulse

picks = pulse.pulse_picks
store = pulse.store


def fresh(tmp_path):
    return store.PulseStore(tmp_path / "pulse.db")


def card(pulse_store, subject="item:7", kind="noticed", created_at=1000.0, look=None):
    [made] = pulse_store.add_cards([{"kind": kind, "item_id": None, "level": "screen", "title": "x",
                                     "news": "x", "why": "x", "created_at": created_at, "subject": subject, "topic": "jobs",
                                     "look": picks.dumps(look)}])
    return made


def test_a_pick_keeps_its_checked_parts():
    pick, why = picks.parse({"line": "Kwan's birthday is Friday", "detail": "You said you'd call.",
                             "subject": "#41", "shape": "date",
                             "facts": {"day": "2026-11-13", "what": "birthday", "whose": "Kwan"},
                             "actions": [{"label": "Draft a note", "does": "draft", "text": "Happy birthday!"},
                                         {"label": "Later", "does": "later"},
                                         {"label": "Done", "does": "done"}]})
    assert why == ""
    assert pick["shape"] == "date" and pick["facts"]["whose"] == "Kwan"
    assert [action["does"] for action in pick["actions"]] == ["draft", "later"]  # at most two
    assert pick["incident"] == {"card": "new"} and pick["subject"] == "#41"
    assert picks.news(pick) == "Kwan's birthday is Friday. You said you'd call."


def test_facts_that_dont_hold_up_draw_plain_and_say_why():
    pick, why = picks.parse({"line": "Audit failed", "shape": "runs", "facts": {"results": ["ok", "maybe"]}})
    assert pick["shape"] == "plain" and pick["facts"] == {} and why == "bad_runs"
    pick, why = picks.parse({"line": "x", "shape": "sparkle", "facts": {}})
    assert pick["shape"] == "plain" and why == "unknown_shape"
    pick, why = picks.parse({"line": "x", "shape": "due", "facts": {"due": "next week"}})
    assert pick["shape"] == "plain" and why == "bad_due"
    assert picks.parse({"line": "  ", "detail": "y"}) == (None, "no_line")
    assert picks.parse("a line") == (None, "not_an_object")


def test_actions_without_what_they_need_are_left_out():
    actions = picks._actions([{"label": "Ask", "does": "ask"},
                              {"label": "Open", "does": "open", "url": "javascript:alert(1)"},
                              {"label": "Retry", "does": "ask", "say": "Run the audit again"}])
    assert actions == [{"label": "Retry", "does": "ask", "say": "Run the audit again"}]


def test_drawn_bodies_only_for_apps_that_draw_them():
    entry = {"line": "Rain at 4", "shape": "drawn",
             "facts": {"body": {"type": "weather", "title": "Rain", "location": "Paris"}}}
    pick, why = picks.parse(entry)
    assert pick["shape"] == "plain" and why == "body_type"
    assert "drawn" not in picks.rules(())
    assert "drawn" in picks.rules(("weather",))


def test_card_ids_are_permanent_not_list_positions():
    assert picks.card_id("pc_1834") == 1834 and picks.card_id_text(1834) == "pc_1834"
    assert picks.card_id("p3") is None and picks.card_id(3) is None
    assert picks.incident({"close": "pc_9"}) == (picks.CLOSE, 9)
    assert picks.incident({"card": "pc_9"}) == (picks.UPDATE, 9)
    assert picks.incident({"card": "new"}) == (picks.NEW, None)


def test_an_update_to_an_open_card_updates_it_and_runs_build_up(tmp_path):
    pulse_store = fresh(tmp_path)
    first = {"line": "Audit failed", "shape": "runs", "facts": {"job": "audit", "results": ["ok", "fail"]}}
    made = card(pulse_store, subject="job:default:41", look=first)
    pick, _ = picks.parse({"line": "Audit failed again", "subject": "job:default:41",
                           "incident": {"card": f"pc_{made}"}, "shape": "runs",
                           "facts": {"job": "audit", "results": ["ok", "fail", "fail"]}})
    assert picks.resolve(pulse_store, pick, "job:default:41", 2000.0) == (picks.UPDATE, made, "")
    columns = picks.updated_card(pulse_store.card(made), pick)
    assert json.loads(columns["look"])["facts"]["results"] == ["ok", "fail", "fail"]
    assert columns["news"] == "Audit failed again"


def test_a_new_incident_about_an_open_subject_is_held(tmp_path):
    pulse_store = fresh(tmp_path)
    made = card(pulse_store, subject="job:default:41", created_at=1000.0)
    pick, _ = picks.parse({"line": "Audit failed", "subject": "job:default:41"})
    assert picks.resolve(pulse_store, pick, "job:default:41", 2000.0) == (picks.DROP, made, picks.HOLD)
    # A day later that card no longer holds a new one back.
    assert picks.resolve(pulse_store, pick, "job:default:41", 1000.0 + picks.HOLD_S + 1)[0] == picks.NEW


def test_a_dates_next_lead_time_isnt_held_by_the_last_ones_card(tmp_path):
    # The day before and on the day each come up once (pulse_dates stages):
    # the card from the day before, still open, doesn't keep the day's away.
    pulse_store = fresh(tmp_path)
    card(pulse_store, subject="person:p_sam", created_at=1000.0)
    pick, _ = picks.parse({"line": "Sam's birthday is today", "subject": "person:p_sam"})
    assert picks.resolve(pulse_store, pick, "person:p_sam", 2000.0) == (picks.NEW, None, "")


def test_after_a_close_the_same_thing_failing_again_is_a_new_card(tmp_path):
    pulse_store = fresh(tmp_path)
    made = card(pulse_store, subject="job:default:41", created_at=1000.0)
    closing, _ = picks.parse({"line": "Audit recovered", "incident": {"close": f"pc_{made}"}})
    assert picks.resolve(pulse_store, closing, "job:default:41", 2000.0) == (picks.CLOSE, made, "")
    pulse_store.add_cards([], closes=[made], now=2000.0)
    assert not pulse_store.card_open(made)
    again, _ = picks.parse({"line": "Audit failed again", "incident": {"card": f"pc_{made}"}})
    assert picks.resolve(pulse_store, again, "job:default:41", 3000.0) == (picks.NEW, None, "update_closed")
    # Closed by the incident, not the user: its words don't keep a later card out.
    assert pulse_store.closed_cards_on(["jobs"], 0.0) == []


def test_a_close_of_an_unknown_card_does_nothing(tmp_path):
    pulse_store = fresh(tmp_path)
    pick, _ = picks.parse({"line": "Over", "incident": {"close": "pc_999"}})
    assert picks.resolve(pulse_store, pick, "", 1.0) == (picks.DROP, None, "close_unknown")


def test_counts_are_kept_per_day(tmp_path):
    pulse_store = fresh(tmp_path)
    picks.count(pulse_store, 0.0, ["shape:date", "update"])
    picks.count(pulse_store, 60.0, ["shape:date"])
    assert pulse_store.get(picks.PICK_COUNTS) == {"1970-01-01": {"shape:date": 2, "update": 1}}


def test_the_feed_gives_a_look_only_when_asked(tmp_path):
    assert picks.look({"look": ""}) == {"shape": "plain", "facts": {}, "actions": []}
    stored = picks.dumps({"line": "x", "shape": "status", "facts": {"label": "Relay", "state": "ok"}, "actions": []})
    assert picks.look({"look": stored})["facts"] == {"label": "Relay", "state": "ok"}


def test_the_brief_is_laid_out_from_its_parts():
    reply = ("A light day, with one thing due.\n\nToday:\n- **Dentist** at 3\n\nStill open:\n"
             "- Send Mara the deck\n- Renew the passport")
    look = pulse.brief.look(reply, held=2)
    assert look["shape"] == picks.BRIEF and look["line"] == "A light day, with one thing due."
    assert look["facts"]["today"] == ["Dentist at 3"]
    assert look["facts"]["open"] == ["Send Mara the deck", "Renew the passport"]
    assert look["facts"]["counts"] == {"today": 1, "open": 2, "held": 2}
    assert picks.look({"look": picks.dumps(look)})["shape"] == picks.BRIEF
    # Lines under no heading are still open.
    assert pulse.brief.look("Quiet.\n- One thing", held=0)["facts"]["open"] == ["One thing"]


def test_the_feed_adds_looks_only_for_apps_that_ask(tmp_path):
    pulse_store = fresh(tmp_path)
    look = {"line": "Relay is up", "shape": "status", "facts": {"label": "Relay", "state": "ok"}, "actions": []}
    made = card(pulse_store, subject="job:default:9", created_at=1000.0, look=look)
    [old] = [entry for entry in control.feed(pulse_store)["cards"] if entry["id"] == made]
    assert not {"look", "card_ref", "subject", "updated_at"} & set(old)
    [new] = [entry for entry in control.feed(pulse_store, looks=True)["cards"] if entry["id"] == made]
    assert new["card_ref"] == f"pc_{made}" and new["subject"] == "job:default:9" and "updated_at" in new
    assert new["look"]["shape"] == "status" and new["look"]["facts"] == {"label": "Relay", "state": "ok"}
    assert {key: value for key, value in new.items() if key not in ("look", "card_ref", "subject", "updated_at")} == old


def test_a_close_earlier_in_the_same_report_holds_nothing_back(tmp_path):
    pulse_store = fresh(tmp_path)
    made = card(pulse_store, subject="job:default:41", created_at=1000.0)
    again, _ = picks.parse({"line": "Audit failed again", "subject": "job:default:41"})
    assert picks.resolve(pulse_store, again, "job:default:41", 2000.0)[0] == picks.DROP
    assert picks.resolve(pulse_store, again, "job:default:41", 2000.0, closing=[made]) == (picks.NEW, None, "")


def test_an_update_to_an_open_card_about_something_else_says_so(tmp_path):
    pulse_store = fresh(tmp_path)
    made = card(pulse_store, subject="job:default:41", created_at=1000.0)
    pick, _ = picks.parse({"line": "x", "incident": {"card": f"pc_{made}"}})
    assert picks.resolve(pulse_store, pick, "item:3", 2000.0) == (picks.NEW, None, "update_subject")


def test_the_next_report_reads_a_closed_incident_as_over_not_done(tmp_path):
    pulse_store = fresh(tmp_path)
    now = 10 * 86400.0
    closed = card(pulse_store, subject="job:default:41", created_at=now - 3600)
    done = card(pulse_store, subject="item:7", created_at=now - 3600)
    pulse_store.add_cards([], closes=[closed], now=now - 60)
    pulse_store.mark_done(done, None, closed_by="user", now=now - 60)
    text = pulse.candidates.recent_section(pulse_store, None, now)
    assert f"pc_{closed} " in text and "(closed: that occurrence ended)" in text
    line = next(line for line in text.splitlines() if f"pc_{done} " in line)
    assert line.endswith("(done)")


def test_a_card_a_pick_updated_lately_is_still_in_the_recent_list(tmp_path):
    pulse_store = fresh(tmp_path)
    now = 30 * 86400.0
    old = card(pulse_store, subject="job:default:41", created_at=now - pulse.candidates.RECENT_S - 3600)
    assert f"pc_{old} " not in pulse.candidates.recent_section(pulse_store, None, now)
    pulse_store.add_cards([], updates=[(old, {"news": "Audit failed again", "fallback": {}, "link": None})], now=now - 60)
    assert f"pc_{old} " in pulse.candidates.recent_section(pulse_store, None, now)


def report_cards(pulse_store, entries, now):
    from types import SimpleNamespace
    return pulse.learning.add_noticed_cards(
        pulse_store, turn=SimpleNamespace(session_id="s", reply_id=1), lines=[("other", entry["line"], None)
                                                                             for entry in entries],
        chat_id="s", chat_title="Pulse", shown={}, kinds=pulse.report.KINDS, item_chat_title=lambda item: "",
        now=now, looks=[picks.dumps(picks.parse(entry)[0]) for entry in entries],
        subjects=[entry.get("subject", "") for entry in entries])


def test_a_close_later_in_the_same_report_still_lets_the_new_incident_through(tmp_path):
    pulse_store = fresh(tmp_path)
    made = card(pulse_store, subject="job:default:41", created_at=1000.0)
    again = {"line": "Audit failed again", "subject": "job:default:41"}
    closing = {"line": "Audit recovered", "subject": "job:default:41", "incident": {"close": f"pc_{made}"}}
    assert picks.closed_in(pulse_store, [(picks.parse(again)[0], again["subject"]),
                                         (picks.parse(closing)[0], closing["subject"])], 2000.0) == [made]
    [new] = report_cards(pulse_store, [again, closing], 2000.0)
    assert new != made and pulse_store.card(new)["news"] == "Audit failed again"
    assert not pulse_store.card_open(made) and pulse_store.card_open(new)


def test_an_update_to_a_card_the_same_report_closes_is_a_new_card(tmp_path):
    pulse_store = fresh(tmp_path)
    made = card(pulse_store, subject="job:default:41", created_at=1000.0)
    pick, _ = picks.parse({"line": "x", "incident": {"card": f"pc_{made}"}})
    assert picks.resolve(pulse_store, pick, "job:default:41", 2000.0, closing=[made]) == (picks.NEW, None,
                                                                                         "update_closed")


def test_an_update_gives_a_card_without_a_subject_its_subject(tmp_path):
    pulse_store = fresh(tmp_path)
    made = card(pulse_store, subject="", created_at=1000.0)
    report_cards(pulse_store, [{"line": "Audit failed again", "subject": "job:default:41",
                                "incident": {"card": f"pc_{made}"}}], 2000.0)
    assert pulse_store.card(made)["subject"] == "job:default:41"
    assert "subject" not in picks.updated_card({"id": 1, "subject": "item:7", "look": ""},
                                               picks.parse({"line": "x"})[0], "item:8")
    pick, _ = picks.parse({"line": "Audit failed", "subject": "job:default:41"})
    assert picks.resolve(pulse_store, pick, "job:default:41", 3000.0)[0] == picks.DROP


def test_a_card_the_report_closes_says_nothing_still(tmp_path):
    pulse_store = fresh(tmp_path)
    made = card(pulse_store, subject="", kind="noticed", created_at=1000.0)
    pulse_store.add_cards([], updates=[(made, {"news": "Audit failed", "topic": "other", "fallback": {}, "link": None})],
                          now=1500.0)
    row = {"origin": "found", "item_id": None, "ref": "", "kind": "other", "text": "Audit failed"}
    assert pulse.candidates.still_open(pulse_store, [row], 2000.0) == [row]
    assert pulse.candidates.still_open(pulse_store, [row], 2000.0, [made]) == []


def test_a_left_out_pick_brought_up_keeps_its_look_and_subject(tmp_path):
    pulse_store = fresh(tmp_path)
    pick, _ = picks.parse({"line": "Audit failed", "subject": "job:default:41", "incident": {"card": "pc_3"},
                           "shape": "runs", "facts": {"job": "audit", "results": ["ok", "fail"]}})
    found = {"kind": "other", "text": "Audit failed", "why": "", "factor": "", "place": "", "band": "",
             "item_id": None, "chat_id": "", "profile": "", "pick": picks.dumps(pick), "subject": "job:default:41"}
    row = pulse.learning.left_out_card(pulse_store, found, kinds=pulse.report.KINDS, now=1.0,
                                       item_chat_title=lambda item: "")
    assert row["subject"] == "job:default:41" and json.loads(row["look"])["shape"] == "runs"
    assert json.loads(row["look"])["incident"] == {"card": "new"}


def test_a_report_can_update_or_close_a_look_ahead_date_card_by_its_id(tmp_path):
    pulse_store = fresh(tmp_path)
    made = card(pulse_store, subject="deadline:12", created_at=1000.0)
    update, _ = picks.parse({"line": "Passport renewal is due tomorrow", "incident": {"card": f"pc_{made}"}})
    assert picks.resolve(pulse_store, update, "item:12", 2000.0) == (picks.UPDATE, made, "")
    closing, _ = picks.parse({"line": "Passport renewed", "incident": {"close": f"pc_{made}"}})
    assert picks.resolve(pulse_store, closing, "item:12", 2000.0) == (picks.CLOSE, made, "")
    # Two keys of one kind that differ are two things.
    assert picks.resolve(pulse_store, closing, "deadline:13", 2000.0) == (picks.DROP, None, "close_subject")


def test_a_close_that_didnt_come_up_is_still_applied_with_the_new_card_it_let_through(tmp_path):
    pulse_store = fresh(tmp_path)
    made = card(pulse_store, subject="job:default:41", created_at=1000.0)
    from types import SimpleNamespace
    entry = {"line": "Audit failed again", "subject": "job:default:41"}
    [new] = pulse.learning.add_noticed_cards(
        pulse_store, turn=SimpleNamespace(session_id="s", reply_id=1), lines=[("other", entry["line"], None)],
        chat_id="s", chat_title="Pulse", shown={}, kinds=pulse.report.KINDS, item_chat_title=lambda item: "",
        now=2000.0, looks=[picks.dumps(picks.parse(entry)[0])], subjects=[entry["subject"]], closing=[made])
    assert pulse_store.card_open(new) and not pulse_store.card_open(made)


def test_a_card_brought_back_after_an_update_holds_by_when_it_came_back(tmp_path):
    import sqlite3
    pulse_store = fresh(tmp_path)
    made = card(pulse_store, subject="job:default:41", created_at=1000.0)
    pulse_store.add_cards([], updates=[(made, {"news": "Audit failed again", "fallback": {}, "link": None})], now=1500.0)
    later = 1500.0 + 3 * picks.HOLD_S
    with sqlite3.connect(pulse_store.path) as conn:  # as bring_back, return_card and release_card do
        conn.execute("UPDATE cards SET created_at=? WHERE id=?", (later, made))
    pick, _ = picks.parse({"line": "Audit failed", "subject": "job:default:41"})
    assert picks.resolve(pulse_store, pick, "job:default:41", later + 60) == (picks.DROP, made, picks.HOLD)


def test_a_second_update_to_one_card_keeps_the_subject_the_first_gave_it(tmp_path):
    pulse_store = fresh(tmp_path)
    made = card(pulse_store, subject="", created_at=1000.0)
    report_cards(pulse_store, [{"line": "Audit failed", "subject": "job:default:41", "incident": {"card": f"pc_{made}"}},
                               {"line": "Audit failed twice", "incident": {"card": f"pc_{made}"}}], 2000.0)
    assert pulse_store.card(made)["subject"] == "job:default:41"
    assert pulse_store.card(made)["news"] == "Audit failed twice"
