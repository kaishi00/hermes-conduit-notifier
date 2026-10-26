"""The rooms mailbox's client (designs/cross-owner-agents, mailbox protocol v1)."""
import base64
import email.message
import http.server
import importlib.util
import io
import json
import logging
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request

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
mailbox_module = _load("conduit_push.room_mailbox", ROOT / "room_mailbox.py")


def _b64u(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _identity_from_seed(root, seed):
    path = root / "conduit-rooms" / "identity.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "sign_key": _b64u(seed), "seal_key": _b64u(bytes(32)),
                                "created_at": 1}))
    return identity_module.load(root)


# Made with the mailbox's own code (mailbox/src/auth.mjs and node:crypto): the
# Ed25519 key whose seed is the bytes 0 to 31 signing one request.
VECTOR_PUBLIC_KEY = "A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg"
VECTOR_INBOX = "Vkdap1RjR0wChd9dvyvKtz2mUTWIOem3dIGy6rEHcIw"
VECTOR_SIGNED = ("mailbox-v1\nPOST\nrooms.milim.dev\n/v1/inboxes/abc/ack?x=1\n1761460000\nAAAAAAAAAAAAAAAAAAAAAA\n"
                 "eXOGWERzFupdZoRCrugUaeTBohWPWwYrruTYrUnDqsE")
VECTOR_SIGNATURE = "CK_3bsQt2R2M-JznI19OA6YLGjkCJoe4cEmY3Mr8XQTUR5SN1GMDoQmpya2Oq1P44Hbg9u6mCA1U7IpHO_NsAw"
VECTOR_TOKEN_HASH = "lIXJHZsgc7y0RzCuAgp34yuIKrJUDm_7FQyaRHaQjPo"


def test_requests_are_signed_as_the_mailbox_checks_them(tmp_path):
    signer = _identity_from_seed(tmp_path, bytes(range(32)))

    assert _b64u(signer.signing_public) == VECTOR_PUBLIC_KEY
    assert signer.inbox == VECTOR_INBOX
    signed = mailbox_module.signed_string("POST", "rooms.milim.dev", "/v1/inboxes/abc/ack?x=1", 1761460000,
                                          "AAAAAAAAAAAAAAAAAAAAAA", b'{"after":0}')
    assert signed == VECTOR_SIGNED.encode()
    authorization = mailbox_module.owner_authorization(signer, "POST", "rooms.milim.dev", "/v1/inboxes/abc/ack?x=1",
                                                       b'{"after":0}', ts=1761460000, nonce="AAAAAAAAAAAAAAAAAAAAAA")
    assert authorization == (f"Mailbox-Ed25519 ts=1761460000,nonce=AAAAAAAAAAAAAAAAAAAAAA,sig={VECTOR_SIGNATURE}")


def test_a_send_tokens_hash_is_the_mailboxs():
    assert mailbox_module.token_sha256("a send token") == VECTOR_TOKEN_HASH


def test_each_request_gets_a_fresh_nonce(tmp_path):
    signer = identity_module.load_or_create(tmp_path)
    pattern = re.compile(r"^Mailbox-Ed25519 ts=(\d+),nonce=([A-Za-z0-9_-]{16,64}),sig=([A-Za-z0-9_-]{86})$")

    first = pattern.match(mailbox_module.owner_authorization(signer, "GET", "h", "/", b"", ts=1))
    second = pattern.match(mailbox_module.owner_authorization(signer, "GET", "h", "/", b"", ts=1))

    assert first and second
    assert first.group(2) != second.group(2) and first.group(3) != second.group(3)


@pytest.mark.parametrize("url, host", [
    ("https://rooms.milim.dev", "rooms.milim.dev"),
    ("https://Rooms.Milim.Dev/", "rooms.milim.dev"),
    ("https://rooms.milim.dev:443", "rooms.milim.dev"),
    ("https://rooms.milim.dev:8443", "rooms.milim.dev:8443"),
    ("http://127.0.0.1:9130", "127.0.0.1:9130"),
    ("http://localhost:80", "localhost"),
    ("http://[::1]:9130", "[::1]:9130"),
    ("http://[0:0:0:0:0:0:0:1]:9130", "[::1]:9130"),
    # The DNS root's spelling of the same name, as the mailbox's own URL has it.
    ("https://rooms.milim.dev./", "rooms.milim.dev"),
    ("http://localhost.:9130", "localhost:9130"),
])
def test_the_host_is_signed_as_a_url_parser_gives_it(url, host):
    assert mailbox_module.url_host(mailbox_module.base_url(url)) == host


@pytest.mark.parametrize("url", [
    "http://rooms.milim.dev",
    "ftp://rooms.milim.dev",
    "https://rooms.milim.dev/v1",
    "https://user:secret@rooms.milim.dev",
    "https://rooms.milim.dev?x=1",
    "https://rooms.milim.dev#top",
    "https://",
    "https://b\N{LATIN SMALL LETTER U WITH DIAERESIS}cher.example",
    "http://127.1:9130",
    "https://rooms..milim.dev",
    "https://rooms.milim.dev..",
    "https://.rooms.milim.dev",
    "https://rooms.milim.dev:0",
    "rooms.milim.dev",
    None,
])
def test_a_mailbox_is_reached_over_https_or_on_this_host(url):
    with pytest.raises(mailbox_module.MailboxError) as refused:
        mailbox_module.base_url(url)
    assert refused.value.code == "bad_url"


@pytest.mark.parametrize("read", [
    lambda inbox: inbox.fetch(after=True),
    lambda inbox: inbox.fetch(after=1.5),
    lambda inbox: inbox.fetch(after="3"),
    lambda inbox: inbox.fetch(after=-1),
    lambda inbox: inbox.fetch(limit=0),
    lambda inbox: inbox.fetch(limit="abc"),
    lambda inbox: inbox.fetch(wait=2.5),
    lambda inbox: inbox.ack(through=False),
    lambda inbox: inbox.ack(through="7"),
    lambda inbox: inbox.ack(seqs=[1, "2"]),
    lambda inbox: inbox.ack(seqs=[1.0]),
], ids=["after-bool", "after-float", "after-text", "after-negative", "limit-zero", "limit-text", "wait-float",
        "through-bool", "through-text", "seqs-text", "seqs-float"])
def test_reads_and_acks_take_only_whole_numbers(eric, read):
    # Refused before anything goes out: nothing listens at this address, so
    # a request would read as unreachable.
    inbox = mailbox_module.Mailbox(f"http://127.0.0.1:{_free_port()}", eric)

    with pytest.raises(mailbox_module.MailboxError) as refused:
        read(inbox)
    assert refused.value.code == "bad_request"


@pytest.mark.parametrize("ack", [{}, {"seqs": []}, {"seqs": [0]}, {"seqs": [1] * 101}],
                         ids=["nothing", "no-seqs", "seq-zero", "over-100-seqs"])
def test_an_ack_the_mailbox_would_refuse_never_goes_out(eric, ack):
    inbox = mailbox_module.Mailbox(f"http://127.0.0.1:{_free_port()}", eric)

    with pytest.raises(mailbox_module.MailboxError) as refused:
        inbox.ack(**ack)
    assert refused.value.code == "bad_request"


@pytest.mark.parametrize("token", ["short-token", "a registration token", "registration-token\r\n",
                                   "registration-tökén"], ids=["short", "space", "line-end", "not-ascii"])
def test_a_registration_token_the_mailbox_cant_have_is_refused(eric, token):
    # The mailbox's own is 16 to 256 printable ASCII characters. Sent, one
    # with a line end would fail in http.client and read as unreachable.
    inbox = mailbox_module.Mailbox(f"http://127.0.0.1:{_free_port()}", eric, registration_token=token)

    with pytest.raises(mailbox_module.MailboxError) as refused:
        inbox.register()
    assert refused.value.code == "bad_request"


def test_a_refused_answer_is_closed(eric, monkeypatch):
    # Left open, a refusal held by a MailboxError someone keeps would hold its
    # connection with it.
    answer = io.BytesIO(b'{"error": "unauthorized"}')

    class Refusing:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", email.message.Message(), answer)

    monkeypatch.setattr(mailbox_module, "_opener", Refusing())
    with pytest.raises(mailbox_module.MailboxError) as refused:
        mailbox_module.Mailbox("https://rooms.milim.dev", eric).info()

    assert refused.value.code == "unauthorized"
    assert answer.closed


# --- Against the mailbox itself --------------------------------------------------


def _node_can_run_the_mailbox():
    node = shutil.which("node")
    if node is None:
        return False
    try:
        version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    match = re.match(r"^v(\d+)\.(\d+)", version)
    return bool(match) and (int(match.group(1)), int(match.group(2))) >= (22, 13)


needs_node = pytest.mark.skipif(not _node_can_run_the_mailbox(), reason="the mailbox needs Node 22.13 or later")


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def mailbox_url():
    data = tempfile.mkdtemp(prefix="conduit-mailbox-test-")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    # Every test makes inboxes from this one address.
    env = dict(os.environ, PUBLIC_URL=url, DATA_PATH=os.path.join(data, "mailbox.sqlite"), PORT=str(port),
               HOST="127.0.0.1", MAILBOX_REGISTRATIONS_PER_HOUR_PER_CLIENT="1000",
               MAILBOX_REGISTRATIONS_PER_MINUTE="1000")
    # Its log goes to a file, which can't fill up and stall it as a pipe nobody reads can.
    log = pathlib.Path(data) / "server.log"
    with open(log, "wb") as output:
        server = subprocess.Popen([shutil.which("node"), str(ROOT / "mailbox" / "src" / "server.mjs")], env=env,
                                  stdout=output, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                with urllib.request.urlopen(f"{url}/healthz", timeout=1) as answer:
                    if json.load(answer) == {"ok": True}:
                        break
            except OSError:
                if server.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("the mailbox didn't start: " + log.read_text(errors="replace")[-2000:])
                time.sleep(0.1)
        yield url
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(data, ignore_errors=True)


@pytest.fixture
def eric(tmp_path):
    return identity_module.load_or_create(tmp_path / "eric")


@needs_node
def test_an_inbox_is_made_once_and_confirmed_after(mailbox_url, eric):
    inbox = mailbox_module.Mailbox(mailbox_url, eric)

    made = inbox.register()
    again = inbox.register()

    assert made["inbox"] == eric.inbox and made["created"] is True
    assert again["inbox"] == eric.inbox and again["created"] is False
    assert made["limits"]["max_envelope_bytes"] == 262144
    assert made["limits"]["max_wait_seconds"] == 25


@needs_node
def test_a_linked_sender_leaves_envelopes_that_the_owner_reads_and_acks(mailbox_url, eric):
    inbox = mailbox_module.Mailbox(mailbox_url, eric)
    inbox.register()
    token = mailbox_module.new_token()
    inbox.put_sender("l-mona", token)

    expires = mailbox_module.send(mailbox_url, eric.inbox, token, b"sealed bytes", idempotency_key="m1")
    again = mailbox_module.send(mailbox_url, eric.inbox, token, b"sealed bytes", idempotency_key="m1")
    envelopes, after = inbox.fetch(after=0)

    assert again == expires and expires > time.time() + 6 * 86400
    assert [(item.sender, item.body) for item in envelopes] == [("l-mona", b"sealed bytes")]
    assert after == envelopes[0].seq
    assert inbox.info()["pending"] == 1
    assert [(row["sender"], row["pending"]) for row in inbox.senders()] == [("l-mona", 1)]
    assert inbox.ack(through=after) == 1
    assert inbox.fetch(after=after) == ([], after)


@needs_node
def test_a_backlog_of_the_largest_envelopes_reads_a_page_at_a_time(mailbox_url, eric):
    # In one answer, this backlog would be more than the client reads, and
    # the inbox could never be read again: the mailbox stops each page at
    # 1 MB of envelopes, whatever the limit.
    inbox = mailbox_module.Mailbox(mailbox_url, eric)
    inbox.register()
    token = mailbox_module.new_token()
    inbox.put_sender("l-mona", token)
    for number in range(13):
        mailbox_module.send(mailbox_url, eric.inbox, token, bytes([number]) * 262144,
                            idempotency_key=f"large{number}")

    pages, after = [], 0
    while True:
        page, after = inbox.fetch(after=after, limit=100)
        if not page:
            break
        pages.append(page)

    assert [len(page) for page in pages] == [4, 4, 4, 1]
    assert [item.body[0] for page in pages for item in page] == list(range(13))


@needs_node
def test_a_waiting_fetch_wakes_when_an_envelope_arrives(mailbox_url, eric):
    inbox = mailbox_module.Mailbox(mailbox_url, eric)
    inbox.register()
    token = mailbox_module.new_token()
    inbox.put_sender("l-mona", token)
    got = []
    reader = threading.Thread(target=lambda: got.append(inbox.fetch(after=0, wait=20)))

    started = time.monotonic()
    reader.start()
    time.sleep(0.5)
    mailbox_module.send(mailbox_url, eric.inbox, token, b"wake up", idempotency_key="m2")
    reader.join(timeout=15)

    assert not reader.is_alive()
    assert time.monotonic() - started < 10
    assert [item.body for item in got[0][0]] == [b"wake up"]


@needs_node
def test_a_removed_sender_can_no_longer_send(mailbox_url, eric):
    inbox = mailbox_module.Mailbox(mailbox_url, eric)
    inbox.register()
    token = mailbox_module.new_token()
    inbox.put_sender("l-mona", token)

    assert inbox.delete_sender("l-mona") is True
    assert inbox.delete_sender("l-mona") is False
    with pytest.raises(mailbox_module.MailboxError) as refused:
        mailbox_module.send(mailbox_url, eric.inbox, token, b"too late", idempotency_key="m3")
    assert refused.value.code == "unauthorized"
    assert refused.value.status == 401


@needs_node
def test_an_envelope_over_the_limit_keeps_the_mailboxs_code(mailbox_url, eric):
    inbox = mailbox_module.Mailbox(mailbox_url, eric)
    inbox.register()
    token = mailbox_module.new_token()
    inbox.put_sender("l-mona", token)

    with pytest.raises(mailbox_module.MailboxError) as refused:
        mailbox_module.send(mailbox_url, eric.inbox, token, b"x" * 262145, idempotency_key="m4")
    assert refused.value.code == "envelope_too_large"
    assert refused.value.status == 413
    assert not refused.value.retryable


@needs_node
def test_a_skewed_clock_reads_as_the_clock_not_a_dead_link(mailbox_url, eric):
    mailbox_module.Mailbox(mailbox_url, eric).register()
    skewed = mailbox_module.Mailbox(mailbox_url, eric, clock=lambda: time.time() - 600)

    with pytest.raises(mailbox_module.MailboxError) as refused:
        skewed.info()

    assert refused.value.code == "clock_off"
    assert "clock" in str(refused.value) and "10 minutes" in str(refused.value)


@needs_node
def test_an_inbox_it_never_made_is_refused_as_unauthorized(mailbox_url, tmp_path):
    stranger = identity_module.load_or_create(tmp_path / "stranger")

    with pytest.raises(mailbox_module.MailboxError) as refused:
        mailbox_module.Mailbox(mailbox_url, stranger).info()
    assert refused.value.code == "unauthorized"


@needs_node
def test_a_deleted_inbox_is_gone(mailbox_url, eric):
    inbox = mailbox_module.Mailbox(mailbox_url, eric)
    inbox.register()

    inbox.delete()

    with pytest.raises(mailbox_module.MailboxError) as refused:
        inbox.info()
    assert refused.value.code == "unauthorized"


def _serve(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_redirect_is_refused_not_followed(eric):
    # Followed, a 302 would take a signed read, or a send and its token, to
    # whatever host the answer names.
    reached = []

    class Elsewhere(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            reached.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    elsewhere = _serve(Elsewhere)

    class Redirecting(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{elsewhere.server_port}{self.path}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *args):
            pass

    redirecting = _serve(Redirecting)
    url = f"http://127.0.0.1:{redirecting.server_port}"
    try:
        with pytest.raises(mailbox_module.MailboxError) as refused:
            mailbox_module.Mailbox(url, eric).info()
        assert refused.value.status == 302
        with pytest.raises(mailbox_module.MailboxError) as refused:
            mailbox_module.send(url, eric.inbox, "a-send-token", b"{}")
        assert refused.value.status == 302
        assert reached == []
    finally:
        for server in (redirecting, elsewhere):
            server.shutdown()
            server.server_close()


def test_a_mailbox_that_isnt_there_is_unreachable(eric):
    inbox = mailbox_module.Mailbox(f"http://127.0.0.1:{_free_port()}", eric)

    with pytest.raises(mailbox_module.MailboxError) as refused:
        inbox.info()
    assert refused.value.code == "unreachable"
    assert refused.value.retryable


def test_why_a_mailbox_couldnt_be_reached_goes_to_the_log_not_the_error(eric, caplog):
    # The reason can name proxies or hosts: the log keeps it, and an answer
    # made from the error doesn't carry it.
    inbox = mailbox_module.Mailbox(f"http://127.0.0.1:{_free_port()}", eric)

    with caplog.at_level(logging.INFO), pytest.raises(mailbox_module.MailboxError) as refused:
        inbox.info()
    assert str(refused.value).startswith("Couldn't reach the mailbox at 127.0.0.1")
    assert "(" not in str(refused.value)
    assert "Connection refused" in caplog.text
