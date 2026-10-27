"""Your agents' approvals from rooms hosted elsewhere, for Linked Rooms (designs/cross-owner-agents, N5c).

An agent of this installation's in a room another Hermes hosts runs its turns here, through the bridge's member
side (room_bridge_member.py), which watches each run it admitted. A tool call in one that needs its owner's
approval leaves the run waiting, and the home hears only that (decision 7): its grant can't approve, and the
bridge refuses its approval calls. The owner answers here instead: ``waiting`` lists each such approval with its
room, who hosts it (as the Link names them), the agent by its handle there and what it asks, and ``answer``
answers one, once or deny (a room turn's approval is for that call only), through this Hermes' own API server, as
the owner would from their own chat.

Hermes ties a room's run to the grant scope that started it, so this Hermes' API key can't see one: each read
uses the grant the bridge keeps for the run (status), and each answer a grant minted for it alone, of the run's
scope and policy with approve only, for a minute. That one never leaves this process and isn't kept or revoked:
Hermes revokes a scope's grants together, the agent's own with it, so it just runs out. Nothing about an
approval leaves this host.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

from . import room_bridge, room_invites, room_join, room_links

logger = logging.getLogger(__name__)

# The answers a room grant may give (Hermes refuses session and always for one).
CHOICES = ("once", "deny")
WAITING = "waiting_for_approval"
# The grant an answer is made with lives a minute, Hermes' shortest dispatch lifetime.
ANSWER_S = 60
# Hermes' longest approval request id (api_server_runs._handle_run_approval).
MAX_REQUEST_ID = 256
# What an approval shows of its command and description: Conduit's card, never a page of output.
MAX_TEXT_BYTES = 8 * 1024
# Hermes' session store in a profile's home, and how long a read of it waits for a writer.
SESSION_STORE = "state.db"
READ_TIMEOUT_S = 2.0
# A grant's place in a room: the scope Hermes ties a run to.
PLACE = ("room_id", "home_install_id", "authority_gateway_id", "authority_epoch", "member_id", "target_install_id",
         "target_profile")


class ApprovalsError(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class Approvals:
    """The approvals the turns of the installation at ``root``'s agents wait for in rooms other people host.
    ``hermes`` is this Hermes' calls (``Hermes``), and ``link(link_id)`` a Link's record (room_links)."""

    def __init__(self, root: Any, *, hermes: Any = None, link: Optional[Callable[[str], Optional[dict]]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.hermes = hermes if hermes is not None else Hermes(self.root)
        self.link = link if link is not None else room_links.Links(self.root).get
        self.clock = clock

    def waiting(self) -> list[dict[str, Any]]:
        """Each approval waited for, the oldest turn's first: ``run_id`` and ``request_id``, which ``answer``
        takes, ``room_id`` and ``room`` (its name), ``link`` and ``from`` (who hosts it, as the Link names them),
        ``agent`` (``profile``, ``handle`` in the room, ``display_name``), ``session_key``, the session Hermes'
        "Approval needed" notification names for it (``_session_key``), ``description`` and ``command`` as Hermes
        shows them to the agent's owner, and the ``choices``. A turn its Hermes doesn't show waiting is left
        out."""
        runs = sorted(((run_id, run) for run_id, run in self._runs() if run["shown"]["status"] == WAITING),
                      key=lambda item: (item[1]["admitted"], item[0]))
        if not runs:
            return []
        invites = self._invites()
        shown = []
        for run_id, run in runs:
            status, answer = self.hermes.run(run["profile"], run_id, run["grant"])
            if status != 200:
                logger.debug("Linked Rooms couldn't read a room turn's approval (%s)", status or "no answer")
                continue
            place = {**self._place(run, invites), "session_key": self._session_key(run["profile"], run_id, answer)}
            for request_id, prompt in _pending(answer):
                shown.append({"run_id": run_id, "request_id": request_id, **place,
                              "description": prompt["description"], "command": prompt["command"],
                              "choices": list(CHOICES)})
        return shown

    def answer(self, run_id: Any, request_id: Any, choice: Any) -> dict[str, Any]:
        """Answers the approval ``request_id`` that the turn ``run_id`` waits for with ``choice``, once or deny,
        with a grant made for this answer alone. Refused: another choice (``bad_choice``, 400), a request id
        Hermes wouldn't take (``bad_request``, 400), a turn this installation's agents aren't running for a
        room elsewhere (``no_approval``, 404), an approval answered already or a turn that moved on
        (``not_pending``, 409), a turn whose Hermes refuses it, as once its agent is out of the room
        (``refused``, 409), and an API server that doesn't answer (``api_server``, 503)."""
        if not isinstance(choice, str) or choice not in CHOICES:
            raise ApprovalsError("bad_choice", "A room turn's approval is answered once or deny", 400)
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > MAX_REQUEST_ID:
            raise ApprovalsError("bad_request", "That isn't an approval's request id", 400)
        run = dict(self._runs()).get(run_id) if isinstance(run_id, str) else None
        if run is None:
            raise ApprovalsError("no_approval", "None of your agents' turns in a room elsewhere waits for that "
                                                "approval here", 404)
        status, answer = self.hermes.run(run["profile"], run_id, run["grant"])
        _check(status, "read")
        prompt = dict(_pending(answer)).get(request_id)
        if prompt is None:
            raise _not_pending()
        body = {"choice": choice, "request_id": request_id} if prompt["generation"] is None else {
            "request_id": request_id, "execution_generation": prompt["generation"], "choice": choice}
        status, _ = self.hermes.respond(run["profile"], run_id, self._grant_for_answer(run["grant"]), body)
        _check(status, "answer")
        return {"run_id": run_id, "request_id": request_id, "choice": choice}

    def _runs(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """The turns the bridge's member side watches that haven't ended."""
        try:
            runs = room_bridge.Store(self.root).read()["member"]["runs"]
        except room_bridge.BridgeError as exc:
            logger.warning("Linked Rooms couldn't read the room turns it watches: %s", exc)
            raise ApprovalsError(exc.code, "This Hermes couldn't read the room turns it watches; its log says why",
                                 503) from None
        for run_id, run in runs.items():
            if run["kind"] == "run" and run.get("ended") is None and room_bridge.RUN_ID.fullmatch(run_id):
                yield run_id, run

    def _invites(self) -> Mapping[str, Any]:
        try:
            return room_invites.Store(self.root).read()["invites"]
        except room_invites.RoomsError as exc:
            logger.warning("Linked Rooms couldn't read its rooms with people you've linked: %s", exc)
            return {}

    def _place(self, run: Mapping[str, Any], invites: Mapping[str, Any]) -> dict[str, Any]:
        """The run's room, who hosts it and its agent, from the invite it joined (room_invites)."""
        scope = room_bridge.grant_scope(run["grant"]) or {}
        room_id, member_id = scope.get("room_id"), scope.get("member_id")
        invite = invites.get(f"{run['link']}/{room_id}") or {}
        agent = next((agent for agent in (*invite.get("joined", ()), *invite.get("gone", ()))
                      if agent["member_id"] == member_id), None)
        seated = next((member for member in invite.get("members") or () if member["member_id"] == member_id), None)
        try:
            shown = self.link(run["link"])
        except room_links.LinkError as exc:  # who hosts it, then, as the invite named them
            logger.warning("Linked Rooms couldn't read its Links: %s", exc)
            shown = None
        return {"room_id": room_id, "room": invite.get("name"), "link": run["link"],
                "from": shown["name"] if shown else invite.get("from"),
                "agent": {"profile": run["profile"],
                          "handle": (seated or agent or {}).get("handle"),
                          "display_name": (agent or seated or {}).get("display_name")}}

    def _session_key(self, profile: str, run_id: str, answer: Mapping[str, Any]) -> Optional[str]:
        """The session the notification for the run's approval names, which Conduit tells it by: a run store keys
        its approvals by the run (api_server_runs), a session authority by the chat the run is in, as this
        profile's session store keeps it (the hidden chat Hermes made for the agent's place in the room). None
        when the store can't say."""
        if not isinstance(answer.get("pending_controls"), list):
            return run_id
        session_id = answer.get("session_id")
        # Where Hermes keeps a profile (hermes_cli.profiles.get_profile_dir).
        path = (self.root if profile == "default" else self.root / "profiles" / profile) / SESSION_STORE
        if not isinstance(session_id, str) or not session_id or not path.is_file():
            return None
        try:
            connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=READ_TIMEOUT_S)
            try:
                row = connection.execute("SELECT session_key FROM sessions WHERE id = ?", (session_id,)).fetchone()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            logger.debug("Linked Rooms couldn't read a room turn's session key: %s", exc)
            return None
        return row[0] if row and isinstance(row[0], str) and row[0] else None

    def _grant_for_answer(self, grant: str) -> str:
        """A grant for the run's own place and policy that may only approve, for ANSWER_S."""
        try:
            claims = self.hermes.decode(grant, "status")
        except ValueError:  # Hermes' HostedRoomGrantError: past its horizon, no turn waits on it
            raise ApprovalsError("refused", "This agent's place in the room ended", 409) from None
        except room_join.JoinError as exc:
            raise ApprovalsError(exc.code, str(exc), exc.status) from None
        try:
            return self.hermes.issue(grant_id=f"grant-{uuid.uuid4().hex}", **{name: claims[name] for name in PLACE},
                                     execution_policy_digest=claims["execution_policy_digest"],
                                     permissions=("approve",), issued_at=int(self.clock()), ttl_seconds=ANSWER_S,
                                     status_ttl_seconds=ANSWER_S)
        except room_join.JoinError as exc:
            raise ApprovalsError(exc.code, str(exc), exc.status) from None


def _not_pending() -> ApprovalsError:
    return ApprovalsError("not_pending", "That approval was answered already, or its turn moved on", 409)


def _check(status: Optional[int], doing: str) -> None:
    """Raises for what this Hermes' API server answered a read or an answer with, unless it took it."""
    if status is not None and 200 <= status < 300:
        return
    logger.info("Linked Rooms' %s of a room turn's approval got %s from this Hermes' API server", doing,
                status or "no answer")
    if status is None or status >= 500 or status in (408, 429):
        raise ApprovalsError("api_server", "This Hermes' API server didn't answer; check that its gateway runs", 503)
    if status in (404, 409):  # the turn is gone, or its approval answered
        raise _not_pending()
    raise ApprovalsError("refused", "This agent's Hermes refused it; its place in the room may have ended", 409)


def _pending(answer: Any) -> list[tuple[str, dict[str, Any]]]:
    """The approvals a run's status says it waits for, by request id: a gateway with a session authority's
    ``pending_controls`` (each with its execution generation), else the run store's ``approval``."""
    if not isinstance(answer, Mapping):
        return []
    found = []
    controls = answer.get("pending_controls")
    if isinstance(controls, list):
        for prompt in controls:
            if isinstance(prompt, Mapping) and prompt.get("kind") == "approval" \
                    and _request_id(prompt.get("prompt_id")) and type(prompt.get("execution_generation")) is int:
                found.append((prompt["prompt_id"], _shown(prompt, prompt["execution_generation"])))
        return found
    approval = answer.get("approval")
    if answer.get("status") == WAITING and isinstance(approval, Mapping) and _request_id(approval.get("request_id")):
        found.append((approval["request_id"], _shown(approval, None)))
    return found


def _request_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_REQUEST_ID


def _shown(prompt: Mapping[str, Any], generation: Optional[int]) -> dict[str, Any]:
    def text(name: str) -> str:
        value = prompt.get(name)
        return room_bridge.cut(value, MAX_TEXT_BYTES) if isinstance(value, str) else ""

    return {"description": text("description"), "command": text("command"), "generation": generation}


class Hermes(room_join.Hermes):
    """This Hermes' calls for its agents' approvals: the grants, as a join mints them (room_join.Hermes), and its
    API server's runs for a room grant, reached as the bridge's member side reaches it."""

    def run(self, profile: str, run_id: str, grant: str) -> tuple[Optional[int], dict]:
        return self._api._call(profile, "GET", f"/v1/runs/{run_id}", grant)

    def respond(self, profile: str, run_id: str, grant: str, body: dict) -> tuple[Optional[int], dict]:
        return self._api._call(profile, "POST", f"/v1/runs/{run_id}/approval", grant, body=body)


def here(root: Any) -> Approvals:
    """The approvals of the installation at ``root``, with this Hermes' own calls."""
    return Approvals(root)
