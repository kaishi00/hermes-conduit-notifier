"""Chat history pages on a real dashboard (hermes-conduit#1525): the plugin's
page is Hermes' own page of the chat's newest rows, row for row, read from
the profile the request named, with the rows Conduit holds sent as their
fingerprints."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from conftest import TOKEN

PAGE = {"limit": 120, "offset": 0, "order": "latest", "include_compacted": True, "inline_images": False}


def _seed(home, session_id, words):
    from hermes_state import SessionDB

    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(session_id, "cli")
        for index, word in enumerate(words):
            db.append_message(session_id, "user" if index % 2 == 0 else "assistant", word)
    finally:
        db.close()


def _hermes_page(dashboard, session_id, profile=None):
    query = {key: str(value).lower() if isinstance(value, bool) else str(value) for key, value in PAGE.items()}
    if profile:
        query["profile"] = profile
    url = (f"http://127.0.0.1:{dashboard.port}/api/sessions/{urllib.parse.quote(session_id)}/messages?"
           + urllib.parse.urlencode(query))
    req = urllib.request.Request(url, headers={"X-Hermes-Session-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def test_the_page_is_hermes_page_and_known_rows_come_as_fingerprints(dashboard):
    _seed(dashboard.home, "history-default", ["one", "two", "three", "four"])
    expected = _hermes_page(dashboard, "history-default")
    status, body = dashboard.request("POST", "sessions/history-page", body={"session_id": "history-default", **PAGE})
    assert status == 200, body
    assert body["messages"] == expected["messages"]
    assert body["pagination"] == expected["pagination"]
    assert len(body["row_digests"]) == len(expected["messages"]) == 4

    known = body["row_digests"][:3]
    status, again = dashboard.request("POST", "sessions/history-page",
                                      body={"session_id": "history-default", "known": known, **PAGE})
    assert status == 200, again
    assert again["messages"][:3] == [{"conduit_known": digest} for digest in known]
    assert again["messages"][3] == expected["messages"][3]


def test_the_page_comes_from_the_profile_named(dashboard):
    _seed(dashboard.home / "profiles" / "second", "history-second", ["only here"])
    status, body = dashboard.request("POST", "sessions/history-page", profile="second",
                                     body={"session_id": "history-second", **PAGE})
    assert status == 200, body
    assert [row.get("content") for row in body["messages"]] == ["only here"]
    status, body = dashboard.request("POST", "sessions/history-page", body={"session_id": "history-second", **PAGE})
    assert status == 404, body


def test_a_bad_order_gets_hermes_400(dashboard):
    _seed(dashboard.home, "history-order", ["one"])
    status, body = dashboard.request("POST", "sessions/history-page",
                                     body={"session_id": "history-order", **PAGE, "order": "newest"})
    assert status == 400, body
