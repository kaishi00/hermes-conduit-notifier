"""Reviews: the options a review card shows, kept on the host round by
round, with the marks the user sent on each.

``conduit_show`` takes a review card's options with their pages (an HTML
page, a page or picture on the host, or a picture's https link); the card
the chat keeps carries only labels and ids, and Conduit reads a round's
pages here when the user opens the review (``GET /boards/reviews``). The
user marks each decision Approve, Change or Skip and sends the marks
through ``POST /boards/taps`` (card_actions), which saves them on their
round; the next round is the same review shown again with its id.

A review shown in a Bot Chat for a workstream (``for``) stays with that
workstream for every round: the plugin hands it the marks
(work_tool.forward_review) and refuses a round of it for another one.

Kept in the root Hermes home, as kept diffs are: the id is all a reader
needs. A review nobody touched in a month goes, and the newest
MAX_REVIEWS stay.

Imports nothing from the plugin package, so the dashboard can load it too.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

DIRECTORY = "conduit-reviews"
META = "review.json"
REVIEW_ID = re.compile(r"rv-[0-9a-f]{16}")
KEEP_S = 30 * 24 * 3600
MAX_REVIEWS = 300
MAX_ROUNDS = 99
MAX_SESSIONS = 8
# A review whose last round is this old is no longer named on its chat's turns.
OPEN_NOTE_S = 3 * 24 * 3600
# What one option's page may hold, and a whole round's pages.
MAX_HTML_CHARS = 300_000
MAX_HTML_FILE_BYTES = 1_000_000
MAX_IMAGE_FILE_BYTES = 2_000_000
MAX_ROUND_BYTES = 6_000_000
MARKS = ("approve", "change", "skip")
_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                ".webp": "image/webp", ".svg": "image/svg+xml"}
_PAGE_TYPES = (".html", ".htm")

_lock = threading.Lock()


class ReviewError(ValueError):
    pass


def picture_page(data: bytes, media_type: str) -> str:
    """A picture on the host as a page of its own, so every option the
    host keeps opens the same way."""
    encoded = base64.b64encode(data).decode("ascii")
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
            "content=\"width=device-width,initial-scale=1\"><style>html,body{margin:0;padding:0;"
            "background:transparent}img{display:block;width:100%;height:auto}</style></head><body>"
            f"<img alt=\"\" src=\"data:{media_type};base64,{encoded}\"></body></html>")


def read_page(option: dict[str, Any]) -> tuple[str, str]:
    """An option's page as the tool gave it: ("page", its HTML) for html or
    a file on this host, ("image", the url) for a picture's https link."""
    given = [key for key in ("html", "file", "image") if isinstance(option.get(key), str) and option[key].strip()]
    if len(given) != 1:
        raise ReviewError("give one of html, file or image")
    key = given[0]
    if key == "html":
        html = option["html"]
        if len(html) > MAX_HTML_CHARS:
            raise ReviewError(f"html is over {MAX_HTML_CHARS:,} characters; write it to a file instead")
        return "page", html
    if key == "image":
        url = option["image"].strip()
        if not url.lower().startswith("https://") or len(url) > 500 or re.search(r"[\s\"'`<>\\()\[\]{}]", url):
            raise ReviewError("image is a direct https link to a picture")
        return "image", url
    path = os.path.expanduser(option["file"].strip())
    if not os.path.isabs(path):
        raise ReviewError("file is the absolute path of a page or picture on the Hermes host")
    suffix = os.path.splitext(path)[1].lower()
    if suffix not in _PAGE_TYPES and suffix not in _IMAGE_TYPES:
        raise ReviewError("file is an .html page or a .png, .jpg, .gif, .webp or .svg picture")
    limit = MAX_HTML_FILE_BYTES if suffix in _PAGE_TYPES else MAX_IMAGE_FILE_BYTES
    try:
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        if os.path.getsize(path) > limit:
            raise ReviewError(f"file is over {limit // 1_000_000} MB")
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except ReviewError:
        raise
    except FileNotFoundError:
        raise ReviewError("no such file on the Hermes host (a workstream in a container or on another machine "
                          "passes html instead)") from None
    except OSError:
        raise ReviewError("file can't be read on the Hermes host") from None
    if suffix in _PAGE_TYPES:
        return "page", data.decode("utf-8", errors="replace")
    return "page", picture_page(data, _IMAGE_TYPES[suffix])


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as present_store: flock where there is one, a
    # first-byte lock on Windows.
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        handle.seek(0)
        for attempt in range(6):
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                break
            except OSError:
                if attempt == 5:
                    raise
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        yield


def _write_json(path: Path, value: Any) -> None:
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".conduit-review-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _well_formed(meta: Any) -> bool:
    return (isinstance(meta, dict) and isinstance(meta.get("rounds"), list) and isinstance(meta.get("title"), str)
            and isinstance(meta.get("for"), str) and isinstance(meta.get("sessions"), list)
            and isinstance(meta.get("updated"), (int, float)) and not isinstance(meta.get("updated"), bool))


def _settled_round(kept: dict[str, Any]) -> bool:
    """Whether a round's marks leave nothing to change: every decision
    marked, each approved or skipped, and no note."""
    marks = kept.get("marks")
    if not isinstance(marks, dict) or marks.get("note"):
        return False
    entries = marks.get("decisions", [])
    return len(entries) == len(kept.get("decisions", [])) and all(
        entry.get("mark") in ("approve", "skip") and not entry.get("note") for entry in entries)


def _shown_marks(marks: Any) -> Any:
    """Marks as Conduit reads them: without the tap that saved them."""
    if isinstance(marks, dict) and "tap" in marks:
        return {key: value for key, value in marks.items() if key != "tap"}
    return marks


class KeptReviews:
    def __init__(self, root: Any, clock: Callable[[], float] = time.time,
                 new_id: Callable[[], str] = lambda: "rv-" + secrets.token_hex(8)) -> None:
        self.directory = Path(root) / DIRECTORY
        self.clock = clock
        self.new_id = new_id

    # --- one review, under its lock -------------------------------------------------

    def _folder(self, review_id: Any) -> Path:
        if not isinstance(review_id, str) or not REVIEW_ID.fullmatch(review_id):
            raise ReviewError("id names a review: rv- and 16 hex digits")
        return self.directory / review_id

    @contextmanager
    def _locked(self, review_id: str) -> Iterator[Path]:
        folder = self._folder(review_id)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.directory / f".{review_id}.lock"
        with _lock, open(lock_path, "a+b") as handle, _exclusive(handle):
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            yield folder

    def get(self, review_id: Any) -> dict[str, Any] | None:
        """A review's record (its title, workstream, chats and rounds without
        their pages), or None when it's gone."""
        meta = _read_json(self._folder(review_id) / META)
        if not _well_formed(meta) or self.clock() - meta["updated"] >= KEEP_S:
            return None
        return meta

    def start(self, title: str, session: str, for_ref: str = "") -> str:
        """A new review, before its first round; answers its id."""
        review_id = self.new_id()
        with self._locked(review_id) as folder:
            folder.mkdir(mode=0o700, exist_ok=True)
            now = self.clock()
            _write_json(folder / META, {"at": now, "updated": now, "title": title, "for": for_ref,
                                        "sessions": [session] if session else [], "rounds": []})
        self._prune()
        return review_id

    def add_round(self, review_id: str, title: str, decisions: list[dict[str, Any]],
                  pages: dict[str, str], session: str = "") -> int:
        """The next round of a review, its decisions as the card shows them
        and the pages its options open; answers the round's number."""
        size = sum(len(page.encode("utf-8")) for page in pages.values())
        if size > MAX_ROUND_BYTES:
            raise ReviewError(f"the pages come to over {MAX_ROUND_BYTES // 1_000_000} MB; show fewer or smaller ones")
        with self._locked(review_id) as folder:
            meta = _read_json(folder / META)
            if not _well_formed(meta):
                raise ReviewError("that review is gone")
            if len(meta["rounds"]) >= MAX_ROUNDS:
                raise ReviewError(f"a review has {MAX_ROUNDS} rounds at most; start a new one")
            number = len(meta["rounds"]) + 1
            _write_json(folder / f"round-{number}.json", {"pages": pages})
            now = self.clock()
            meta["rounds"].append({"at": now, "decisions": decisions, "marks": None})
            meta["title"] = title or meta["title"]
            meta["updated"] = now
            meta.pop("done", None)
            if session and session not in meta["sessions"]:
                meta["sessions"] = [*meta["sessions"], session][-MAX_SESSIONS:]
            _write_json(folder / META, meta)
        return number

    def save_marks(self, review_id: str, number: int, marks: dict[str, Any], sessions: list[str]) -> None:
        """The marks the user sent on a round; a round that leaves nothing to
        change (every decision approved or skipped, no note) settles the
        review."""
        with self._locked(review_id) as folder:
            meta = _read_json(folder / META)
            if not _well_formed(meta) or not 1 <= number <= len(meta["rounds"]):
                raise ReviewError("that round of the review is gone")
            if number != len(meta["rounds"]):
                # Checked under the lock: a round shown meanwhile is the one to mark.
                raise ReviewError("a newer round of that review is in the chat; mark that one")
            if meta["rounds"][number - 1].get("marks"):
                raise ReviewError("the marks on that round were already sent")
            now = self.clock()
            meta["rounds"][number - 1]["marks"] = {**marks, "at": now}
            meta["updated"] = now
            meta["done"] = _settled_round(meta["rounds"][-1])
            for session in sessions:
                if session and session not in meta["sessions"]:
                    meta["sessions"] = [*meta["sessions"], session][-MAX_SESSIONS:]
            _write_json(folder / META, meta)

    def drop_round(self, review_id: str, number: int) -> None:
        """Takes back the newest round when no card showed it; a review left
        with no round goes."""
        with self._locked(review_id) as folder:
            meta = _read_json(folder / META)
            if not _well_formed(meta) or number != len(meta["rounds"]) or meta["rounds"][-1].get("marks"):
                return
            meta["rounds"].pop()
            try:
                (folder / f"round-{number}.json").unlink()
            except OSError:
                pass
            if meta["rounds"]:
                # A settled review the unshown round reopened is settled again.
                meta["done"] = _settled_round(meta["rounds"][-1])
                _write_json(folder / META, meta)
                return
            shutil.rmtree(folder, ignore_errors=True)  # under the lock: nothing was added meanwhile
        self._unlink_lock(review_id)

    def drop_empty(self, review_id: str) -> None:
        """Removes a review whose first round never saved."""
        with self._locked(review_id) as folder:
            meta = _read_json(folder / META)
            if _well_formed(meta) and meta["rounds"]:
                return
            shutil.rmtree(folder, ignore_errors=True)
        self._unlink_lock(review_id)

    def _unlink_lock(self, review_id: str) -> None:
        """A removed review's lock file, once its lock is let go."""
        try:
            (self.directory / f".{review_id}.lock").unlink()
        except OSError:
            pass

    def drop_marks(self, review_id: str, number: int, tap: str | None = None) -> None:
        """Forgets a round's marks whose message Conduit didn't send, so the
        user can send them again. With a tap, only the marks that tap saved:
        a resend's newer marks stay."""
        with self._locked(review_id) as folder:
            meta = _read_json(folder / META)
            if not _well_formed(meta) or not 1 <= number <= len(meta["rounds"]):
                return
            marks = meta["rounds"][number - 1].get("marks")
            if tap is not None and isinstance(marks, dict) and marks.get("tap") != tap:
                return
            meta["rounds"][number - 1]["marks"] = None
            meta["done"] = _settled_round(meta["rounds"][-1])
            _write_json(folder / META, meta)

    def pages(self, review_id: str, number: int) -> dict[str, str]:
        kept = _read_json(self._folder(review_id) / f"round-{number}.json")
        pages = kept.get("pages") if isinstance(kept, dict) else None
        return {key: value for key, value in pages.items() if isinstance(key, str) and isinstance(value, str)} \
            if isinstance(pages, dict) else {}

    def read(self, review_id: Any, number: Any = None) -> dict[str, Any] | None:
        """A round as Conduit opens it: its decisions, the pages its options
        open (by decision and option id), the marks sent on it, and how many
        rounds there are. The latest round without a number."""
        meta = self.get(review_id)
        if meta is None or not meta["rounds"]:
            return None
        count = len(meta["rounds"])
        if number is None:
            number = count
        if isinstance(number, str) and number.isdigit():
            number = int(number)
        if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= count:
            raise ReviewError(f"round is 1 to {count}")
        entry = meta["rounds"][number - 1]
        return {"id": review_id, "round": number, "rounds": count, "title": meta["title"],
                **({"for": meta["for"]} if meta["for"] else {}), "decisions": entry["decisions"],
                "pages": self.pages(review_id, number), "marks": _shown_marks(entry.get("marks"))}

    def settled(self, review_id: str, keep_ids: set[str]) -> list[dict[str, Any]]:
        """Decisions earlier rounds approved or skipped that the next round
        doesn't show again, each with its latest mark."""
        meta = self.get(review_id)
        if meta is None:
            return []
        latest: dict[str, dict[str, Any]] = {}
        for number, entry in enumerate(meta["rounds"], start=1):
            marks = entry.get("marks") or {}
            titles = {decision["id"]: decision for decision in entry["decisions"]}
            for mark in marks.get("decisions", []):
                decision = titles.get(mark.get("decision"))
                if decision is None:
                    continue
                if mark.get("mark") in ("approve", "skip"):
                    choice = next((option["label"] for option in decision["options"]
                                   if option["id"] == mark.get("option")), "")
                    latest[decision["id"]] = {"title": decision["title"], "mark": mark["mark"], "round": number,
                                              **({"choice": choice} if choice and mark["mark"] == "approve" else {})}
                else:
                    latest.pop(decision["id"], None)
        return [entry for key, entry in latest.items() if key not in keep_ids][:12]

    def open_reviews(self, session: Any) -> list[dict[str, Any]]:
        """The reviews of a chat still waiting on a next round or the user's
        marks, newest first, each with its id."""
        if not isinstance(session, str) or not session:
            return []
        found = []
        now = self.clock()
        try:
            folders = list(self.directory.glob("rv-*"))
        except OSError:
            return []
        for folder in folders:
            if not REVIEW_ID.fullmatch(folder.name):
                continue
            meta = _read_json(folder / META)
            if not _well_formed(meta) or not meta["rounds"] or meta.get("done"):
                continue
            if session not in meta["sessions"] or now - meta["updated"] >= OPEN_NOTE_S:
                continue
            found.append({"id": folder.name, **meta})
        return sorted(found, key=lambda meta: meta["updated"], reverse=True)

    def _prune(self) -> None:
        """The oldest past MAX_REVIEWS go, and any untouched for a month."""
        try:
            kept = sorted(((path.stat().st_mtime, path) for path in self.directory.glob("rv-*") if path.is_dir()),
                          reverse=True)
        except OSError:
            return
        now = self.clock()
        for index, (seen, folder) in enumerate(kept):
            if not REVIEW_ID.fullmatch(folder.name):
                continue
            # Under the review's lock, as every writer: one saving marks or a
            # round meanwhile finishes first, and is looked at again after.
            with self._locked(folder.name):
                try:
                    if folder.stat().st_mtime != seen:
                        continue  # saved since the order above was taken: newer than it ranked
                except OSError:
                    continue
                meta = _read_json(folder / META)
                updated = meta.get("updated") if isinstance(meta, dict) else None
                if not isinstance(updated, (int, float)) or isinstance(updated, bool):
                    try:
                        updated = folder.stat().st_mtime  # a review being started has no record yet
                    except OSError:
                        continue
                if index < MAX_REVIEWS and now - updated < KEEP_S:
                    continue
                shutil.rmtree(folder, ignore_errors=True)
            self._unlink_lock(folder.name)
