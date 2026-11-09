"""Room files carried over Links (Eric 2026-11-08: every participant in a room sees the same thing): the home sends
each file a room message names to every Link with an agent in the room, the member's Hermes keeps it under its own
room-files root, and a turn for its agent names that copy instead of the home's path.

Furina hosts; Laifu and Mona join over link1 and link2 (test_room_invites' installations, test_room_copies' log).
"""
import base64
import hashlib
import pathlib
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


def test_a_pass_sends_up_to_its_bytes_and_the_rest_next(copies, monkeypatch):  # noqa: F811
    # DeepSeek on notifier #629: the per-pass bound had no test.
    monkeypatch.setattr(files_module, "PASS_BYTES", 2500)
    room_id = started(copies)
    names = [f"shot{index}.jpg" for index in range(5)]
    paths = [upload(copies, name, bytes([index]) * 1000) for index, name in enumerate(names)]
    post(copies, room_id, "\n".join(f"@file:{path}" for path in paths) + "\n\nThese")

    copies.furina.pass_once()
    assert [carried(copies, "laifu", name).exists() for name in names] == [True, True, False, False, False]

    copies.furina.pass_once()
    copies.furina.pass_once()
    assert all(carried(copies, "laifu", name).exists() for name in names)


def test_pieces_past_the_incoming_bound_are_dropped(tmp_path, monkeypatch):
    # DeepSeek on notifier #629: the 64 MB bound on pieces still arriving had no test.
    monkeypatch.setattr(files_module, "INCOMING_BYTES", 8)
    files = files_module.Files(type("Rooms", (), {"root": tmp_path, "clock": staticmethod(lambda: 0.0)})())
    first = {"sha": "a" * 64, "folder": FOLDER, "name": "one.jpg", "index": 0, "count": 2, "size": 10,
             "data": b"x" * 5}
    second = {**first, "sha": "b" * 64, "name": "two.jpg"}

    assert files._take("link1", first) is None
    assert files._take("link1", second) is None
    incoming = tmp_path / files_module.room_identity.DIRECTORY / files_module.INCOMING
    assert sorted(len(piece.read_bytes()) for piece in incoming.rglob("*") if piece.is_file()) == [5]


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
        (folder / "a.bin").write_bytes(b"x" * (160_000 + 1))
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


def test_a_send_that_doesnt_go_isnt_a_try(copies, monkeypatch):  # noqa: F811
    # DeepSeek on notifier #629: a mailbox outage spent a file's tries though nothing went.
    room_id = started(copies)
    path = upload(copies, "shot.jpg", b"\xff\xd8 shot")
    post(copies, room_id, f"@file:{path}\nhi")
    send = copies.furina.send

    def down(link_id, kind, body, **kwargs):
        if kind == files_module.CHUNK:
            raise files_module.room_links.LinkError("unavailable", "mailbox down", 503)
        return send(link_id, kind, body, **kwargs)

    monkeypatch.setattr(copies.furina, "send", down)
    copies.furina.pass_once()

    owed = [entry for link in files_module.Store(copies.tmp_path / "furina").read()["out"].values()
            for entry in link.values()]
    assert owed and all(entry["tries"] == 0 and entry["at"] is None and not entry["done"] for entry in owed)
    assert not carried(copies, "laifu", "shot.jpg").exists()


def test_a_link_over_its_entries_forgets_files_it_has_first(copies, monkeypatch):  # noqa: F811
    # Beam on notifier #629: the bound dropped the oldest entry even when it hadn't gone.
    monkeypatch.setattr(files_module, "MAX_KEPT", 2)
    room_id = started(copies)
    store = files_module.Store(copies.tmp_path / "furina")
    first = upload(copies, "first.jpg", b"\xff\xd8 first")
    post(copies, room_id, f"@file:{first}\nhi")
    copies.furina.pass_once()
    links = list(store.read()["out"])
    with store.update() as state:
        for link_id in links:
            state["out"][link_id]["old.jpg"] = {"room": room_id, "rooms": [room_id], "size": 1, "base": "/nowhere",
                                                "at": 0.0, "tries": 1, "done": True, "noted": 5.0}
            state["out"][link_id][f"{FOLDER}/first.jpg"].update(done=False, at=10.0 ** 9, noted=-1.0)
    second = upload(copies, "second.jpg", b"\xff\xd8 second")
    post(copies, room_id, f"@file:{second}\nhi")
    copies.furina.pass_once()

    for link_id in links:
        kept = store.read()["out"][link_id]
        assert "old.jpg" not in kept, "a file the guest has goes before one still owed"
        assert any(key.endswith("/first.jpg") for key in kept)


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


def test_a_file_isnt_sent_to_a_link_no_longer_in_its_room(copies):  # noqa: F811
    # GLM on notifier #629: the home kept sending to a Link whose agents had left, which the guest refuses.
    room_id = started(copies)
    path = upload(copies, "shot.jpg", b"\xff\xd8 shot")
    store = files_module.Store(copies.tmp_path / "furina")
    entry = {"size": path.stat().st_size, "base": str(path.parent.parent), "at": None, "tries": 0, "done": False,
             "noted": 0}
    with store.update() as state:
        state["out"]["link1"] = {f"{FOLDER}/shot.jpg": {**entry, "room": "room-gone"}}
        state["out"]["link9"] = {f"{FOLDER}/shot.jpg": {**entry, "room": room_id}}

    copies.furina.pass_once()

    assert _sent(copies, "room.file") == []
    assert store.read()["out"].get("link1", {}) == {} and store.read()["out"].get("link9", {}) == {}


def test_a_carried_file_isnt_written_through_a_symlinked_folder(tmp_path):
    # Beam on notifier #629.
    root = tmp_path / "laifu"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    folder = files_module.files_root(root) / FOLDER
    folder.parent.mkdir(parents=True)
    folder.symlink_to(elsewhere, target_is_directory=True)
    files = files_module.Files(type("Rooms", (), {"root": root, "clock": staticmethod(lambda: 0.0)})())

    assert files._keep(folder / "shot.jpg", b"data", {"folder": FOLDER, "name": "shot.jpg"}) is False
    assert list(elsewhere.iterdir()) == []


def test_a_drive_qualified_name_isnt_one():
    # Greptile on notifier #629: joined on a Windows home, D:report.txt leaves the upload folder.
    for name in ("D:report.txt", "c:shot.jpg", "D:", "a|b.txt", "what?.jpg"):
        assert files_module._name_ok(name) is False, name
    assert files_module._name_ok("report final (2).txt") is True


def test_a_carried_file_isnt_written_outside_room_files(tmp_path):
    # Greptile on notifier #629: the final target is checked, whatever the name.
    root = tmp_path / "laifu"
    files = files_module.Files(type("Rooms", (), {"root": root, "clock": staticmethod(lambda: 0.0)})())
    outside = tmp_path / "outside.txt"

    assert files._keep(files_module.files_root(root) / FOLDER / outside, b"data",
                       {"folder": FOLDER, "name": "outside.txt"}) is False
    assert not outside.exists()


def test_a_piece_that_cant_be_written_is_dropped(tmp_path, monkeypatch):
    # Beam on notifier #629: an OSError escaped _take.
    files = files_module.Files(type("Rooms", (), {"root": tmp_path, "clock": staticmethod(lambda: 0.0)})())

    def full(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(files_module.Path, "write_bytes", full)
    body = {"sha": "a" * 64, "folder": FOLDER, "name": "shot.jpg", "index": 0, "count": 2, "size": 10,
            "data": b"x" * 5}
    assert files._take("link1", body) is None


def test_a_full_disk_drops_a_piece_before_its_written(tmp_path, monkeypatch):
    # GLM on notifier #629: an OSError checking room for a piece escaped _take.
    files = files_module.Files(type("Rooms", (), {"root": tmp_path, "clock": staticmethod(lambda: 0.0)})())

    def full(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(files_module.shutil, "disk_usage", full)
    body = {"sha": "a" * 64, "folder": FOLDER, "name": "shot.jpg", "index": 0, "count": 2, "size": 10,
            "data": b"x" * 5}
    assert files._take("link1", body) is None


def test_another_homes_file_under_the_same_name_isnt_replaced_or_answered(copies):  # noqa: F811
    # Greptile on notifier #629: a home linked here could send its own bytes first under another's upload name.
    room_id = started(copies)
    planted = carried(copies, "laifu", "shot.jpg")
    planted.parent.mkdir(parents=True)
    planted.write_bytes(b"not it")
    with files_module.Store(copies.tmp_path / "laifu").update() as state:
        state.setdefault("kept", {})[f"{FOLDER}/shot.jpg"] = {"link": "link9", "at": 0}
    path = upload(copies, "shot.jpg", b"\xff\xd8 the real one")
    post(copies, room_id, f"@file:{path}\n\nWhat's this?")

    copies.furina.pass_once()

    assert planted.read_bytes() == b"not it"
    assert carried(copies, "mona", "shot.jpg").read_bytes() == b"\xff\xd8 the real one"
    assert len(_sent(copies, "room.file_got")) == 1
    assert files_module.origin_of(copies.tmp_path / "mona", FOLDER, "shot.jpg") is not None


def test_a_file_shared_in_two_rooms_goes_while_either_has_the_link(copies):  # noqa: F811
    # Greptile on notifier #629: only the first room that shared a file was kept with it.
    room_id = started(copies)
    path = upload(copies, "shot.jpg", b"\xff\xd8 shot")
    store = files_module.Store(copies.tmp_path / "furina")
    with store.update() as state:
        state["out"]["link1"] = {f"{FOLDER}/shot.jpg": {
            "room": "room-gone", "rooms": ["room-gone", room_id], "size": path.stat().st_size,
            "base": str(path.parent.parent), "at": None, "tries": 0, "done": False, "noted": 0}}

    copies.furina.pass_once()

    assert carried(copies, "laifu", "shot.jpg").read_bytes() == b"\xff\xd8 shot"
    assert {body["room_id"] for _, body, _ in _sent(copies, "room.file")} == {room_id}


def test_a_file_shared_again_in_another_room_is_kept_with_both(copies):  # noqa: F811
    path = upload(copies, "shot.jpg", b"\xff\xd8 shot")
    events = [{"kind": "message.user", "payload": {"text": f"@file:{path}\nhi"}}]
    copies.furina.files.note("room-one", "link1", events)
    copies.furina.files.note("room-two", "link1", events)
    copies.furina.files.note("room-two", "link1", events)

    entry = files_module.Store(copies.tmp_path / "furina").read()["out"]["link1"][f"{FOLDER}/shot.jpg"]
    assert entry["rooms"] == ["room-one", "room-two"]


def test_a_carried_files_sender_is_saved_before_the_file_shows(copies, monkeypatch):  # noqa: F811
    # Greptile on notifier #629: a turn waiting for the copy could see it before its sender was saved, and a
    # sweep between the two forgot the sender; both now happen under the disk lock the sweep takes.
    room_id = started(copies)
    path = upload(copies, "shot.jpg", b"\xff\xd8 shot")
    noted, shown = [], []
    note_origin, replace = files_module.Files._note_origin, files_module.os.replace

    def noting(self, key, link_id):
        noted.append(files_module._disk_thread_lock.locked())
        return note_origin(self, key, link_id)

    def replacing(source, target):
        if pathlib.Path(target).name == "shot.jpg":
            shown.append(files_module.origin_of(pathlib.Path(target).parents[3], FOLDER, "shot.jpg"))
        return replace(source, target)

    monkeypatch.setattr(files_module.Files, "_note_origin", noting)
    monkeypatch.setattr(files_module.os, "replace", replacing)
    post(copies, room_id, f"@file:{path}\nhi")

    copies.furina.pass_once()

    assert noted == [True, True]
    assert len(shown) == 2 and all(shown)


def test_senders_past_the_cap_go_with_their_files_gone_ones_first(tmp_path, monkeypatch):
    # Greptile on notifier #629: the cap dropped senders of files still kept, which then never read.
    monkeypatch.setattr(files_module, "MAX_ORIGINS", 2)
    clock = [10.0]
    files = files_module.Files(type("Rooms", (), {"root": tmp_path, "clock": staticmethod(lambda: clock[0])})())
    root = files_module.files_root(tmp_path)
    for name in ("old.jpg", "new.jpg"):
        (root / FOLDER).mkdir(parents=True, exist_ok=True)
        (root / FOLDER / name).write_bytes(b"x")
    with files.store.update() as state:
        state["kept"] = {f"{FOLDER}/gone.jpg": {"link": "link1", "at": 0}, f"{FOLDER}/old.jpg": {"link": "link1", "at": 2},
                         f"{FOLDER}/new.jpg": {"link": "link1", "at": 1}}

    files._note_origin(f"{FOLDER}/next.jpg", "link2")

    assert set(files.store.read()["kept"]) == {f"{FOLDER}/old.jpg", f"{FOLDER}/next.jpg"}
    assert not (root / FOLDER / "new.jpg").exists(), "the oldest file goes with its sender"
    assert (root / FOLDER / "old.jpg").exists()
    assert files_module.origin_of(tmp_path, FOLDER, "old.jpg") == "link1"

    files.sweep()
    assert set(files.store.read()["kept"]) == {f"{FOLDER}/old.jpg"}, "the sweep forgets files that aren't here"


def test_a_guest_takes_a_disbanded_rooms_files_but_not_one_it_left(copies):  # noqa: F811
    # GLM on notifier #629: a room's last files were refused once it disbanded, though its pages still came.
    room_id = started(copies)
    for side, status in ((copies.laifu, "left"), (copies.mona, "ended")):
        with side.store.update() as state:
            for invite in state["invites"].values():
                if invite["room_id"] == room_id:
                    invite.update(status=status, reason="left" if status == "left" else "disbanded")
    path = upload(copies, "shot.jpg", b"\xff\xd8 shot")
    post(copies, room_id, f"@file:{path}\nhi")

    copies.furina.pass_once()

    assert not carried(copies, "laifu", "shot.jpg").exists()
    assert carried(copies, "mona", "shot.jpg").read_bytes() == b"\xff\xd8 shot"


def test_a_guest_whose_agents_were_taken_out_takes_no_files(copies):  # noqa: F811
    # DeepSeek on notifier #629: a removed invite ends too, and its home could still send.
    room_id = started(copies)
    for side in (copies.laifu, copies.mona):
        with side.store.update() as state:
            for invite in state["invites"].values():
                if invite["room_id"] == room_id:
                    invite.update(status="ended", reason="removed")
    path = upload(copies, "shot.jpg", b"\xff\xd8 shot")
    post(copies, room_id, f"@file:{path}\nhi")

    copies.furina.pass_once()

    assert not carried(copies, "laifu", "shot.jpg").exists()
    assert not carried(copies, "mona", "shot.jpg").exists()


def test_an_answer_for_a_file_not_sent_yet_doesnt_count(copies):  # noqa: F811
    # Beam on notifier #629: a room.file_got with no digest marked a noted file done before it went.
    started(copies)
    store = files_module.Store(copies.tmp_path / "furina")
    with store.update() as state:
        state["out"]["link1"] = {f"{FOLDER}/shot.jpg": {"room": "r", "size": 1, "at": None, "tries": 0,
                                                       "done": False, "noted": 0}}
    received = files_module.room_links.Received(link_id="link1", kind=files_module.GOT, message_id="m", at=0,
                                                body={"v": files_module.VERSION, "folder": FOLDER, "name": "shot.jpg"})
    copies.furina.files.on_got(received)

    assert store.read()["out"]["link1"][f"{FOLDER}/shot.jpg"]["done"] is False


def test_a_noted_file_that_moved_or_grew_is_dropped_unsent(copies, monkeypatch):  # noqa: F811
    # GLM on notifier #629: _send_file's guards had no tests.
    room_id = started(copies)
    moved = upload(copies, "moved.jpg", b"x")
    grown = upload(copies, "grown.jpg", b"y")
    store = files_module.Store(copies.tmp_path / "furina")
    entry = {"room": room_id, "rooms": [room_id], "size": 1, "at": None, "tries": 0, "done": False, "noted": 0}
    with store.update() as state:
        state["out"]["link1"] = {f"{FOLDER}/moved.jpg": {**entry, "base": str(copies.tmp_path / "elsewhere")},
                                 f"{FOLDER}/grown.jpg": {**entry, "base": str(grown.parent.parent)}}
    monkeypatch.setattr(files_module, "MAX_FILE_BYTES", 0)

    copies.furina.pass_once()

    assert moved.exists() and grown.exists()
    assert _sent(copies, "room.file") == []
    assert store.read()["out"].get("link1", {}) == {}


def test_a_folder_that_wont_go_isnt_counted_as_freed(tmp_path, monkeypatch):
    # DeepSeek on notifier #629: a folder rmtree couldn't remove was still taken off the total.
    root = files_module.files_root(tmp_path)
    stuck = root / ("20261101-" + "3" * 32)
    stuck.mkdir(parents=True)
    (stuck / "a.bin").write_bytes(b"x" * (160_000 + 1))
    monkeypatch.setattr(files_module, "TOTAL_BYTES", 12)
    monkeypatch.setattr(files_module.shutil, "rmtree", lambda *_args, **_kwargs: None)

    assert files_module._room_for(root, 5, stuck.stat().st_mtime + 60) is False


def test_a_disbanded_rooms_last_files_go_after_its_last_page(copies):  # noqa: F811
    # Greptile on notifier #629: once a disbanded room's copy had its end, the files still owed were dropped.
    room_id = started(copies)
    path = upload(copies, "shot.jpg", b"\xff\xd8 last")
    copies.furina.disbanded(room_id)
    from test_room_copies import copies_module

    with copies.furina.store.update() as state:
        copies_module._copy_of(state["drafts"][room_id], "link1", keep=True)["done"] = True
    store = files_module.Store(copies.tmp_path / "furina")
    with store.update() as state:
        state["out"]["link1"] = {f"{FOLDER}/shot.jpg": {
            "room": room_id, "rooms": [room_id], "size": path.stat().st_size, "base": str(path.parent.parent),
            "at": None, "tries": 0, "done": False, "noted": 0}}

    copies.furina.files.send_files()

    assert {link for link, _, _ in _sent(copies, "room.file")} == {"link1"}


def test_a_sender_stays_while_its_file_wont_go(tmp_path, monkeypatch):
    # DeepSeek on notifier #629: the cap dropped a sender whose file couldn't be deleted.
    monkeypatch.setattr(files_module, "MAX_ORIGINS", 2)
    files = files_module.Files(type("Rooms", (), {"root": tmp_path, "clock": staticmethod(lambda: 10.0)})())
    root = files_module.files_root(tmp_path)
    (root / FOLDER).mkdir(parents=True)
    for name in ("held.jpg", "next.jpg"):
        (root / FOLDER / name).write_bytes(b"x")
    with files.store.update() as state:
        state["kept"] = {f"{FOLDER}/held.jpg": {"link": "link1", "at": 1}, f"{FOLDER}/next.jpg": {"link": "link1", "at": 2}}
    unlink = pathlib.Path.unlink

    def held(self, *args, **kwargs):
        if self.name == "held.jpg":
            raise PermissionError(13, "in use")
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(files_module.Path, "unlink", held)

    files._note_origin(f"{FOLDER}/new.jpg", "link2")

    assert set(files.store.read()["kept"]) == {f"{FOLDER}/held.jpg", f"{FOLDER}/new.jpg"}
    assert not (root / FOLDER / "next.jpg").exists()


def test_an_update_doesnt_save_over_a_store_it_couldnt_read(tmp_path, monkeypatch):
    # DeepSeek on notifier #629: a transient read error made an update save an empty store over it.
    import pytest

    store = files_module.Store(tmp_path)
    with store.update() as state:
        state["out"]["link1"] = {"k": {"done": True}}
    before = store.path.read_bytes()
    read_bytes = pathlib.Path.read_bytes

    def flaky(self):
        if self == store.path:
            raise PermissionError(13, "not now")
        return read_bytes(self)

    monkeypatch.setattr(files_module.Path, "read_bytes", flaky)
    with pytest.raises(OSError):
        with store.update() as state:
            state["out"]["link2"] = {}
    monkeypatch.undo()
    assert store.path.read_bytes() == before


def test_a_message_too_big_for_a_page_still_carries_its_files(copies, monkeypatch):  # noqa: F811
    # Greptile on notifier #629: the page drops a too-big message's payload, and its files went with it.
    from test_room_copies import copies_module

    room_id = started(copies)
    monkeypatch.setattr(copies_module, "PAGE_BYTES", 4000)
    path = upload(copies, "shot.jpg", b"\xff\xd8 big")
    post(copies, room_id, f"@file:{path}\n\n" + "x" * 5000)

    for _ in range(3):
        copies.furina.pass_once()

    assert carried(copies, "laifu", "shot.jpg").read_bytes() == b"\xff\xd8 big"


def test_a_file_that_cant_be_read_just_now_stays_owed(copies, monkeypatch):  # noqa: F811
    # DeepSeek on notifier #629: any read error dropped the carry for good.
    room_id = started(copies)
    path = upload(copies, "shot.jpg", b"\xff\xd8 shot")
    store = files_module.Store(copies.tmp_path / "furina")
    with store.update() as state:
        state["out"]["link1"] = {f"{FOLDER}/shot.jpg": {
            "room": room_id, "rooms": [room_id], "size": path.stat().st_size, "base": str(path.parent.parent),
            "at": None, "tries": 0, "done": False, "noted": 0}}
    read_bytes = pathlib.Path.read_bytes

    def busy(self):
        if self.name == "shot.jpg":
            raise PermissionError(13, "busy")
        return read_bytes(self)

    monkeypatch.setattr(files_module.Path, "read_bytes", busy)
    copies.furina.files.send_files()
    assert f"{FOLDER}/shot.jpg" in store.read()["out"]["link1"]
    monkeypatch.undo()

    copies.furina.pass_once()

    assert carried(copies, "laifu", "shot.jpg").read_bytes() == b"\xff\xd8 shot"
