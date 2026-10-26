"""Links between installations, for Linked Rooms (designs/cross-owner-agents, layer 1).

A Link is two Hermes installations that know each other's keys and can leave
envelopes in each other's inbox at the rooms mailbox (room_mailbox.py). One
person makes an invite (``Links.invite``): a ``conduit://link/`` URL, also the
QR code's payload, with the inviter's name, keys and mailbox and a token good
for one accept within 24 hours. The other person's Conduit shows the name and
the fingerprint words from it (``preview``), and accepting it (``accept``)
leaves a sealed link.accept in the inviter's inbox with a send token for the
acceptor's own. The inviter's Hermes answers with a link.confirm carrying a
send token for its inbox, and both sides are linked. Either side can unlink,
and the other side's Hermes drops it when its link.remove arrives: an acceptor
that unlinks while it waits sends it in answer to the confirm, if one comes.

One loop an installation reads the inbox (``LinkLoop``): whichever process
holds ``conduit-rooms/.lease``. Everything a Link needs is in
``conduit-rooms/links.json`` beside the identity, owner only; its tokens never
leave it. No mailbox call happens under its lock: what's to be done is
recorded first and the mailbox calls follow, each safe to make again and made
by one process at a time, so a crash between them only means they're made on
the next pass. Envelopes of other kinds go to the handlers registered with
``handles``; one of a kind nothing here handles is dropped.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # everything but Windows
    msvcrt = None  # type: ignore[assignment]

from . import room_envelope, room_identity, room_mailbox, sealing

logger = logging.getLogger(__name__)

DIRECTORY = room_identity.DIRECTORY
FILE_NAME = "links.json"
LEASE_NAME = ".lease"
SEND_LOCK_NAME = ".send.lock"
VERSION = 1
URL_PREFIX = "conduit://link/"
ENV_URL = "CONDUIT_ROOMS_MAILBOX_URL"
ENV_REGISTRATION = "CONDUIT_ROOMS_MAILBOX_REGISTRATION"

INVITE_TTL_S = 24 * 3600
# Past this, with a day's slack for its maker's clock, an invite can't be one
# this plugin made.
LONGEST_INVITE_S = 2 * INVITE_TTL_S
# What the mailbox holds an envelope for. A Link still waiting a day past it
# was never answered: its accept or its confirm expired there.
HOLD_S = 7 * 86400
UNANSWERED_S = HOLD_S + 86400
# The mailbox takes 64 senders an inbox: one a Link, one a waiting invite.
MAX_INVITES = 10
MAX_LINKS = 50
NAME_MAX = 64
# Message ids remembered a Link, for an envelope sent again after it was read.
MAX_SEEN = 512
# While nothing is waiting, how often the loop looks at links.json, so it
# soon sees an invite the CLI made; and the longest it waits after a failed
# pass.
IDLE_S = 2
MAX_BACKOFF_S = 300
MAX_PAGES = 20
# A handler that fails is tried on this many passes, then the envelope is dropped.
HANDLER_TRIES = 3

ACCEPT, CONFIRM, REMOVE = "link.accept", "link.confirm", "link.remove"

_ID = re.compile(r"^[A-Za-z0-9_-]{1,48}\Z")
_KEY = re.compile(r"^[A-Za-z0-9_-]{43}\Z")
_INVITE_FIELDS = frozenset({"v", "name", "sign", "seal", "mailbox", "invite", "token", "exp"})
_ACCEPT_FIELDS = frozenset({"sign", "seal", "name", "mailbox", "token", "invite", "invite_token"})
_thread_lock = threading.Lock()
_send_lock = threading.Lock()
_warned_url: set[tuple[str, str]] = set()

# kind -> handler(Received); the bridge and rooms register theirs with ``handles``.
HANDLERS: dict[str, Callable[["Received"], None]] = {}


class LinkError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class AcceptKept(LinkError):
    """An accept the mailbox didn't take yet: its Link waits, and the loop
    tries it again while the invite lasts."""


@dataclass(frozen=True)
class Invite:
    """What an invite URL holds."""

    name: str
    sign: bytes
    seal: bytes
    mailbox: str
    id: str
    token: str = field(repr=False)
    expires_at: int


@dataclass(frozen=True)
class Received:
    """An envelope from a linked installation, for its kind's handler."""

    link_id: str
    kind: str
    message_id: str
    at: int
    body: dict


def handles(kind: str) -> Callable[[Callable[[Received], None]], Callable[[Received], None]]:
    """Registers a handler for envelopes of ``kind``. Handling one twice must
    be harmless: an envelope can come again after a crash."""
    if not isinstance(kind, str) or not room_envelope.KIND.match(kind) or kind.startswith("link."):
        raise ValueError(f"{kind!r} isn't a kind a handler can take")

    def register(handler: Callable[[Received], None]) -> Callable[[Received], None]:
        HANDLERS[kind] = handler
        return handler

    return register


def parse_invite(url: Any, now: Optional[int] = None) -> Invite:
    """The invite in a ``conduit://link/`` URL. Raises LinkError("bad_invite")
    for anything else, and, given ``now``, for one that says it lasts longer
    than invites do."""
    refused = LinkError("bad_invite", "That isn't a Conduit link invite")
    if not isinstance(url, str):
        raise refused
    url = url.strip()
    if len(url) > 2048 or not url.startswith(URL_PREFIX):
        raise refused
    try:
        fields = json.loads(sealing.unb64u(url[len(URL_PREFIX):], LinkError))
    except (LinkError, ValueError, RecursionError):  # bytes that aren't UTF-8 included
        raise refused from None
    if not isinstance(fields, dict) or set(fields) != _INVITE_FIELDS or type(fields["v"]) is not int \
            or fields["v"] != VERSION:
        raise refused
    name, sign, seal = _checked_name(fields["name"]), _public_key(fields["sign"]), _public_key(fields["seal"])
    mailbox = _canonical_mailbox(fields["mailbox"])
    invite_id, token, expires_at = fields["invite"], fields["token"], fields["exp"]
    if name is None or sign is None or seal is None or mailbox is None or not _is_id(invite_id) \
            or not _is_token(token) or not _whole(expires_at):
        raise refused
    if now is not None and expires_at > now + LONGEST_INVITE_S:
        raise LinkError("bad_invite", "That invite says it lasts longer than invites do; the clock where it was "
                                      "made may be wrong")
    return Invite(name=name, sign=sign, seal=seal, mailbox=mailbox, id=invite_id, token=token,
                  expires_at=expires_at)


class Links:
    """This installation's Links, kept in ``<root>/conduit-rooms/links.json``.
    ``clock`` dates invites and Links; the mailbox's requests carry the real
    time, which its signatures need."""

    def __init__(self, root: Any, *, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.clock = clock
        self._store = _Store(self.root)

    # --- What the routes and the CLI call --------------------------------------

    def invite(self, name: Any) -> dict[str, Any]:
        """A new invite: ``{url, invite: {id, name, expires_at}, words}``,
        ``words`` this Hermes' own. ``name`` is who the other person sees
        it's from."""
        name = _name(name)
        identity = self._identity()
        # First, so a plugin install that can't show them makes no invite.
        words = _words(identity.signing_public, identity.sealing_public)
        now = int(self.clock())
        invite_id, token = _new_id(9), room_mailbox.new_token()
        with self._store.update() as state:
            if _waiting_invites(state, now) >= MAX_INVITES:
                raise LinkError("too_many_invites", f"{MAX_INVITES} invites are already waiting; cancel one, or "
                                                    "wait for one to be used or run out", 409)
            if _places_held(state) >= MAX_LINKS:
                raise _too_many_links(state, now)
            url = self._mailbox_url(state)
            state["invites"][invite_id] = {"name": name, "token": token, "created_at": now,
                                           "expires_at": now + INVITE_TTL_S}
        called = False
        try:
            mailbox = self._mailbox(identity, url)
            called = True  # a new inbox gets every sender this Hermes gave out, this invite's too
            self._registered(mailbox)
            self._owner_call(mailbox, lambda: mailbox.put_sender(_invite_sender(invite_id), token))
        except (room_mailbox.MailboxError, LinkError) as exc:
            with self._store.update() as state:
                state["invites"].pop(invite_id, None)
                if called:
                    # The mailbox may have put its sender without answering.
                    _clean_up(state, _invite_sender(invite_id), int(self.clock()))
            if isinstance(exc, room_mailbox.MailboxError) and not exc.retryable:
                # It answered, so the senders still to go go now, as after a
                # refused accept: an inbox with as many as the mailbox keeps
                # takes this one when it's asked again.
                self._flush_quietly()
            raise _link_error(exc) from None
        expires_at = now + INVITE_TTL_S
        payload = {"v": VERSION, "name": name, "sign": sealing.b64u(identity.signing_public),
                   "seal": sealing.b64u(identity.sealing_public), "mailbox": mailbox.url, "invite": invite_id,
                   "token": token, "exp": expires_at}
        encoded = sealing.b64u(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        return {"url": URL_PREFIX + encoded, "invite": {"id": invite_id, "name": name, "expires_at": expires_at},
                "words": words}

    def words(self) -> dict[str, Any]:
        """This installation's fingerprint words, or None before its first
        invite or accept makes its keys."""
        try:
            identity = room_identity.load(self.root)
        except room_identity.IdentityError as exc:
            raise _link_error(exc) from None
        return {"words": None if identity is None else _words(identity.signing_public, identity.sealing_public),
                "crypto": sealing.crypto_available()}

    def preview(self, url: Any) -> dict[str, Any]:
        """What an invite says, before it's accepted: who it's from and their
        fingerprint words, the host of the mailbox it names, whether it ran
        out, whether it's this Hermes' own, and the Link this Hermes already
        has with them, if any. An invite whose mailbox this Hermes won't
        call is refused, as accepting it would be."""
        now = int(self.clock())
        invite = parse_invite(url, now)
        try:
            identity = room_identity.load(self.root)
        except room_identity.IdentityError as exc:
            raise _link_error(exc) from None
        state = self._store.read()
        if _peer_mailbox(invite.mailbox, self._mailbox_url(state)) is None:
            raise _mailbox_refused()
        return {"name": invite.name, "words": _words(invite.sign, invite.seal),
                "mailbox": room_mailbox.url_host(invite.mailbox), "expires_at": invite.expires_at,
                "expired": invite.expires_at <= now,
                "own": identity is not None and identity.signing_public == invite.sign,
                "linked_as": _linked_with(state, invite.sign, now)}

    def accept(self, url: Any, name: Any) -> dict[str, Any]:
        """Accepts an invite, as ``name``: ``{link}``, waiting until the
        inviter's Hermes confirms it. The same invite again, while that Link
        waits, is the same accept, sent now if it hasn't gone. An accept the
        mailbox didn't answer, or couldn't take yet, stays with its Link, and
        goes on a later pass while the invite lasts."""
        now = int(self.clock())
        invite = parse_invite(url, now)
        name = _name(name)
        identity = self._identity()
        # First, so a plugin install that can't show the Link makes no accept.
        _words(invite.sign, invite.seal)
        if identity.signing_public == invite.sign:
            raise LinkError("own_invite", "That invite is from this Hermes: it's for the person you're linking with")
        if invite.expires_at <= now:
            raise LinkError("expired", "That invite has run out; ask for a new one", 410)
        link_id, my_token = _new_id(12), room_mailbox.new_token()
        with self._store.update() as state:
            url_mine = self._mailbox_url(state)
            if _peer_mailbox(invite.mailbox, url_mine) is None:
                raise _mailbox_refused()
            again = _accepted(state, invite, now)
            if again is not None:
                link_id = again
                # Unlinked while it waited: accepted again, it waits again.
                state["links"][again].pop("leaving", None)
            else:
                for other_id, other in list(state["links"].items()):
                    if _key_of(other, "sign") == invite.sign and not other.get("leaving"):
                        if _status(other, now) != "unanswered":
                            raise LinkError("already_linked", f"This Hermes is already linked with {other['name']}; "
                                                              "unlink first to link again", 409)
                        _forget(state, other_id, now)
                if _places_held(state) >= MAX_LINKS:
                    raise _too_many_links(state, now)
                link = state["links"][link_id] = {
                    "name": invite.name, "sign": sealing.b64u(invite.sign), "seal": sealing.b64u(invite.seal),
                    "inbox": room_identity.inbox_id(invite.sign), "mailbox": invite.mailbox, "status": "waiting",
                    "role": "acceptor", "send_token": "", "my_token": my_token, "sender_up": False,
                    "created_at": now, "invite": invite.id, "seen": {},
                }
                # Recorded with the Link, so an accept cut off on its way goes
                # on the loop's next pass. It goes with the invite's token, and
                # not once the invite has run out.
                body = {"sign": sealing.b64u(identity.signing_public), "seal": sealing.b64u(identity.sealing_public),
                        "name": name, "mailbox": url_mine, "token": my_token, "invite": invite.id,
                        "invite_token": invite.token}
                state["outbox"].append(dict(_outgoing(ACCEPT, link_id, link, body, now), token=invite.token,
                                            until=invite.expires_at))
        try:
            mailbox = self._mailbox(identity, url_mine)
            self._registered(mailbox)
            self._send_accept(identity, mailbox, link_id)
        except (room_mailbox.MailboxError, room_envelope.EnvelopeError, LinkError) as exc:
            # A refusal takes back an accept that can't have reached the
            # inviter: never sent, or refused each time. One the mailbox
            # didn't answer may have, and one it couldn't take yet may go
            # later, as on the loop's pass, so those stay for the loop, unless
            # the invite is used.
            used = getattr(exc, "code", "") == "invite_used"
            waits = isinstance(exc, room_mailbox.MailboxError) and _goes_later(exc)
            if used or (not waits and not _accept_went(self._store.read(), link_id)):
                self._take_back(link_id)
            elif waits:
                raise AcceptKept(exc.code, f"{exc}. This Hermes keeps the accept and tries it again while the "
                                           "invite lasts", 503) from None
            raise _link_error(exc) from None
        shown = self.get(link_id)
        if shown is None:  # a pass found the invite used while this waited
            raise _invite_used()
        return {"link": shown}

    def view(self) -> dict[str, Any]:
        """The Links and the invites still waiting, with no token or key."""
        state = self._store.read()
        now = int(self.clock())
        links = sorted((_shown(link_id, link, now) for link_id, link in state["links"].items()
                        if not link.get("leaving")), key=lambda link: (link["name"].casefold(), link["id"]))
        invites = sorted(({"id": invite_id, "name": invite["name"], "expires_at": invite["expires_at"]}
                          for invite_id, invite in state["invites"].items() if invite["expires_at"] > now),
                         key=lambda invite: (invite["expires_at"], invite["id"]))
        try:
            url = self._mailbox_url(state)
        except LinkError:  # a setting that isn't a mailbox's URL, shown as it is
            url = self._configured_url()
        return {"links": links, "invites": invites, "mailbox": {"url": url, "reading": reading(self.root)}}

    def get(self, link_id: Any) -> Optional[dict[str, Any]]:
        """One Link as ``view`` shows it, or None."""
        link = self._store.read()["links"].get(link_id) if isinstance(link_id, str) else None
        return None if link is None or link.get("leaving") else _shown(link_id, link, int(self.clock()))

    def remove(self, link_id: Any) -> dict[str, bool]:
        """Unlinks: the Link goes here at once, and the other side's Hermes
        drops it when the link.remove arrives (sent later if it can't go now).
        One still waiting for its confirm, whose accept may have reached the
        inviter, goes from sight at once: its confirm, if it comes, is
        answered with the link.remove."""
        if not isinstance(link_id, str):
            return {"removed": False}
        now = int(self.clock())
        with self._store.update() as state:
            link = state["links"].get(link_id)
            if link is None or link.get("leaving"):
                return {"removed": False}
            if link["send_token"]:
                _forget(state, link_id, now)
                state["outbox"].append(_outgoing(REMOVE, link_id, link, {}, now))
            elif _status(link, now) == "waiting" and _accept_went(state, link_id):
                # Until then this Hermes has no token for the inviter's inbox.
                # Its sender stays for the confirm, which the inviter's Hermes
                # takes as linked once the mailbox has it.
                link["leaving"] = True
            else:
                _forget(state, link_id, now)
        self._flush_quietly()
        return {"removed": True}

    def cancel(self, invite_id: Any) -> dict[str, bool]:
        """Takes an invite back before it's used."""
        if not isinstance(invite_id, str):
            return {"cancelled": False}
        with self._store.update() as state:
            if state["invites"].pop(invite_id, None) is None:
                return {"cancelled": False}
            _clean_up(state, _invite_sender(invite_id), int(self.clock()))
        self._flush_quietly()
        return {"cancelled": True}

    def send(self, link_id: str, kind: str, body: dict, message_id: Optional[str] = None) -> str:
        """Leaves an envelope of ``kind`` for the other side of a Link: its
        message id. The same message id again while the first waits stores no
        copy, and the other side handles it once."""
        link = self._store.read()["links"].get(link_id) if isinstance(link_id, str) else None
        if link is None or link["status"] != "linked":
            raise LinkError("not_linked", "This Hermes isn't linked with that one", 404)
        if not isinstance(kind, str) or not room_envelope.KIND.match(kind) or kind.startswith("link."):
            raise LinkError("bad_kind", "That isn't a kind of envelope a Link carries")
        message_id = message_id or room_envelope.new_message_id()
        try:
            data = room_envelope.seal(self._identity(), link=link_id, recipient_sign=_key_of(link, "sign"),
                                      recipient_seal=_key_of(link, "seal"), message_id=message_id, kind=kind,
                                      body=body, clock=self.clock)
            room_mailbox.send(link["mailbox"], link["inbox"], link["send_token"], data, idempotency_key=message_id)
        except (room_mailbox.MailboxError, room_envelope.EnvelopeError) as exc:
            raise _link_error(exc) from None
        return message_id

    def mailbox(self) -> room_mailbox.Mailbox:
        """This installation's own inbox."""
        return self._mailbox(self._identity(), self._mailbox_url(self._store.read()))

    # --- The mailbox calls the store says are waiting ---------------------------

    def flush(self) -> None:
        """Makes the mailbox calls the store has waiting: senders to delete,
        senders to put for new Links, envelopes to send. What fails stays for
        the next time, up to the mailbox's 7-day hold (an accept, until its
        invite runs out), unless the mailbox refused it as it is."""
        state = self._store.read()
        if not (any(not link.get("sender_up") for link in state["links"].values())
                or state["outbox"] or state["cleanup"]):
            return
        identity = self._identity()
        mailbox = self._mailbox(identity, self._mailbox_url(state))
        with self._sending():
            state = self._store.read()  # what's left now no other pass can be sending it
            # A mailbox that doesn't answer gets no more calls this pass: each
            # waits out its timeout, with the send lock held.
            down: set[str] = set()
            # Old senders go first: an inbox with as many as the mailbox keeps
            # takes a new one once they have.
            for item in state["cleanup"]:
                if mailbox.url in down:
                    break
                try:
                    mailbox.delete_sender(item["sender"])
                except room_mailbox.MailboxError as exc:
                    if exc.code == "unreachable":
                        down.add(mailbox.url)
                    # Unauthorized: the inbox is gone, and its senders with it.
                    if _goes_later(exc) and int(self.clock()) < item["until"]:
                        logger.info("Linked Rooms will remove a sender from its inbox later (%s)", exc.code)
                        continue
                with self._store.update() as current:
                    current["cleanup"] = [other for other in current["cleanup"] if other["sender"] != item["sender"]]
            for link_id, link in state["links"].items():
                if not link.get("sender_up") and mailbox.url not in down:
                    self._owner_call(mailbox, lambda: mailbox.put_sender(_link_sender(link_id), link["my_token"]))
                    self._mark_sender_up(link_id)
            for item in state["outbox"]:
                if item["mailbox"] not in down:
                    self._deliver(identity, item, down)

    @contextmanager
    def _sending(self) -> Iterator[None]:
        """Held while what the store has waiting is sent: one thread of one
        process at a time, so an accept and the loop's pass never both send
        the same envelope. flush takes it, so it's never held around one."""
        directory = self.root / DIRECTORY
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _send_lock, open(directory / SEND_LOCK_NAME, "a+b") as handle, room_identity._exclusive(handle):
            yield

    def _send_accept(self, identity: room_identity.Identity, mailbox: room_mailbox.Mailbox, link_id: str) -> None:
        """Puts a new Link's sender and sends its accept, unless a pass did
        while this waited. Raises LinkError("invite_used") when the
        inviter's mailbox refuses its first try."""
        with self._sending():
            state = self._store.read()
            link = state["links"].get(link_id)
            item = next((item for item in state["outbox"] if item["id"] == f"accept.{link_id}"), None)
            if link is None or item is None:
                return
            if not link.get("sender_up"):
                # Up before the accept goes, so the confirm has somewhere to land.
                self._owner_call(mailbox, lambda: mailbox.put_sender(_link_sender(link_id), link["my_token"]))
                self._mark_sender_up(link_id)
            earlier = bool(item.get("tried"))
            try:
                self._send_outgoing(identity, item)
            except room_mailbox.MailboxError as exc:
                if exc.code != "unauthorized":
                    raise
                if not earlier:
                    raise _invite_used() from None
                # An earlier try may have reached the inviter, whose Hermes
                # then used the invite answering it: the Link waits for that.
                self._give_up(item, int(self.clock()), exc.code, keep_link=True)
                return
            self._drop_outgoing(item)

    def _deliver(self, identity: room_identity.Identity, item: dict[str, Any], down: set[str]) -> None:
        now = int(self.clock())
        # An accept tried before without an answer may have reached the
        # inviter, whose Hermes then used the invite answering it, so its Link
        # waits for the confirm (and reads unanswered after 8 days without
        # one). Never sent, or refused each time it went, it can't have.
        earlier = bool(item.get("tried"))
        if item["kind"] in (ACCEPT, CONFIRM):
            link = self._store.read()["links"].get(item["link"])
            if link is None:
                self._drop_outgoing(item)
                return
            if not link.get("sender_up"):
                return  # it hands out a token for a sender that isn't up yet
            if item["kind"] == ACCEPT and now >= item["until"]:
                self._give_up(item, now, "its invite ran out", keep_link=earlier)
                return
        try:
            self._send_outgoing(identity, item)
        except (LinkError, room_envelope.EnvelopeError) as exc:
            # What's stored for it can't make an envelope (links.json edited
            # by hand), and never will. The rest still go.
            self._give_up(item, now, exc.code, keep_link=item["kind"] == ACCEPT and earlier)
            return
        except room_mailbox.MailboxError as exc:
            if exc.code == "unreachable":
                down.add(item["mailbox"])
            # Unauthorized: the other side took its sender back, or lost its
            # inbox. One the mailbox lost (not_found) is made again by its
            # Hermes, with the senders it gave out.
            if _goes_later(exc) and now < item["until"]:
                logger.info("Linked Rooms will send a %s for Link %s later (%s)", item["kind"], item["link"],
                            exc.code)
                return
            self._give_up(item, now, exc.code, keep_link=item["kind"] == ACCEPT and earlier)
            return
        with self._store.update() as state:
            state["outbox"] = [other for other in state["outbox"] if other["id"] != item["id"]]
            link = state["links"].get(item["link"])
            if item["kind"] == CONFIRM and link is not None:
                link["status"] = "linked"

    def _send_outgoing(self, identity: room_identity.Identity, item: dict[str, Any]) -> None:
        data = room_envelope.seal(identity, link=item["link"], recipient_sign=_key_of(item, "sign"),
                                  recipient_seal=_key_of(item, "seal"), message_id=item["id"],
                                  kind=item["kind"], body=item["body"], clock=self.clock)
        first = item["kind"] == ACCEPT and not item.get("tried")
        if first:
            # From here it may reach the inviter's mailbox. Unlinked before
            # it was tried, it's gone, and it never goes.
            with self._store.update() as state:
                queued = next((queued for queued in state["outbox"] if queued["id"] == item["id"]), None)
                if queued is None:
                    return
                queued["tried"] = True
        try:
            room_mailbox.send(item["mailbox"], item["inbox"], item["token"], data, idempotency_key=item["id"])
        except room_mailbox.MailboxError as exc:
            if first and _refused(exc):
                # The mailbox said it didn't take it, so it's still untried:
                # the inviter can't have it.
                with self._store.update() as state:
                    for queued in state["outbox"]:
                        if queued["id"] == item["id"]:
                            queued.pop("tried", None)
            raise

    def _give_up(self, item: dict[str, Any], now: int, why: str, *, keep_link: bool = False) -> None:
        with self._store.update() as state:
            state["outbox"] = [other for other in state["outbox"] if other["id"] != item["id"]]
            if item["kind"] in (ACCEPT, CONFIRM) and item["link"] in state["links"] and not keep_link:
                # Refused: the acceptor took its side back before this Hermes
                # answered, or the invite was used or ran out. Or it never
                # could be sent.
                _forget(state, item["link"], now)
        logger.info("Linked Rooms dropped a %s for Link %s (%s)", item["kind"], item["link"], why)

    def _drop_outgoing(self, item: dict[str, Any]) -> None:
        with self._store.update() as state:
            state["outbox"] = [other for other in state["outbox"] if other["id"] != item["id"]]

    def _flush_quietly(self) -> None:
        try:
            self.flush()
        except (room_mailbox.MailboxError, room_envelope.EnvelopeError, room_identity.IdentityError, LinkError,
                OSError) as exc:
            logger.info("Linked Rooms will finish with the mailbox later (%s)",
                        getattr(exc, "code", type(exc).__name__))

    # --- Helpers ----------------------------------------------------------------

    def _identity(self) -> room_identity.Identity:
        try:
            return room_identity.load_or_create(self.root)
        except room_identity.IdentityError as exc:
            raise _link_error(exc) from None

    def _configured_url(self) -> str:
        return (os.environ.get(ENV_URL) or "").strip() or room_mailbox.DEFAULT_URL

    def _mailbox_url(self, state: dict[str, Any]) -> str:
        # Every Link and invite names this installation's mailbox, and the
        # senders still to delete are there, so while any is left the mailbox
        # they were made with stays.
        stored = state["mailbox"].get("url")
        configured = self._configured_url()
        if stored and (state["links"] or state["invites"] or state["cleanup"]):
            if configured != stored and (str(self.root), configured) not in _warned_url:
                _warned_url.add((str(self.root), configured))
                logger.warning("Linked Rooms keeps the mailbox at %s, which this installation's Links name; %s "
                               "takes effect only with no Links or invites", stored, ENV_URL)
            return stored
        try:
            return room_mailbox.base_url(configured)
        except room_mailbox.MailboxError:
            raise LinkError("bad_mailbox", f"{ENV_URL} isn't a mailbox's URL (https://, with nothing after its "
                                           "host name)", 500) from None

    def _mailbox(self, identity: room_identity.Identity, url: str) -> room_mailbox.Mailbox:
        try:
            return room_mailbox.Mailbox(url, identity,
                                        registration_token=(os.environ.get(ENV_REGISTRATION) or "").strip() or None)
        except room_mailbox.MailboxError as exc:
            raise _link_error(exc) from None

    def _registered(self, mailbox: room_mailbox.Mailbox, *, force: bool = False) -> None:
        """Makes this installation's inbox, or confirms it, the first time
        the store doesn't know it, and again after one went (90 idle days, a
        reset): then every sender this Hermes gave out goes back in, and the
        inbox is read from the start."""
        state = self._store.read()
        known = state["mailbox"]
        if not force and known.get("url") == mailbox.url and known.get("inbox") == mailbox.identity.inbox:
            return
        mailbox.register()
        senders = _senders(state, int(self.clock()))
        for sender, token in senders:
            mailbox.put_sender(sender, token)
        put = {sender for sender, _ in senders}
        with self._store.update() as state:
            state["mailbox"] = {"url": mailbox.url, "inbox": mailbox.identity.inbox, "after": 0}
            for link_id, link in state["links"].items():
                if _link_sender(link_id) in put:
                    link["sender_up"] = True

    def _owner_call(self, mailbox: room_mailbox.Mailbox, call: Callable[[], Any]) -> Any:
        try:
            return call()
        except room_mailbox.MailboxError as exc:
            if exc.code != "unauthorized":
                raise
        # The inbox isn't there any more: made again, with its senders.
        self._registered(mailbox, force=True)
        return call()

    def _mark_sender_up(self, link_id: str) -> None:
        with self._store.update() as state:
            link = state["links"].get(link_id)
            if link is not None:
                link["sender_up"] = True

    def _take_back(self, link_id: str) -> None:
        with self._store.update() as state:
            if link_id in state["links"]:
                _forget(state, link_id, int(self.clock()))
        self._flush_quietly()


class LinkLoop:
    """Reads this installation's inbox and makes the mailbox calls waiting in
    links.json. Only the process holding the lease reads; ``start`` runs it
    in a daemon thread, the tests call ``poll_once``."""

    def __init__(self, root: Any, *, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.clock = clock
        self.links = Links(self.root, clock=clock)
        self._lock = threading.Lock()
        self._lease: Any = None
        self._stopped = threading.Event()
        self._woken = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._failures: dict[str, int] = {}
        self._looked: Optional[tuple[int, int]] = None
        self._idle = True

    def lease(self) -> bool:
        """Takes the installation's lease, or keeps it: False while another
        loop, in this process or another, has it."""
        with self._lock:
            if self._lease is not None:
                return True
            directory = self.root / DIRECTORY
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle = open(directory / LEASE_NAME, "a+b")
            if not _try_lock(handle):
                handle.close()
                return False
            self._lease = handle
            return True

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stopped.is_set()

    def wake(self) -> None:
        """Looks at links.json now rather than after its rest: a route that
        made an invite or a Link calls it."""
        self._woken.set()

    def stop(self) -> None:
        # Only tests stop a loop: in Hermes it ends with its process. A pass
        # still running may finish after the lease goes, alongside another
        # loop's, which handlers allow (an envelope can come again).
        self._stopped.set()
        self._woken.set()
        with self._lock:
            if self._lease is not None:
                _unlock(self._lease)
                self._lease.close()
                self._lease = None

    def start(self) -> "LinkLoop":
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stopped.clear()
                self._thread = threading.Thread(target=self._run, name="conduit-room-links", daemon=True)
                self._thread.start()
        return self

    def poll_once(self, wait: int = 0) -> bool:
        """One pass: reads what's in the inbox, oldest first, then makes the
        mailbox calls waiting in the store. False when another loop holds the
        lease."""
        if not self.lease():
            return False
        links = self.links
        state = links._store.read()
        if not _has_work(state):
            return True
        identity = links._identity()
        mailbox = links._mailbox(identity, links._mailbox_url(state))
        links._registered(mailbox)
        drained = self._read(identity, mailbox, wait)
        links.flush()
        if drained and self._run_out():
            links.flush()
        return True

    def _run(self) -> None:
        backoff = 0
        while not self._stopped.is_set():
            try:
                if self._nothing_waiting() or not self.lease():
                    self._rest(IDLE_S)
                    continue
                self.poll_once(wait=room_mailbox.MAX_WAIT_S)
                backoff = 0
            except Exception as exc:  # noqa: BLE001 - the loop outlives any one pass
                backoff = min(MAX_BACKOFF_S, max(5, backoff * 2))
                logger.warning("Linked Rooms couldn't finish reading the mailbox (%s); trying again in %d s",
                               getattr(exc, "code", type(exc).__name__), backoff)
                self._rest(backoff)

    def _rest(self, seconds: float) -> None:
        # Cleared only when it woke it: a wake as the rest times out holds
        # for the next one.
        if self._woken.wait(seconds):
            self._woken.clear()

    def _nothing_waiting(self) -> bool:
        # Only links.json's size and time are read while it says nothing waits.
        try:
            stat = self.links._store.path.stat()
        except FileNotFoundError:
            return True
        looked = (stat.st_mtime_ns, stat.st_size)
        if looked != self._looked:
            self._looked, self._idle = looked, not _has_work(self.links._store.read())
        return self._idle

    def _read(self, identity: room_identity.Identity, mailbox: room_mailbox.Mailbox, wait: int) -> bool:
        """Reads the inbox a page at a time: True once a read found it empty."""
        store = self.links._store
        after = store.read()["mailbox"].get("after", 0)
        for page_number in range(MAX_PAGES):
            try:
                page, _ = mailbox.fetch(after=after, wait=wait if page_number == 0 else 0)
            except room_mailbox.MailboxError as exc:
                if exc.code != "unauthorized":
                    raise
                # The inbox is gone (90 idle days, or the mailbox was reset).
                self.links._registered(mailbox, force=True)
                after = 0
                page, _ = mailbox.fetch(after=after)
            if not page:
                return True
            last = after
            for delivered in page:
                if not self._handle(identity, delivered):
                    break
                last = delivered.seq
            if last != after:
                mailbox.ack(through=last)
                with store.update() as state:
                    if state["mailbox"].get("inbox") == identity.inbox:
                        state["mailbox"]["after"] = last
                after = last
            if last != page[-1].seq:
                return False
        return False

    def _handle(self, identity: room_identity.Identity, delivered: room_mailbox.Delivered) -> bool:
        """Handles one envelope: False to read it again on the next pass."""
        try:
            header = room_envelope.peek(delivered.body)
        except room_envelope.EnvelopeError as exc:
            logger.info("Linked Rooms dropped something in its inbox that isn't an envelope (%s)", exc.code)
            return True
        try:
            if delivered.sender.startswith("i-"):
                self._on_accept(identity, delivered, header)
                return True
            if delivered.sender == _link_sender(header.link):
                return self._on_link(identity, delivered, header)
        except (ValueError, TypeError, LookupError, AttributeError) as exc:
            # Something in it this plugin didn't expect: it goes, rather than
            # hold up everything behind it. Only the error's type is logged,
            # as its message can quote the envelope.
            logger.warning("Linked Rooms dropped a %s it couldn't handle (%s)", header.kind, type(exc).__name__)
            return True
        logger.info("Linked Rooms dropped an envelope whose Link isn't its sender's")
        return True

    def _on_accept(self, identity: room_identity.Identity, delivered: room_mailbox.Delivered,
                   header: room_envelope.Header) -> None:
        invite_id = delivered.sender[2:]
        if header.kind != ACCEPT:
            logger.info("Linked Rooms dropped a %s sent with an invite", header.kind)
            return
        try:
            opened = room_envelope.open_envelope(identity, delivered.body)
        except room_envelope.EnvelopeError as exc:
            logger.info("Linked Rooms dropped an accept that didn't check (%s)", exc.code)
            return
        accept = _accept_fields(opened.body)
        if accept is None or accept["invite"] != invite_id:
            logger.info("Linked Rooms dropped an accept that isn't one")
            return
        now, link_id = int(self.clock()), header.link
        with self.links._store.update() as state:
            link = state["links"].get(link_id)
            if link is not None:
                # Read again after a crash: what it asked for is already done.
                if link.get("invite") != invite_id or _key_of(link, "sign") != accept["sign"]:
                    logger.info("Linked Rooms dropped an accept for a Link id it already has")
                return
            invite = state["invites"].get(invite_id)
            if invite is None:
                logger.info("Linked Rooms dropped an accept for an invite that's been used or taken back")
                return
            if not hmac.compare_digest(accept["invite_token"].encode("utf-8"), invite["token"].encode("utf-8")):
                logger.info("Linked Rooms dropped an accept without its invite's token")
                return
            # The mailbox's time, not this host's: an inviter whose Hermes was
            # off still answers an accept that came in time.
            if delivered.received_at > invite["expires_at"]:
                logger.info("Linked Rooms dropped an accept that came after its invite ran out")
                return
            mine = self.links._mailbox_url(state)
            mailbox_url = _peer_mailbox(accept["mailbox"], mine)
            if mailbox_url is None:
                logger.info("Linked Rooms dropped an accept naming a mailbox on someone's own computer or network")
                return
            # Someone already linked from that Hermes lost their side, or took
            # it back, and linked again: this Link replaces theirs. One this
            # Hermes waits on as the acceptor stays: their invite and this one
            # crossed, and their Hermes keeps both Links too.
            for other_id, other in list(state["links"].items()):
                if _key_of(other, "sign") == accept["sign"] \
                        and not (other["role"] == "acceptor" and _status(other, now) == "waiting"):
                    _forget(state, other_id, now)
            if len(state["links"]) >= MAX_LINKS:
                logger.warning("Linked Rooms dropped an accept: this Hermes has %d Links", MAX_LINKS)
                return
            my_token = room_mailbox.new_token()
            link = state["links"][link_id] = {
                "name": accept["name"], "sign": sealing.b64u(accept["sign"]), "seal": sealing.b64u(accept["seal"]),
                "inbox": room_identity.inbox_id(accept["sign"]), "mailbox": mailbox_url, "status": "waiting",
                "role": "inviter", "send_token": accept["token"], "my_token": my_token, "sender_up": False,
                "created_at": now, "invite": invite_id, "seen": {},
            }
            del state["invites"][invite_id]
            # The flush deletes the invite's sender, with anything else left
            # with it (this accept is acked by then), then puts the Link's
            # own and sends the confirm.
            # With this Hermes' mailbox: the invite's could have been changed on
            # its way, and the confirm is signed.
            state["outbox"].append(_outgoing(CONFIRM, link_id, link, {"token": my_token, "mailbox": mine}, now))
            _clean_up(state, _invite_sender(invite_id), now)

    def _on_link(self, identity: room_identity.Identity, delivered: room_mailbox.Delivered,
                 header: room_envelope.Header) -> bool:
        store, link_id = self.links._store, header.link
        link = store.read()["links"].get(link_id)
        if link is None:
            logger.info("Linked Rooms dropped an envelope for a Link this Hermes doesn't have")
            return True
        try:
            opened = room_envelope.open_envelope(identity, delivered.body, sender_sign=_key_of(link, "sign"))
        except room_envelope.EnvelopeError as exc:
            logger.info("Linked Rooms dropped an envelope for Link %s that didn't check (%s)", link_id, exc.code)
            return True
        now, kind = int(self.clock()), header.kind
        if kind == CONFIRM:
            token = opened.body.get("token")
            with store.update() as state:
                link = state["links"].get(link_id)
                if link is not None and link["role"] == "acceptor" and link["status"] == "waiting" \
                        and _is_token(token):
                    link.update(status="linked", send_token=token)
                    # The inviter's Hermes names its own mailbox, which the
                    # invite only said.
                    mailbox_url = _peer_mailbox(opened.body.get("mailbox"), self.links._mailbox_url(state))
                    if mailbox_url is not None:
                        link["mailbox"] = mailbox_url
                    else:
                        logger.info("Linked Rooms kept the invite's mailbox for Link %s: its confirm named none "
                                    "this Hermes calls", link_id)
                    # A copy of the accept still to go is answered.
                    state["outbox"] = [item for item in state["outbox"] if item["id"] != f"accept.{link_id}"]
                    if link.pop("leaving", False):
                        # Unlinked while it waited: the inviter's Hermes,
                        # which took it as linked, hears now.
                        _forget(state, link_id, now)
                        state["outbox"].append(_outgoing(REMOVE, link_id, link, {}, now))
            return True
        if kind == REMOVE:
            with store.update() as state:
                if link_id in state["links"]:
                    _forget(state, link_id, now)
            return True
        handler = HANDLERS.get(kind)
        if link["status"] != "linked":
            logger.info("Linked Rooms dropped a %s envelope for Link %s, which isn't confirmed", kind, link_id)
            return True
        if handler is None:
            logger.info("Linked Rooms dropped a %s envelope: nothing on this Hermes handles that kind", kind)
            return True
        if header.id in link.get("seen", {}):
            return True
        key = f"{link_id}/{header.id}"
        try:
            handler(Received(link_id=link_id, kind=kind, message_id=header.id, at=header.at, body=opened.body))
        except Exception:  # noqa: BLE001 - one handler's failure doesn't stop the inbox
            tries = self._failures.get(key, 0) + 1
            if tries < HANDLER_TRIES:
                self._failures[key] = tries
                logger.warning("Linked Rooms' handler for %s failed; it tries again on the next pass", kind,
                               exc_info=True)
                return False
            logger.warning("Linked Rooms' handler for %s failed %d times; the envelope is dropped", kind, tries,
                           exc_info=True)
        self._failures.pop(key, None)
        with store.update() as state:
            link = state["links"].get(link_id)
            if link is not None:
                seen = {message_id: at for message_id, at in link.get("seen", {}).items() if at > now - HOLD_S}
                seen[header.id] = now
                link["seen"] = dict(sorted(seen.items(), key=lambda item: item[1])[-MAX_SEEN:])
        return True

    def _run_out(self) -> bool:
        # Only once a read found the inbox empty: an accept still waiting there
        # would go with its invite's sender, and a confirm with the sender of
        # the Link it's for.
        now = int(self.clock())
        with self.links._store.update() as state:
            expired = [invite_id for invite_id, invite in state["invites"].items() if invite["expires_at"] <= now]
            for invite_id in expired:
                del state["invites"][invite_id]
                _clean_up(state, _invite_sender(invite_id), now)
            # Unlinked while they waited, and no confirm came while one could.
            unanswered = [link_id for link_id, link in state["links"].items()
                          if link.get("leaving") and _status(link, now) == "unanswered"]
            for link_id in unanswered:
                _forget(state, link_id, now)
        return bool(expired or unanswered)


_loops: dict[str, LinkLoop] = {}
_loops_lock = threading.Lock()


def start(root: Any) -> LinkLoop:
    """This process's loop for the installation at ``root``, started."""
    key = str(Path(root))
    with _loops_lock:
        loop = _loops.get(key)
        if loop is None:
            loop = _loops[key] = LinkLoop(root)
    return loop.start()


def reading(root: Any) -> bool:
    """Whether a loop reads this installation's inbox: one running in this
    process, or whichever holds the lease in another."""
    loop = _loops.get(str(Path(root)))
    return (loop is not None and loop.running) or lease_held(root)


def lease_held(root: Any) -> bool:
    """Whether a loop, in any process, reads this installation's inbox now."""
    try:
        handle = open(Path(root) / DIRECTORY / LEASE_NAME, "rb")
    except OSError:
        return False
    with handle:
        if not _try_lock(handle):
            return True
        _unlock(handle)
    return False


# --- The store ----------------------------------------------------------------------


class _Store:
    def __init__(self, root: Path) -> None:
        self.directory = root / DIRECTORY
        self.path = self.directory / FILE_NAME

    def read(self) -> dict[str, Any]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return _empty()
        except OSError as exc:
            raise LinkError("unreadable", f"Couldn't read {self.path} ({exc.strerror or exc}), so Linked Rooms "
                                          "can't use its Links until it can", 500) from None
        try:
            state = json.loads(raw)
        except (ValueError, RecursionError):
            state = None
        if not _well_formed(state):
            raise LinkError("unreadable", f"{self.path} isn't a links file this plugin wrote, so Linked Rooms "
                                          "can't use its Links. Moving it aside ends them on this side: each "
                                          "person links again", 500)
        return state

    @contextmanager
    def update(self) -> Iterator[dict[str, Any]]:
        """The state to change, saved when the block ends without raising."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        lock_path = self.directory / f".{FILE_NAME}.lock"
        with _thread_lock, open(lock_path, "a+b") as handle, room_identity._exclusive(handle):
            state = self.read()
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                self._save(state)

    def _save(self, state: dict[str, Any]) -> None:
        encoded = json.dumps(state, separators=(",", ":"), sort_keys=True, ensure_ascii=False) + "\n"
        # A fresh, unguessable file (mkstemp opens it O_EXCL, mode 0600).
        descriptor, name = tempfile.mkstemp(prefix=f".{FILE_NAME}.", suffix=".tmp", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self.path.chmod(0o600)
        room_identity._sync_directory(self.directory)


def _empty() -> dict[str, Any]:
    return {"version": VERSION, "mailbox": {}, "invites": {}, "links": {}, "outbox": [], "cleanup": []}


def _well_formed(state: Any) -> bool:
    if not isinstance(state, dict) or state.get("version") != VERSION:
        return False
    if not all(isinstance(state.get(name), kind) for name, kind in (
            ("mailbox", dict), ("invites", dict), ("links", dict), ("outbox", list), ("cleanup", list))):
        return False
    return all(isinstance(link, dict) and all(isinstance(link.get(name), str) for name in (
        "name", "sign", "seal", "inbox", "mailbox", "status", "role", "send_token", "my_token"))
        and _public_key(link["sign"]) is not None and _public_key(link["seal"]) is not None
        and isinstance(link.get("created_at"), int) and isinstance(link.get("seen", {}), dict)
        for link in state["links"].values()) \
        and all(isinstance(invite, dict) and isinstance(invite.get("token"), str)
                and isinstance(invite.get("name"), str) and isinstance(invite.get("expires_at"), int)
                for invite in state["invites"].values()) \
        and all(isinstance(item, dict) and isinstance(item.get("until"), int) and isinstance(item.get("body"), dict)
                and all(isinstance(item.get(name), str) for name in (
                    "id", "kind", "link", "mailbox", "inbox", "sign", "seal", "token"))
                for item in state["outbox"]) \
        and all(isinstance(item, dict) and isinstance(item.get("sender"), str)
                and isinstance(item.get("until"), int) for item in state["cleanup"])


def _has_work(state: dict[str, Any]) -> bool:
    return bool(state["invites"] or state["links"] or state["outbox"] or state["cleanup"])


def _forget(state: dict[str, Any], link_id: str, now: int) -> None:
    """Drops a Link here, and its sender at this installation's inbox with
    whatever it left there."""
    state["links"].pop(link_id, None)
    state["outbox"] = [item for item in state["outbox"]
                       if not (item["kind"] in (ACCEPT, CONFIRM) and item["link"] == link_id)]
    _clean_up(state, _link_sender(link_id), now)


def _clean_up(state: dict[str, Any], sender: str, now: int) -> None:
    if all(item["sender"] != sender for item in state["cleanup"]):
        state["cleanup"].append({"sender": sender, "until": now + HOLD_S})


def _outgoing(kind: str, link_id: str, link: dict[str, Any], body: dict[str, Any], now: int) -> dict[str, Any]:
    # Everything the envelope needs, so it can go after the Link is gone.
    return {"id": f"{kind.split('.')[-1]}.{link_id}", "kind": kind, "link": link_id, "mailbox": link["mailbox"],
            "inbox": link["inbox"], "sign": link["sign"], "seal": link["seal"], "token": link["send_token"],
            "body": body, "until": now + HOLD_S}


def _senders(state: dict[str, Any], now: int) -> list[tuple[str, str]]:
    senders = [(_link_sender(link_id), link["my_token"]) for link_id, link in state["links"].items()]
    senders += [(_invite_sender(invite_id), invite["token"]) for invite_id, invite in state["invites"].items()
                if invite["expires_at"] > now]
    return senders


def _shown(link_id: str, link: dict[str, Any], now: int) -> dict[str, Any]:
    return {"id": link_id, "name": link["name"], "status": _status(link, now),
            "words": _words(_key_of(link, "sign"), _key_of(link, "seal")), "created_at": link["created_at"]}


def _words(sign: bytes, seal: bytes) -> list[str]:
    try:
        return list(room_identity.fingerprint_words(sign, seal))
    except room_identity.IdentityError as exc:  # the plugin's word list is missing or changed
        raise _link_error(exc) from None


def _status(link: dict[str, Any], now: int) -> str:
    if link["status"] == "linked":
        return "linked"
    return "unanswered" if now - link["created_at"] > UNANSWERED_S else "waiting"


def _linked_with(state: dict[str, Any], sign: bytes, now: int) -> Optional[str]:
    for link_id, link in state["links"].items():
        if _key_of(link, "sign") == sign and _status(link, now) != "unanswered" and not link.get("leaving"):
            return link_id
    return None


def _accept_went(state: dict[str, Any], link_id: str) -> bool:
    """Whether a waiting Link's accept may have reached the inviter: tried
    without an answer, or gone from the outbox, sent."""
    item = next((item for item in state["outbox"] if item["id"] == f"accept.{link_id}"), None)
    return item is None or bool(item.get("tried"))


def _accept_fields(body: dict) -> Optional[dict[str, Any]]:
    if set(body) != _ACCEPT_FIELDS:
        return None
    name, sign, seal = _checked_name(body["name"]), _public_key(body["sign"]), _public_key(body["seal"])
    if name is None or sign is None or seal is None or not _is_token(body["token"]) \
            or not _is_id(body["invite"]) or not _is_token(body["invite_token"]) \
            or not isinstance(body["mailbox"], str):
        return None
    return {"name": name, "sign": sign, "seal": seal, "token": body["token"], "invite": body["invite"],
            "invite_token": body["invite_token"], "mailbox": body["mailbox"]}


def _canonical_mailbox(value: Any) -> Optional[str]:
    try:
        url = room_mailbox.base_url(value)
    except room_mailbox.MailboxError:
        return None
    return url if url == value else None


def _peer_mailbox(value: Any, mine: str) -> Optional[str]:
    """The other side's mailbox URL, when this Hermes may call it, so nobody
    can point it at a service of this computer's or its network's: http
    (only ever on this host) while this Hermes' own is too (tests, a mailbox
    beside Hermes), and one on this computer or a private network only when
    it's this Hermes' own."""
    url = _canonical_mailbox(value)
    if url is None:
        return None
    if url.startswith("http://"):
        return url if mine.startswith("http://") else None
    return None if url != mine and _private_host(url) else url


def _private_host(url: str) -> bool:
    """Whether a mailbox URL's host is this computer or on a private network:
    a name only when it's localhost, since what other names resolve to isn't
    known until they're called."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def _refused(exc: room_mailbox.MailboxError) -> bool:
    """Whether a send refused with ``exc`` didn't reach the mailbox's store:
    not when it didn't answer, or answered with a server error, which can
    come after it kept what was sent."""
    return exc.status < 500 and exc.code not in ("unreachable", "bad_answer")


def _goes_later(exc: room_mailbox.MailboxError) -> bool:
    """Whether a mailbox call refused with ``exc`` may go through later as it
    is: not once its token is refused, nor what the mailbox never takes."""
    return exc.code not in ("unauthorized", "bad_request", "bad_url", "envelope_too_large", "payload_too_large")


def _mailbox_refused() -> LinkError:
    return LinkError("bad_invite", "That invite names a mailbox on someone's own computer or network, which this "
                                   "Hermes doesn't call for an invite")


def _key_of(entry: dict[str, Any], name: str) -> bytes:
    return sealing.unb64u(entry[name], LinkError)


def _public_key(value: Any) -> Optional[bytes]:
    if not isinstance(value, str) or not _KEY.match(value):
        return None
    raw = sealing.unb64u(value, LinkError)
    return raw if len(raw) == room_identity.KEY_BYTES and sealing.b64u(raw) == value else None


def _checked_name(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    name = value.strip()
    return name if 1 <= len(name) <= NAME_MAX and name.isprintable() else None


def _name(value: Any) -> str:
    name = _checked_name(value)
    if name is None:
        raise LinkError("bad_name", f"A name is 1 to {NAME_MAX} characters, on one line")
    return name


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID.match(value))


def _is_token(value: Any) -> bool:
    return isinstance(value, str) and bool(room_mailbox.SEND_TOKEN.match(value))


def _whole(value: Any) -> bool:
    return type(value) is int and value >= 0


def _new_id(size: int) -> str:
    return sealing.b64u(secrets.token_bytes(size))


def _invite_sender(invite_id: str) -> str:
    return f"i-{invite_id}"


def _link_sender(link_id: str) -> str:
    return f"l-{link_id}"


def _accepted(state: dict[str, Any], invite: Invite, now: int) -> Optional[str]:
    """The Link this Hermes made accepting ``invite``, while it waits."""
    for link_id, link in state["links"].items():
        if link["role"] == "acceptor" and link.get("invite") == invite.id and _key_of(link, "sign") == invite.sign \
                and _status(link, now) == "waiting":
            return link_id
    return None


def _invite_used() -> LinkError:
    # The invite's sender is gone at the inviter's mailbox: used or cancelled.
    return LinkError("invite_used", "That invite has been used or cancelled; ask for a new one", 410)


def _too_many_links(state: dict[str, Any], now: int) -> LinkError:
    message = (f"This Hermes keeps up to {MAX_LINKS} Links, each invite still waiting holding a place for one, and "
               "has none left; unlink one or cancel an invite first")
    # Kept, and not listed, until the loop has read what came for them.
    unlisted = sum(1 for link in state["links"].values() if link.get("leaving")) \
        + len(state["invites"]) - _waiting_invites(state, now)
    if unlisted == 1:
        message += (". One place is held by an invite that ran out or a Link unlinked while it waited, which the "
                    "list doesn't show, until Hermes' dashboard has read the mailbox for it")
    elif unlisted:
        message += (f". {unlisted} places are held by invites that ran out or Links unlinked while they waited, "
                    "which the list doesn't show, until Hermes' dashboard has read the mailbox for them")
    return LinkError("too_many_links", message, 409)


def _waiting_invites(state: dict[str, Any], now: int) -> int:
    return sum(1 for invite in state["invites"].values() if invite["expires_at"] > now)


def _places_held(state: dict[str, Any]) -> int:
    # Each invite kept holds a place for its Link, so an accept for one always
    # finds room: one that ran out is kept until the loop has read what came
    # for it in time.
    return len(state["links"]) + len(state["invites"])


def _link_error(exc: Exception) -> LinkError:
    if isinstance(exc, LinkError):
        return exc
    if isinstance(exc, room_mailbox.MailboxError):
        return LinkError(exc.code, str(exc), 503 if exc.retryable else 502)
    if isinstance(exc, room_envelope.EnvelopeError):
        return LinkError(exc.code, str(exc), exc.status)
    if isinstance(exc, room_identity.IdentityError):
        return LinkError(exc.code, str(exc), 503)
    raise exc


def _try_lock(handle: Any) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True
    if msvcrt is not None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
    return True


def _unlock(handle: Any) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
