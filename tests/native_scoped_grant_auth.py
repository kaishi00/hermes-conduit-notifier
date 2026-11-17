"""Run with native Hermes' Python/PYTHONPATH and a disposable HERMES_HOME.

Uses unchanged core HTML/bootstrap, password login, cookie validation and owner
identity. Only session RPC execution and plugin hook registration use fixtures.
No sockets, models or existing profile state are required.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import tempfile

# Native imports read HERMES_HOME: isolate them before importing core.
HOME = Path(tempfile.mkdtemp(prefix="scoped-grant-auth-")) / "hermes"
HOME.mkdir()
os.environ["HERMES_HOME"] = str(HOME)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
import hermes_cli.web_server as web

from hermes_cli.dashboard_auth.middleware import gated_auth_middleware
from hermes_cli.dashboard_auth import routes, registry
from plugins.dashboard_auth.basic import BasicAuthProvider, hash_password

from test_work import ROOT, hermes, on, bots, work, call, BOT_CHAT, store_mod
from test_work_grants import recipient_sessions, TASK, SCOPE, BASE


def main(after=None):
    home = HOME
    mp = pytest.MonkeyPatch()
    fake = hermes.__wrapped__(home, mp)
    store = on.__wrapped__(home, fake)
    homes = bots.__wrapped__(home, store, fake, mp)
    recipient_sessions.__wrapped__(fake, homes, mp)
    spec = importlib.util.spec_from_file_location("native_grant_api", ROOT / "dashboard/plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    mp.setattr(api.workstreams, "_work_tool", lambda: work)
    mp.setattr(api.core, "_calls_home", lambda: home)
    args = {"bot": "nahida", "text": TASK, "scope": SCOPE}
    work.tool_block(BOT_CHAT, "work_ask", args, turn_id="native-turn", tool_call_id="native-call")
    assert call("work_ask", args)["ok"]
    grant = work.work_grants.cards(home)["grants"][0]
    url = BASE + "/" + grant["id"]
    app = FastAPI()
    app.state.auth_required = False
    app.include_router(api.router, prefix="/api/plugins/conduit_push")
    client = TestClient(app)
    assert client.get(BASE + "/grants").status_code == 401
    dist = home.parent / "web-dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><html><head></head><body>Auth probe</body></html>")
    mp.setattr(web.app.state, "web_dist", dist, raising=False)
    mp.setattr(web.app.state, "auth_required", False, raising=False)
    page = TestClient(web.app).get("/")
    assert page.status_code == 200
    match = re.search(r'window\.__HERMES_SESSION_TOKEN__=("[^"]+")', page.text)
    assert match, "Native anonymous standalone bootstrap must exercise its public token"
    headers = {"x-hermes-session-token": json.loads(match.group(1)),
               "origin": "http://testserver", "sec-fetch-site": "same-origin"}
    preview = client.get(BASE + "/grants", headers=headers)
    approved = client.post(url + "/grant_approve", headers=headers, json={"digest": grant["digest"]})
    print(json.dumps({"anonymous_html": page.status_code, "bootstrap_preview": preview.status_code,
                      "bootstrap_approve": approved.status_code, "session_creates": len(fake.named("session.create"))}))
    assert preview.status_code in (401, 403), preview.text
    assert approved.status_code in (401, 403), approved.text
    assert work.work_grants.get(home, grant["id"])["status"] == "pending"
    assert not fake.named("session.create") and not fake.named("prompt.submit")
    password = secrets.token_urlsafe(24)
    registry.register_global_provider(BasicAuthProvider(username="test-owner", password_hash=hash_password(password),
                                                      secret=secrets.token_bytes(32)))
    gated = FastAPI()
    gated.state.auth_required = True
    gated.middleware("http")(gated_auth_middleware)
    gated.include_router(routes.router)
    gated.include_router(api.router, prefix="/api/plugins/conduit_push")
    owner = TestClient(gated)
    assert owner.get(BASE + "/grants").status_code == 401
    login = owner.post("/auth/password-login", json={"provider": "basic", "username": "test-owner", "password": password})
    assert login.status_code == 200 and owner.cookies
    assert owner.get(BASE + "/grants").status_code == 200
    body = {"digest": grant["digest"]}
    assert owner.post(url + "/grant_approve", headers={"origin": "https://evil.example"}, json=body).status_code == 403
    headers.pop("x-hermes-session-token")
    assert owner.post(url + "/grant_approve", headers=headers, json=body).status_code == 200
    assert owner.post(url + "/grant_approve", headers=headers, json=body).status_code == 409
    worker = store_mod.Store(homes["nahida"]).get("W1")
    assert worker and worker.goal == TASK
    assert work.work_grants.get(home, grant["id"])["actor"]["source"] == "dashboard_session"
    assert len(fake.named("session.create")) == len(fake.named("prompt.submit")) == 1
    assert not fake.named("approval.respond") and not fake.named("config.set")
    assert owner.post(url + "/grant_revoke", headers=headers, json=body).status_code == 200
    assert store_mod.Store(homes["nahida"]).get("W1").status == "stopped"
    assert fake.named("session.interrupt")
    print(json.dumps({"password_login": login.status_code, "cookie_preview": 200, "cookie_approve": 200,
                      "cross_origin": 403, "replay": 409, "revoke": 200, "worker_status": "stopped",
                      "session_creates": 1, "prompt_submits": 1, "approval_responses": 0, "config_writes": 0,
                      "auth": "native unchanged core", "rpc": "FakeHermes", "real_browser_or_model": False}))
    if after is not None:
        after(home, api, owner, client, fake, homes)
    mp.undo()


if __name__ == "__main__":
    main()
