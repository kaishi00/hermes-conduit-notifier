"""Predictive's fast model as one of Hermes' helper models (hermes-conduit,
configuring Hermes from Conduit): the plugin registers `conduit_predict`, so
Hermes' own model routes list it and set it in the profile the request
names, where the plugin's predictions read it."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import yaml

from conftest import TOKEN


def _hermes(dashboard, method, path, body=None, **query):
    qs = f"?{urllib.parse.urlencode(query)}" if query else ""
    headers = {"X-Hermes-Session-Token": TOKEN}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{dashboard.port}{path}{qs}", data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def _predict_row(dashboard, profile):
    status, body = _hermes(dashboard, "GET", "/api/model/auxiliary", profile=profile)
    assert status == 200, body
    return next((row for row in body["tasks"] if row["task"] == "conduit_predict"), None)


def _aux_config(home):
    config = yaml.safe_load((home / "config.yaml").read_text()) or {}
    return (config.get("auxiliary") or {}).get("conduit_predict") or {}


def test_hermes_lists_the_predictions_model_with_its_name(dashboard):
    row = _predict_row(dashboard, "second")
    assert row is not None, "conduit_predict isn't among the helper models"
    assert row["label"] == "Conduit predictions"
    assert row["hint"]
    assert row["provider"] == "auto" and row["model"] == ""


def test_a_pick_lands_in_the_profile_named(dashboard):
    second = dashboard.home / "profiles" / "second"
    status, body = _hermes(dashboard, "POST", "/api/model/set", profile="second", body={
        "scope": "auxiliary", "task": "conduit_predict", "provider": "openrouter", "model": "fast/model",
        "confirm_expensive_model": True, "profile": "second",
    })
    assert status == 200, body
    assert _aux_config(second).get("model") == "fast/model"
    assert _aux_config(dashboard.home).get("model", "") == "", "The launch profile kept its own"
    row = _predict_row(dashboard, "second")
    assert row["provider"] == "openrouter" and row["model"] == "fast/model"

    status, body = _hermes(dashboard, "POST", "/api/model/set", profile="second", body={
        "scope": "auxiliary", "task": "conduit_predict", "provider": "auto", "model": "", "profile": "second",
    })
    assert status == 200, body
    assert _aux_config(second).get("model", "") == ""
