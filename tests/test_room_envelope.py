"""Envelopes between linked installations (designs/cross-owner-agents, decision 5)."""
import base64
import importlib.util
import json
import pathlib
import re
import sys
import tempfile
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


identity_module = _load("conduit_push.room_identity", ROOT / "room_identity.py")
envelope = _load("conduit_push.room_envelope", ROOT / "room_envelope.py")

# The mailbox's Idempotency-Key (mailbox/src/server.mjs).
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@pytest.fixture
def eric(tmp_path):
    return identity_module.load_or_create(tmp_path / "eric")


@pytest.fixture
def mona(tmp_path):
    return identity_module.load_or_create(tmp_path / "mona")


@pytest.fixture
def mallory(tmp_path):
    return identity_module.load_or_create(tmp_path / "mallory")


def _sealed(sender, recipient, body=None, **overrides):
    arguments = dict(link="link1", recipient_sign=recipient.signing_public, recipient_seal=recipient.sealing_public,
                     message_id="m1", kind="link.test", body={"hello": "Mona"} if body is None else body, at=1_761_460_000)
    arguments.update(overrides)
    return envelope.seal(sender, **arguments)


def _edited(data, **fields):
    fields_now = json.loads(data)
    fields_now.update(fields)
    return json.dumps(fields_now).encode()


def _b64u(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def test_an_envelope_opens_for_its_recipient_with_its_senders_key(eric, mona):
    data = _sealed(eric, mona, {"text": "Zoo on the 22nd? \N{GIRAFFE FACE}"})

    opened = envelope.open_envelope(mona, data, eric.signing_public)

    assert opened.body == {"text": "Zoo on the 22nd? \N{GIRAFFE FACE}"}
    assert opened.header == envelope.Header(link="link1", sender=eric.inbox, recipient=mona.inbox, id="m1",
                                            kind="link.test", at=1_761_460_000)
    assert opened.sender_sign == eric.signing_public


def test_the_mailbox_sees_only_the_header(eric, mona):
    data = _sealed(eric, mona, {"text": "a secret plan"})

    assert len(data) <= envelope.MAX_BYTES
    fields = json.loads(data)
    assert set(fields) == {"v", "link", "from", "to", "id", "kind", "at", "epk", "nonce", "ct", "sig"}
    assert b"secret" not in data and b"plan" not in data
    assert envelope.peek(data) == envelope.Header(link="link1", sender=eric.inbox, recipient=mona.inbox, id="m1",
                                                  kind="link.test", at=1_761_460_000)


def test_a_links_first_message_carries_its_senders_key_inside(eric, mona):
    data = _sealed(eric, mona, {"sign": _b64u(eric.signing_public), "name": "Eric"}, kind="link.accept")

    opened = envelope.open_envelope(mona, data)

    assert opened.sender_sign == eric.signing_public
    assert opened.body["name"] == "Eric"


class _Claiming:
    """Mallory's signing key, sending as someone else's inbox."""

    def __init__(self, keys, inbox):
        self.keys, self.inbox = keys, inbox

    def sign(self, message):
        return self.keys.sign(message)


def test_a_first_message_whose_key_isnt_its_senders_is_refused(eric, mona, mallory):
    # Mallory says the envelope is Eric's and puts his key inside, but can
    # only sign it with hers.
    data = _sealed(_Claiming(mallory, eric.inbox), mona, {"sign": _b64u(eric.signing_public)})

    with pytest.raises(envelope.EnvelopeError) as refused:
        envelope.open_envelope(mona, data)
    assert refused.value.code == "bad_signature"

    # Her own key inside, Eric's inbox outside.
    data = _sealed(_Claiming(mallory, eric.inbox), mona, {"sign": _b64u(mallory.signing_public)})
    with pytest.raises(envelope.EnvelopeError) as refused:
        envelope.open_envelope(mona, data)
    assert refused.value.code == "wrong_sender"


def test_a_first_messages_header_is_bound_to_its_body(eric, mona):
    # The header is the body's associated data, so an edit can't survive
    # opening even before the key inside is known.
    data = _edited(_sealed(eric, mona, {"sign": _b64u(eric.signing_public)}), kind="link.other")

    with pytest.raises(envelope.EnvelopeError) as refused:
        envelope.open_envelope(mona, data)
    assert refused.value.code == "bad_seal"


def test_a_first_message_without_a_key_inside_is_refused(eric, mona):
    with pytest.raises(envelope.EnvelopeError) as refused:
        envelope.open_envelope(mona, _sealed(eric, mona, {"name": "Eric"}))
    assert refused.value.code == "wrong_sender"


def test_a_known_sender_must_be_the_one_who_signed(eric, mona, mallory):
    data = _sealed(mallory, mona)

    with pytest.raises(envelope.EnvelopeError) as refused:
        envelope.open_envelope(mona, data, eric.signing_public)
    assert refused.value.code == "wrong_sender"


@pytest.mark.parametrize("field, value", [
    ("link", "link2"),
    ("id", "m2"),
    ("kind", "link.other"),
    ("at", 1_761_460_001),
    ("epk", _b64u(bytes(range(32)))),
    ("nonce", _b64u(bytes(12))),
])
def test_every_header_field_is_signed(eric, mona, field, value):
    data = _edited(_sealed(eric, mona), **{field: value})

    with pytest.raises(envelope.EnvelopeError) as refused:
        envelope.open_envelope(mona, data, eric.signing_public)
    assert refused.value.code == "bad_signature"


def test_the_ciphertext_and_signature_are_checked(eric, mona):
    data = _sealed(eric, mona)
    fields = json.loads(data)
    ciphertext = bytearray(base64.urlsafe_b64decode(fields["ct"] + "=" * (-len(fields["ct"]) % 4)))
    ciphertext[0] ^= 1
    signature = bytearray(base64.urlsafe_b64decode(fields["sig"] + "=="))
    signature[0] ^= 1

    for edit in ({"ct": _b64u(bytes(ciphertext))}, {"sig": _b64u(bytes(signature))}):
        with pytest.raises(envelope.EnvelopeError) as refused:
            envelope.open_envelope(mona, _edited(data, **edit), eric.signing_public)
        assert refused.value.code == "bad_signature"


def test_an_envelope_for_someone_else_is_refused(eric, mona, mallory):
    data = _sealed(eric, mona)

    with pytest.raises(envelope.EnvelopeError) as refused:
        envelope.open_envelope(mallory, data, eric.signing_public)
    assert refused.value.code == "not_for_me"


def test_a_body_sealed_to_another_key_doesnt_open(eric, mona, mallory):
    # Addressed and signed for Mona, sealed to Mallory's key.
    data = _sealed(eric, mona, recipient_seal=mallory.sealing_public)

    with pytest.raises(envelope.EnvelopeError) as refused:
        envelope.open_envelope(mona, data, eric.signing_public)
    assert refused.value.code == "bad_seal"


def test_an_envelope_over_the_mailboxs_limit_is_refused(eric, mona):
    with pytest.raises(envelope.EnvelopeError) as refused:
        _sealed(eric, mona, {"text": "x" * envelope.MAX_BYTES})
    assert refused.value.code == "too_large"

    with pytest.raises(envelope.EnvelopeError) as refused:
        envelope.peek(b"{" + b" " * envelope.MAX_BYTES + b"}")
    assert refused.value.code == "too_large"


@pytest.mark.parametrize("data", [
    b"not json",
    b"[]",
    b'{"v": 1}',
    "{\"v\": 1, \"link\": \"l\N{LATIN SMALL LETTER E WITH ACUTE}\"}".encode(),
    b"\xff\xfe",
])
def test_bytes_that_arent_an_envelope_are_refused(mona, data):
    with pytest.raises(envelope.EnvelopeError) as refused:
        envelope.peek(data)
    assert refused.value.code == "malformed"


@pytest.mark.parametrize("edit", [
    {"v": 2},
    {"v": True},
    {"at": -1},
    {"at": 1.5},
    {"at": True},
    {"link": ""},
    {"link": "a/b"},
    {"id": "has space"},
    {"kind": "Link.Test"},
    {"from": "short"},
    {"extra": "not signed"},
])
def test_header_fields_of_the_wrong_shape_are_refused(eric, mona, edit):
    data = _edited(_sealed(eric, mona), **edit)

    with pytest.raises(envelope.EnvelopeError) as refused:
        envelope.open_envelope(mona, data, eric.signing_public)
    assert refused.value.code == "malformed"


def test_seal_refuses_what_it_couldnt_open(eric, mona):
    for overrides in ({"body": ["not", "an", "object"]}, {"link": "bad link"}, {"message_id": ""},
                      {"kind": "UPPER"}, {"recipient_seal": b"short"}):
        with pytest.raises(envelope.EnvelopeError):
            _sealed(eric, mona, **overrides)


def test_message_ids_are_fresh_and_fit_the_mailboxs_idempotency_key():
    made = {envelope.new_message_id() for _ in range(100)}

    assert len(made) == 100
    assert all(IDEMPOTENCY_KEY.match(made_id) and envelope.MESSAGE_ID.match(made_id) for made_id in made)


def test_the_time_is_the_senders_clock_when_none_is_given(eric, mona):
    data = envelope.seal(eric, link="link1", recipient_sign=mona.signing_public,
                         recipient_seal=mona.sealing_public, message_id="m1", kind="link.test", body={},
                         clock=lambda: 1_761_460_123.9)

    assert envelope.peek(data).at == 1_761_460_123
