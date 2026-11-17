"""Explicit owner enrollment and one-use Ed25519 phone consent receipts.

Notification encryption is not consent. These are independent signing keys,
created by an explicit enrollment tap and registered only by the owner HTTP
adapter. No tool, push credential or generic card tap calls this adapter.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from . import work_grants

PROTOCOL = "conduit-phone-consent/1"
TTL = 60
_B64 = re.compile(r"^[A-Za-z0-9_-]+$")


class Refusal(ValueError):
    def __init__(self, status: int, note: str):
        super().__init__(note)
        self.status = status


def decode(value: str, length: int) -> bytes:
    if (not isinstance(value, str) or len(value) != (length * 8 + 5) // 6
            or not _B64.fullmatch(value)):
        raise Refusal(400, "Malformed phone consent key or signature.")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(raw) != length or base64.urlsafe_b64encode(raw).decode().rstrip("=") != value:
        raise Refusal(400, "Malformed phone consent key or signature.")
    return raw


def pairing(home: Path) -> dict:
    try:
        state = json.loads((Path(home) / "conduit-push.json").read_text())
        keys = ("installation_id", "gateway_id", "credential", "relay_url")
        if not all(isinstance(state.get(k), str) and state[k] for k in keys) or state.get("relay_rejected_at"):
            raise ValueError()
        return {"installation_id": state["installation_id"], "gateway_id": state["gateway_id"],
                "binding": hashlib.sha256(work_grants.canonical({k: state[k] for k in keys}).encode()).hexdigest()}
    except (OSError, ValueError, AttributeError):
        raise Refusal(409, "Pair this profile with this phone before enabling scoped consent.")


def _device(db, profile_id: str):
    row = db.execute("SELECT record FROM consent_devices WHERE profile_id = ? AND active = 1",
                     (profile_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _matches(home: Path, device: dict) -> bool:
    try:
        return device["pairing"] == pairing(home)
    except Refusal:
        return False


def actor_valid(home: Path, actor: dict) -> bool:
    if actor.get("source") != "phone_consent":
        return True
    try:
        enrolled_home = Path(actor["profile_id"])
        if work_grants.root(enrolled_home).resolve() != work_grants.root(home).resolve():
            return False
        with work_grants.registry(home)._db(write=False) as db:
            device = _device(db, str(enrolled_home.resolve()))
        return bool(device and device["id"] == actor["device"] and device["owner"] == actor["subject"]
                    and _matches(enrolled_home, device))
    except Exception:
        return False


def _retire(db, devices: list[dict], audit: dict) -> list[dict]:
    grants = []
    for device in devices:
        retired = {**device, "revoked_at": time.time(), "revoked_by": audit}
        db.execute("UPDATE consent_devices SET active = 0, record = ? WHERE id = ?",
                   (work_grants.canonical(retired), device["id"]))
        rows = db.execute("SELECT * FROM task_grants WHERE status IN ('pending', 'consumed') "
                          "AND json_extract(actor, '$.source') = 'phone_consent' "
                          "AND json_extract(actor, '$.device') = ?", (device["id"],)).fetchall()
        grants.extend(work_grants._decode(r) for r in rows)
        db.executemany("UPDATE task_grants SET status = 'revoked', revoked_by = ?, acted_at = ? WHERE id = ?",
                       [(work_grants.canonical({"source": "phone_device_revocation", "device": device["id"]}),
                         time.time(), r[0]) for r in rows])
    return grants


def _interrupt(home: Path, tool, grants: list[dict]) -> bool:
    confirmed = True
    for grant in grants:
        # Persisted revocation is independent of mutable recipient registration.
        if not work_grants.stop(tool, home, grant):
            confirmed = False
    return confirmed


def pairing_changed(home: Path, tool) -> bool:
    store = work_grants.registry(home)
    if not store.path.exists():
        return True
    with store._db() as db:
        device = _device(db, str(Path(home).resolve()))
        retired = _retire(db, [device], {"source": "pairing_changed"}) if device and not _matches(home, device) else []
    return _interrupt(home, tool, retired)


def pairing_file_changed(path: Path) -> None:
    """Used after atomic pairing writes/removal, outside the pairing file lock."""
    home = Path(path).parent
    if not work_grants.registry(home).path.exists():
        return
    from . import work_tool

    tool = work_tool.copy_for(home) or work_tool
    if not pairing_changed(home, tool):
        tool.logger.warning("Consent device revoked; native interruption could not be confirmed")


def revoke(home: Path, tool, actor: dict, body: dict) -> dict:
    if set(body) != {"device"} or not isinstance(body["device"], str):
        raise Refusal(400, "Send the displayed device enrollment.")
    with work_grants.registry(home)._db() as db:
        device = _device(db, actor["profile_id"])
        if not device or device["id"] != body["device"] or device["owner"] != actor["subject"]:
            raise Refusal(409, "This device enrollment has changed.")
        retired = _retire(db, [device], {**actor, "source": "phone_device_revocation"})
    confirmed = _interrupt(home, tool, retired)
    return {"ok": True, "interrupted": confirmed,
            "note": "" if confirmed else "Device revoked; native interruption could not be confirmed. Tools and resume remain blocked."}


def preview(home: Path, tool, actor: dict) -> dict:
    confirmed = pairing_changed(home, tool)
    current_pairing = pairing(home)
    with work_grants.registry(home)._db() as db:
        device = _device(db, actor["profile_id"])
    profile = tool.work_broker.profile_of(home)
    grants = [g for g in work_grants.cards(home)["grants"]
              if profile in {g["snapshot"]["sender"]["profile"], g["snapshot"]["recipient"]["profile"]}
              and work_grants.digest(g["snapshot"]) == g["digest"]]
    return {"protocol": PROTOCOL, "device": device["id"] if device else None,
            "profile_id": actor["profile_id"], "owner": actor["subject"], "pairing": current_pairing,
            "installation": work_grants.identity(work_grants.registry(home)),
            "public_key": device["public_key"] if device else None,
            "installation_id": current_pairing["installation_id"], "gateway_id": current_pairing["gateway_id"],
            "grants": [{**g, "snapshot_text": work_grants.canonical(g["snapshot"])} for g in grants], "interrupted": confirmed}


def challenge(home: Path, tool, actor: dict, body: dict) -> dict:
    if not all(isinstance(value, str) for value in body.values()):
        raise Refusal(400, "Phone consent fields must be strings.")
    action = body.get("action")
    pairing_changed(home, tool)
    pair = pairing(home)
    store = work_grants.registry(home)
    installation = work_grants.identity(store)
    with store._db() as db:
        device = _device(db, actor["profile_id"])
        if action == "enroll":
            if set(body) != {"action", "public_key", "installation_id", "gateway_id"}:
                raise Refusal(400, "Send only the phone's new public key and current pairing.")
            decode(body["public_key"], 32)
            if any(body[k] != pair[k] for k in ("installation_id", "gateway_id")):
                raise Refusal(409, "This profile is paired with another phone.")
            context = {"public_key": body["public_key"], "device": uuid.uuid4().hex,
                       "previous": device["id"] if device else None}
        elif action in work_grants.ACTIONS:
            if set(body) != {"action", "device", "grant", "digest"}:
                raise Refusal(400, "Send only the displayed grant and device enrollment.")
            if (not device or device["id"] != body["device"] or device["owner"] != actor["subject"]
                    or not _matches(home, device)):
                raise Refusal(409, "Enable scoped consent on this phone first.")
            row = db.execute("SELECT snapshot, digest FROM task_grants WHERE id = ?", (body["grant"],)).fetchone()
            if not row or row[1] != body["digest"] or work_grants.digest(json.loads(row[0])) != row[1]:
                raise Refusal(409, "The displayed scoped task does not match.")
            snapshot = json.loads(row[0])
            if tool.work_broker.profile_of(home) not in {snapshot["sender"]["profile"], snapshot["recipient"]["profile"]}:
                raise Refusal(403, "This grant is not for this profile.")
            context = {"device": device["id"], "public_key": device["public_key"],
                       "grant": body["grant"], "digest": body["digest"]}
        else:
            raise Refusal(400, "Unknown phone consent action.")
        nonce = uuid.uuid4().hex
        expires = int(time.time()) + TTL
        message = work_grants.canonical({"protocol": PROTOCOL, "challenge": nonce, "expires": expires,
                                        "installation": installation, "owner": actor["subject"],
                                        "profile_id": actor["profile_id"], "action": action, "pairing": pair,
                                        "pairing_binding": pair["binding"], **context})
        db.execute("DELETE FROM consent_challenges WHERE expires < ?", (time.time() - TTL,))
        db.execute("INSERT INTO consent_challenges (id, message, expires) VALUES (?, ?, ?)", (nonce, message, expires))
    return {"challenge": nonce, "message": message}


def receipt(home: Path, tool, actor: dict, body: dict) -> dict:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if set(body) != {"challenge", "signature"} or not isinstance(body["challenge"], str):
        raise Refusal(400, "Send only a signed phone challenge.")
    signature = decode(body["signature"], 64)
    pairing_changed(home, tool)
    installation = work_grants.identity(work_grants.registry(home))
    retired = []
    with work_grants.registry(home)._db() as db:
        row = db.execute("SELECT * FROM consent_challenges WHERE id = ?", (body["challenge"],)).fetchone()
        if not row or row["used"] or time.time() >= row["expires"]:
            raise Refusal(409, "Phone consent expired or was already used. Refresh; do not resend.")
        context = json.loads(row["message"])
        if (context["owner"] != actor["subject"] or context["profile_id"] != actor["profile_id"]
                or context["pairing"] != pairing(home)
                or context["installation"] != installation):
            raise Refusal(409, "Phone consent belongs to another owner, profile or pairing.")
        try:
            Ed25519PublicKey.from_public_bytes(decode(context["public_key"], 32)).verify(signature, row["message"].encode())
        except InvalidSignature:
            raise Refusal(403, "Phone consent signature did not verify.")
        device = _device(db, actor["profile_id"])
        if context["action"] == "enroll":
            if (device["id"] if device else None) != context["previous"]:
                raise Refusal(409, "Device enrollment changed; refresh first.")
            retired = _retire(db, [device] if device else [], {**actor, "source": "phone_device_replacement"})
            record = {"id": context["device"], "owner": actor["subject"], "profile_id": actor["profile_id"],
                      "public_key": context["public_key"], "pairing": context["pairing"], "enrolled_at": time.time()}
            db.execute("INSERT INTO consent_devices VALUES (?, ?, ?, 1)",
                       (record["id"], actor["profile_id"], work_grants.canonical(record)))
        elif (not device or device["id"] != context["device"] or device["owner"] != actor["subject"]
              or device["public_key"] != context["public_key"]):
            raise Refusal(409, "This phone's enrollment was revoked or replaced.")
        # The valid receipt is committed before admission; no native RPC under this lock.
        db.execute("UPDATE consent_challenges SET used = 1 WHERE id = ? AND used = 0", (body["challenge"],))
    if context["action"] == "enroll":
        return {"ok": True, "device": context["device"], "interrupted": _interrupt(home, tool, retired)}
    signed_actor = {**actor, "source": "phone_consent", "device": context["device"], "challenge": body["challenge"]}
    return work_grants.act(tool, home, context["grant"], context["action"], {"digest": context["digest"]}, signed_actor)
