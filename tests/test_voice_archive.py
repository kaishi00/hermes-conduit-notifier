"""Voice chats archived at call end come back once used (hermes-conduit#1124)."""
import importlib.util
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("conduit_voice_archive", ROOT / "voice_archive.py")
voice_archive = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(voice_archive)


def _home(tmp_path, rows, tags):
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, archived INTEGER, auto_archived INTEGER)")
    conn.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO sessions VALUES (?, ?, ?)", rows)
    conn.execute("INSERT INTO state_meta VALUES (?, ?)", (voice_archive.VOICE_TAGS_KEY, json.dumps(tags)))
    conn.commit()
    conn.close()
    return tmp_path


class FakeSessionDB:
    """Hermes' write path: its reopen un-archives what the sweep stamped."""
    opened = 0

    def __init__(self, path):
        FakeSessionDB.opened += 1
        self.conn = sqlite3.connect(path)

    def _execute_write(self, fn):
        with self.conn:
            return fn(self.conn)

    @staticmethod
    def _unarchive_auto_archived_lineage(conn, session_id):
        return conn.execute("UPDATE sessions SET archived = 0, auto_archived = 0 "
                            "WHERE id = ? AND archived <> 0 AND auto_archived <> 0", (session_id,)).rowcount > 0

    def close(self):
        self.conn.close()


def _flags(home, session_id):
    conn = sqlite3.connect(home / "state.db")
    try:
        return conn.execute("SELECT archived, auto_archived FROM sessions WHERE id = ?", (session_id,)).fetchone()
    finally:
        conn.close()


def test_a_classic_chat_call_or_voice_job_archived_at_call_end_comes_back(tmp_path):
    home = _home(tmp_path, [("classic", 1, 1), ("call", 1, 1), ("job", 1, 1)],
                 {"classic": {"kind": "classic"}, "call": {"kind": "call", "engine": "gpt-live"},
                  "job": {"kind": "job"}})
    for session_id in ("classic", "call", "job"):
        assert voice_archive.bring_back(home, session_id, open_db=FakeSessionDB)
        assert _flags(home, session_id) == (0, 0)


def test_other_rows_are_left_alone_without_opening_the_store_for_writes(tmp_path):
    home = _home(tmp_path, [
        ("live", 0, 0),          # most turns: not archived
        ("on-purpose", 1, 0),    # archived by someone
        ("swept", 1, 1),         # Hermes' idle sweep, not a voice chat
    ], {"on-purpose": {"kind": "classic"}})
    FakeSessionDB.opened = 0
    for session_id in ("live", "on-purpose", "swept", "missing", ""):
        assert not voice_archive.bring_back(home, session_id, open_db=FakeSessionDB)
    assert FakeSessionDB.opened == 0
    assert _flags(home, "on-purpose") == (1, 0) and _flags(home, "swept") == (1, 1)


def test_a_missing_store_an_unreadable_tag_list_or_an_older_hermes_never_raise(tmp_path):
    assert not voice_archive.bring_back(tmp_path / "nowhere", "x", open_db=FakeSessionDB)
    home = _home(tmp_path, [("classic", 1, 1)], {})
    conn = sqlite3.connect(home / "state.db")
    conn.execute("UPDATE state_meta SET value = 'not json'")
    conn.commit()
    conn.close()
    assert not voice_archive.bring_back(home, "classic", open_db=FakeSessionDB)

    class OlderHermes:
        def __init__(self, path):
            pass

        def close(self):
            pass

    conn = sqlite3.connect(home / "state.db")
    conn.execute("UPDATE state_meta SET value = ?", (json.dumps({"classic": {"kind": "classic"}}),))
    conn.commit()
    conn.close()
    assert not voice_archive.bring_back(home, "classic", open_db=OlderHermes)
    assert _flags(home, "classic") == (1, 1)


# --- Voice jobs ---------------------------------------------------------------

JOB_PROMPT = ("[Background job started from a Conduit voice conversation. Nobody is watching this chat live, "
              "so work on it on your own.]\n\nCheck the GPU box")
NEXT_PROMPT = "[The next request from the same Conduit voice conversation. As before, ...]\n\nAnd the NAS"


def _job_home(tmp_path, chats, tags):
    """chats: id -> (pinned, archived, [(role, content), ...])."""
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, archived INTEGER, auto_archived INTEGER, "
                 "pinned INTEGER)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
                 "content TEXT)")
    conn.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
    for chat_id, (pinned, archived, messages) in chats.items():
        conn.execute("INSERT INTO sessions VALUES (?, ?, 0, ?)", (chat_id, archived, pinned))
        conn.executemany("INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                         [(chat_id, role, content) for role, content in messages])
    conn.execute("INSERT INTO state_meta VALUES (?, ?)", (voice_archive.VOICE_TAGS_KEY, json.dumps(tags)))
    conn.commit()
    conn.close()
    return tmp_path


class JobSessionDB(FakeSessionDB):
    """The reads settled_voice_job makes, and the idle sweep's archive."""

    def get_meta(self, key):
        row = self.conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def get_session(self, session_id):
        row = self.conn.execute("SELECT id, archived, pinned FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return {"id": row[0], "archived": row[1], "pinned": row[2]} if row else None

    def get_messages(self, session_id):
        return [{"role": role, "content": content} for role, content in self.conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id", (session_id,))]

    def _auto_archive_lineage(self, session_id):
        with self.conn:
            return self.conn.execute("UPDATE sessions SET archived = 1, auto_archived = 1 "
                                     "WHERE id = ? AND archived = 0", (session_id,)).rowcount > 0


def test_a_voice_job_hermes_answered_leaves_the_list(tmp_path):
    home = _job_home(tmp_path, {
        "job": (0, 0, [("user", JOB_PROMPT), ("assistant", "Both GPUs are idle.")]),
        # The Watch's later request in the call's chat, and a correction.
        "next": (0, 0, [("user", JOB_PROMPT), ("assistant", "Idle."), ("user", NEXT_PROMPT),
                        ("user", "(voice) the old NAS"), ("assistant", "It's full.")]),
    }, {"job": {"kind": "job"}, "next": {"kind": "job", "parent_id": "call-1"}})
    for session_id in ("job", "next"):
        assert voice_archive.archive_answered_job(home, session_id, open_db=JobSessionDB)
        assert _flags(home, session_id) == (1, 1)
    # The next request voice sends there brings it back; its answer archives it again.
    assert voice_archive.bring_back(home, "job", open_db=JobSessionDB)
    assert voice_archive.archive_answered_job(home, "job", open_db=JobSessionDB)


def test_a_voice_job_someone_used_or_kept_stays_listed(tmp_path):
    home = _job_home(tmp_path, {
        "typed": (0, 0, [("user", JOB_PROMPT), ("assistant", "Idle."), ("user", "thanks, now the NAS"),
                         ("assistant", "Full.")]),
        "running": (0, 0, [("user", JOB_PROMPT)]),
        "silent": (0, 0, [("user", JOB_PROMPT), ("assistant", "  ")]),
        "pinned": (1, 0, [("user", JOB_PROMPT), ("assistant", "Idle.")]),
        "archived": (0, 1, [("user", JOB_PROMPT), ("assistant", "Idle.")]),
    }, {sid: {"kind": "job"} for sid in ("typed", "running", "silent", "pinned", "archived")})
    for session_id in ("typed", "running", "silent", "pinned"):
        assert not voice_archive.archive_answered_job(home, session_id, open_db=JobSessionDB)
        assert _flags(home, session_id) == (0, 0)
    assert not voice_archive.archive_answered_job(home, "archived", open_db=JobSessionDB)
    assert _flags(home, "archived") == (1, 0)


def test_only_a_tagged_job_that_voice_started_opens_the_store(tmp_path):
    home = _job_home(tmp_path, {
        # The chat an unattached call took for its first request is the
        # call's chat, not a job: Conduit never tags it.
        "call-chat": (0, 0, [("user", JOB_PROMPT), ("assistant", "Idle.")]),
        "chat": (0, 0, [("user", "hello"), ("assistant", "hi")]),
        "classic": (0, 0, [("user", "(voice) hi"), ("assistant", "hi")]),
    }, {"chat": {"kind": "job"}, "classic": {"kind": "classic"}})
    JobSessionDB.opened = 0
    for session_id in ("call-chat", "chat", "classic", "missing", ""):
        assert not voice_archive.archive_answered_job(home, session_id, open_db=JobSessionDB)
    assert JobSessionDB.opened == 0
    assert not voice_archive.archive_answered_job(tmp_path / "nowhere", "chat", open_db=JobSessionDB)


def test_voice_requests_are_known_by_their_mark():
    assert voice_archive.is_voice_request(JOB_PROMPT)
    assert voice_archive.is_voice_request("  " + NEXT_PROMPT)
    assert voice_archive.is_voice_request("(voice) and the NAS")
    for other in ("voice: hi", "Please (voice) hi", None, ["(voice) hi"], ""):
        assert not voice_archive.is_voice_request(other)
