"""An admitted session stays scoped independently of its mutable work row."""
import pytest

from test_work_grants import (act, as_bot, bots, hermes, home, http, on, preview,
                              scoped, store_mod, work, recipient_sessions)


@pytest.mark.parametrize("loss", ["database", "row", "replacement", "unreadable"])
@pytest.mark.parametrize("revoke_first", [False, True])
def test_registration_loss_and_recovery_never_release_authority(http, bots, hermes, monkeypatch, loss, revoke_first):
    assert scoped()["ok"]
    grant = preview(http)
    assert act(http, grant).status_code == 200
    target = store_mod.Store(bots["nahida"])
    worker = target.get("W1")
    if revoke_first:
        assert act(http, grant, "grant_revoke").status_code == 200
    if loss in ("database", "replacement", "unreadable"):
        saved = target.path.with_suffix(".saved")
        target.path.rename(saved)
        if loss == "replacement":
            target.set_meta("grant_identity", "replacement")
        elif loss == "unreadable":
            target.path.write_bytes(b"not a database")
    else:
        with target._db() as db:
            columns = [r[1] for r in db.execute("PRAGMA table_info(work)")]
            saved_row = db.execute("SELECT * FROM work WHERE ref = ?", (worker.ref,)).fetchone()
            db.execute("DELETE FROM work WHERE ref = ?", (worker.ref,))
    hermes._sessions["rt-" + worker.session_id]["pending_title"] = "Bot Chat"
    as_bot(monkeypatch, bots, "nahida")
    assert work._titled_bot_chat(worker.session_id, target.home) is True
    assert work._lookup(worker.session_id, target.home)[2:] == (True, True)
    assert work.turn_started(worker.session_id) is True
    assert work._turn_ended(worker.session_id, "canceled task result") is True
    assert work._turn_finished(worker.session_id, failed=False, interrupted=True) is True
    before_creates = len(hermes.named("session.create"))
    for name in ("terminal", "delegate_task", "work_start", "work_list"):
        block = work.tool_block(worker.session_id, name, {"command": "pwd"})
        assert block and block["action"] == "block"
    if not revoke_first:
        response = act(http, grant, "grant_revoke")
        assert response.status_code == 200, response.text
    assert hermes.named("session.interrupt")
    assert not hermes._sessions["rt-" + worker.session_id]["running"]
    for name in ("terminal", "delegate_task"):
        assert work.tool_block(worker.session_id, name, {})["action"] == "block"
    # Restore the exact pre-loss registration: neither recovery nor revoke can
    # resurrect a consumed turn. The binding is persisted, not a process cache.
    if loss != "row":
        if target.path.exists():
            target.path.unlink()
        saved.rename(target.path)
    else:
        with target._db() as db:
            db.execute(f"INSERT INTO work ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(saved_row))
    for name in ("terminal", "delegate_task"):
        assert work.tool_block(worker.session_id, name, {})["action"] == "block"
    before = len(hermes.named("prompt.submit"))
    work._recover_one(target.home, target, work.work_broker.Broker(target.home), target.get(worker.ref), "continue", None)
    assert len(hermes.named("prompt.submit")) == before
    assert not work.app_act(target.home, worker.ref, "continue")["ok"]
    assert len(hermes.named("session.create")) == before_creates


@pytest.mark.parametrize("loss", ["database", "row"])
def test_retitled_canceled_task_stays_scoped_after_unconfirmed_interrupt(http, bots, hermes, monkeypatch, loss):
    assert scoped()["ok"]
    grant = preview(http)
    assert act(http, grant).status_code == 200
    target = store_mod.Store(bots["nahida"])
    worker = target.get("W1")
    native = hermes._sessions["rt-" + worker.session_id]
    hermes.refuse["session.interrupt"] = {"code": -32000, "message": "interrupt failed"}
    response = act(http, grant, "grant_revoke")
    assert response.status_code == 409 and "could not be confirmed" in response.json()["detail"]
    assert preview(http)["status"] == "revoked"
    assert target.get(worker.ref).status == "stopped" and native["running"]
    if loss == "database":
        saved = target.path.with_suffix(".saved")
        target.path.rename(saved)
    else:
        with target._db() as db:
            columns = [r[1] for r in db.execute("PRAGMA table_info(work)")]
            saved_row = db.execute("SELECT * FROM work WHERE ref = ?", (worker.ref,)).fetchone()
            db.execute("DELETE FROM work WHERE ref = ?", (worker.ref,))
    native["pending_title"] = "Bot Chat"
    as_bot(monkeypatch, bots, "nahida")
    assert work._titled_bot_chat(worker.session_id, target.home) is True
    binding = work.work_grants.session_binding(target.home, worker.session_id)
    assert binding and binding["grant_id"] == grant["id"]
    before_creates = len(hermes.named("session.create"))
    before_prompts = len(hermes.named("prompt.submit"))
    for name in ("terminal", "delegate_task", "work_start", "work_list"):
        block = work.tool_block(worker.session_id, name, {})
        assert block and block["action"] == "block"
    if loss == "database":
        saved.rename(target.path)
    else:
        with target._db() as db:
            db.execute(f"INSERT INTO work ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(saved_row))
    for name in ("terminal", "delegate_task", "work_start", "work_list"):
        assert work.tool_block(worker.session_id, name, {})["action"] == "block"
    work._recover_one(target.home, target, work.work_broker.Broker(target.home), target.get(worker.ref), "continue", None)
    assert not work.app_act(target.home, worker.ref, "continue")["ok"]
    assert act(http, grant).status_code == 409
    assert native["running"]
    assert len(hermes.named("session.create")) == before_creates
    assert len(hermes.named("prompt.submit")) == before_prompts
    del hermes.refuse["session.interrupt"]
    assert act(http, grant, "grant_revoke").status_code == 200
    assert not native["running"]


@pytest.mark.parametrize("loss", ["database", "unreadable"])
def test_genuinely_unbound_bot_chat_keeps_registration_read_exemption(http, bots, hermes, monkeypatch, loss):
    from test_work import NAHIDA_CHAT

    assert scoped()["ok"]
    target = store_mod.Store(bots["nahida"])
    target.path.rename(target.path.with_suffix(".saved"))
    if loss == "unreadable":
        target.path.write_bytes(b"not a database")
    as_bot(monkeypatch, bots, "nahida")
    assert work.work_grants.session_binding(target.home, NAHIDA_CHAT) is None
    assert work._titled_bot_chat(NAHIDA_CHAT, target.home) is True
    assert work.tool_block(NAHIDA_CHAT, "work_start", {}) is None


@pytest.mark.parametrize("loss", ["database", "row"])
def test_registration_loss_with_failed_interrupt_reports_unconfirmed(http, bots, hermes, loss):
    scoped()
    grant = preview(http)
    assert act(http, grant).status_code == 200
    target = store_mod.Store(bots["nahida"])
    if loss == "database":
        target.path.rename(target.path.with_suffix(".saved"))
    else:
        with target._db() as db:
            db.execute("DELETE FROM work WHERE ref = 'W1'")
    hermes.refuse["session.interrupt"] = {"code": -32000, "message": "interrupt failed"}
    response = act(http, grant, "grant_revoke")
    assert response.status_code == 409
    assert "could not be confirmed" in response.json()["detail"]
    assert preview(http)["status"] == "revoked"


@pytest.mark.parametrize("mutation", ["grant", "identity", "session"])
def test_replaced_worker_row_cannot_inherit_admitted_session(http, bots, hermes, monkeypatch, mutation):
    scoped()
    grant = preview(http)
    assert act(http, grant).status_code == 200
    target = store_mod.Store(bots["nahida"])
    worker = target.get("W1")
    if mutation == "grant":
        target.update(worker.ref, requested_by={"by": "user"})
    else:
        column = "idempotency_key" if mutation == "identity" else "session_id"
        with target._db() as db:
            db.execute(f"UPDATE work SET {column} = 'replacement' WHERE ref = ?", (worker.ref,))
    hermes._sessions["rt-" + worker.session_id]["pending_title"] = "Bot Chat"
    as_bot(monkeypatch, bots, "nahida")
    assert work._titled_bot_chat(worker.session_id, target.home) is True
    for name in ("terminal", "delegate_task", "work_start", "work_list"):
        block = work.tool_block(worker.session_id, name, {})
        assert block and block["action"] == "block"
    assert act(http, grant, "grant_revoke").status_code == 200
    assert not hermes._sessions["rt-" + worker.session_id]["running"]
