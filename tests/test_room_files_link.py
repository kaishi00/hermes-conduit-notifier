"""Room files carried over Links (Eric 2026-11-08: every participant in a room sees the same thing): the home sends
each file a room message names to every Link with an agent in the room, the member's Hermes keeps it under its own
room-files root, and a turn for its agent names that copy instead of the home's path.

Furina hosts; Laifu and Mona join over link1 and link2 (test_room_invites' installations, test_room_copies' log).
"""
import base64
import hashlib
import stat

from test_room_bridge_member import ROOT, _load
from test_room_copies import copies, started  # noqa: F401 - the fixture
from test_room_invites import _sent, rooms  # noqa: F401 - the fixture

files_module = _load("conduit_push.room_files_link", ROOT / "room_files_link.py")

FOLDER = "20261108-" + "a" * 32


def upload(side, name, data, folder=FOLDER):
    path = side.tmp_path / "furina" / "conduit" / "room-files" / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def post(side, room_id, text):
    return side.log.add(room_id, "message.user", {"text": text}, actor={"kind": "user", "id": "user"})


def carried(side, name, file_name, folder=FOLDER):
    return side.tmp_path / name / "conduit" / "room-files" / folder / file_name


def test_a_file_a_room_message_names_reaches_every_member_and_is_answered(copies):  # noqa: F811
    room_id = started(copies)
    data = bytes(range(256)) * 900  # three chunks
    path = upload(copies, "shot.jpg", data)
    post(copies, room_id, f"@file:{path}\n\nWhat's this?")

    copies.furina.pass_once()

    chunks = [(link, body) for link, body, _ in _sent(copies, "room.file")]
    assert sorted({link for link, _ in chunks}) == ["link1", "link2"]
    assert {body["count"] for _, body in chunks} == {3}
    assert all(base64.b64decode(body["data"]) for _, body in chunks)
    for name in ("laifu", "mona"):
        kept = carried(copies, name, "shot.jpg")
        assert kept.read_bytes() == data
        assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    got = [body for _, body, _ in _sent(copies, "room.file_got")]
    assert len(got) == 2 and {body["sha"] for body in got} == {hashlib.sha256(data).hexdigest()}
    state = files_module.Store(copies.tmp_path / "furina").read()
    assert all(entry["done"] for out in state["out"].values() for entry in out.values())
    # Answered: it doesn't go again.
    copies.furina.pass_once()
    assert len(_sent(copies, "room.file")) == len(chunks)


def test_a_file_uploaded_under_a_profile_goes_too(copies):  # noqa: F811
    # POST /room-files stores under the profile Conduit names (GLM on notifier #629).
    room_id = started(copies)
    path = copies.tmp_path / "furina" / "profiles" / "work" / "conduit" / "room-files" / FOLDER / "plan.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF-1.7 plan")
    post(copies, room_id, f"@file:{path}\nRead this")

    copies.furina.pass_once()

    for name in ("laifu", "mona"):
        assert carried(copies, name, "plan.pdf").read_bytes() == b"%PDF-1.7 plan"


def test_every_file_a_message_names_goes_in_one_pass(copies):  # noqa: F811
    # GLM on notifier #629: one file a pass outran a guest's 8 s wait for a message with several.
    room_id = started(copies)
    names = [f"shot{index}.jpg" for index in range(5)]
    paths = [upload(copies, name, bytes([index]) * 1000) for index, name in enumerate(names)]
    post(copies, room_id, "\n".join(f"@file:{path}" for path in paths) + "\n\nThese")

    copies.furina.pass_once()

    for name in names:
        assert carried(copies, "laifu", name).exists()


def test_only_room_files_under_the_homes_root_go(copies, tmp_path):  # noqa: F811
    room_id = started(copies)
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"no")
    outside = copies.tmp_path / "furina" / "elsewhere" / FOLDER / "x.txt"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"no")
    post(copies, room_id, f"@file:{secret}\n@file:{outside}\n@file:/conduit/room-files/{FOLDER}/gone.txt\nhi")

    copies.furina.pass_once()

    assert _sent(copies, "room.file") == []


def test_a_turn_names_this_hosts_copy_of_a_room_file(tmp_path):
    root = tmp_path / "laifu"
    kept = root / "conduit" / "room-files" / FOLDER / "my photo.jpg"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"x")
    home = f"/root/.hermes/conduit/room-files/{FOLDER}"
    prompt = (f"  User (user): @file:`{home}/my photo.jpg`\nand @file:{home}/missing.pdf\n"
              f"@file:/etc/passwd")
    waits = []
    local = files_module.localized(prompt, root, wait_s=1.0, clock=lambda: len(waits), sleep=waits.append)

    assert local == (f"  User (user): @file:`{kept}`\nand @file:{home}/missing.pdf\n@file:/etc/passwd")
    # It waited for the one still on its way, then went with the home's path.
    assert waits
    assert files_module.localized("no files here", root) == "no files here"


def test_a_chunk_that_isnt_one_is_dropped(copies):  # noqa: F811
    good = {"v": 1, "room_id": "r", "folder": FOLDER, "name": "a.txt", "sha": "0" * 64, "size": 3, "index": 0,
            "count": 1, "data": base64.b64encode(b"abc").decode()}
    assert files_module._chunk_from(good) is not None
    for bad in ({**good, "folder": "../x"}, {**good, "name": "../a"}, {**good, "name": "a/b"},
                {**good, "count": 2}, {**good, "index": 1}, {**good, "data": "!!"}, {**good, "size": -1},
                {**good, "data": base64.b64encode(b"abcd").decode()}, {**good, "v": 2},
                {**good, "size": files_module.MAX_FILE_BYTES + 1}):
        assert files_module._chunk_from(bad) is None, bad


def test_a_windows_homes_path_is_one_too(tmp_path):
    # DeepSeek on notifier #629: a Windows home names its uploads with backslashes.
    root = tmp_path / "laifu"
    kept = root / "conduit" / "room-files" / FOLDER / "shot.jpg"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"x")
    home = f"C:\\Users\\furina\\.hermes\\conduit\\room-files\\{FOLDER}\\shot.jpg"

    assert files_module.references(f"@file:{home} look") == [(home, FOLDER, "shot.jpg")]
    assert files_module.localized(f"@file:{home} look", root, wait_s=0) == f"@file:{kept} look"


def test_a_carried_file_that_cant_be_kept_leaves_nothing_behind(tmp_path, monkeypatch):
    root = tmp_path / "laifu"
    target = files_module.files_root(root) / FOLDER / "shot.jpg"
    files = files_module.Files(type("Rooms", (), {"root": root, "clock": staticmethod(lambda: 0.0)})())
    body = {"folder": FOLDER, "name": "shot.jpg"}

    def full(*_args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(files_module.os, "replace", full)
    assert files._keep(target, b"data", body) is False
    assert list(target.parent.iterdir()) == []

    monkeypatch.undo()
    monkeypatch.setattr(files_module, "TOTAL_BYTES", 3)
    assert files._keep(target, b"data", body) is False, "over the room files' total"
    assert not target.exists()
    monkeypatch.setattr(files_module, "TOTAL_BYTES", 2 * 1024 ** 3)
    assert files._keep(target, b"data", body) is True
    assert target.read_bytes() == b"data"


def test_room_files_age_out_and_make_room_on_a_guest_that_never_uploads(tmp_path, monkeypatch):
    # DeepSeek on notifier #629: the uploads' sweep runs only on an upload.
    import os

    root = files_module.files_root(tmp_path / "laifu")
    old, recent = root / ("20261001-" + "1" * 32), root / ("20261107-" + "2" * 32)
    for folder in (old, recent):
        folder.mkdir(parents=True)
        (folder / "a.bin").write_bytes(b"x" * 10)
    now = recent.stat().st_mtime + 60
    os.utime(old, (now - files_module.RETENTION_S - 1,) * 2)

    assert files_module._room_for(root, 5, now)
    assert not old.exists() and recent.exists()
    monkeypatch.setattr(files_module, "TOTAL_BYTES", 12)
    assert files_module._room_for(root, 5, now), "the oldest goes to make room"
    assert not recent.exists()
    assert not files_module._room_for(root, 13, now), "a file over the total never fits"


def test_a_name_with_a_lone_surrogate_isnt_one():
    assert files_module._name_ok("shot\ud800.jpg") is False
    assert files_module._name_ok("shot.jpg") is True


def test_a_linked_persons_post_naming_a_room_file_carries_nothing(copies, monkeypatch):  # noqa: F811
    # DeepSeek on notifier #629: only what the home's own people sent goes.
    from test_room_copies import copies_module

    room_id = started(copies)
    path = upload(copies, "theirs.jpg", b"x" * 100)
    post(copies, room_id, f"@file:{path}\nlook")
    monkeypatch.setattr(copies_module.room_posts, "by_event",
                        lambda root, room, ids: {each: "link2" for each in ids})

    copies.furina.pass_once()

    assert _sent(copies, "room.file") == []


def test_a_file_no_member_answered_after_every_try_is_given_up(copies):  # noqa: F811
    # GLM on notifier #629: an entry past MAX_TRIES was neither sent nor dropped.
    room_id = started(copies)
    path = upload(copies, "shot.jpg", b"\xff\xd8 shot")
    store = files_module.Store(copies.tmp_path / "furina")
    with store.update() as state:
        state["out"]["link1"] = {f"{FOLDER}/shot.jpg": {
            "room": room_id, "size": path.stat().st_size, "base": str(path.parent.parent), "at": -10.0 ** 9,
            "tries": files_module.MAX_TRIES, "done": False, "noted": 0}}

    copies.furina.pass_once()

    assert _sent(copies, "room.file") == []
    assert store.read()["out"].get("link1", {}) == {}


def test_a_guest_sweeps_its_room_files_without_more_arriving(tmp_path):
    # DeepSeek on notifier #629: carried files aged out only when another arrived.
    import os
    import types

    root = files_module.files_root(tmp_path)
    old = root / ("20261001-" + "1" * 32)
    old.mkdir(parents=True)
    (old / "a.bin").write_bytes(b"x")
    clock = [old.stat().st_mtime + files_module.RETENTION_S + 60]
    files = files_module.Files(types.SimpleNamespace(root=tmp_path, clock=lambda: clock[0]))

    files.sweep()

    assert not old.exists()
    assert (tmp_path / "conduit" / files_module.LOCK_NAME).exists()
    # Not again within SWEEP_S.
    old.mkdir()
    os.utime(old, (0, 0))
    clock[0] += files_module.SWEEP_S - 1
    files.sweep()
    assert old.exists()
    clock[0] += 2
    files.sweep()
    assert not old.exists()
