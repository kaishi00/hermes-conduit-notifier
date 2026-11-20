"""A file's stamp as a stat sees it, for remembering a read until the file
changes: the hooks that run for every tool call in every chat ask the same
stores the same question, and the answer is nearly always "nothing"
(notifier issue 960)."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterable, Optional

# A file written this recently may be written again within its timestamp's
# resolution without its stamp changing, so it isn't trusted yet (as
# calls_store's settled signature).
SETTLED_NS = 2_000_000_000


def settled(paths: Iterable[Path]) -> Optional[tuple]:
    """Each path's (inode, mtime, ctime, size), None for one that's missing;
    None for the whole when the first is missing or any was just written. A
    SQLite store passes its file and its ``-wal``: every commit writes one."""
    stamp = []
    now = time.time_ns()
    for index, path in enumerate(paths):
        try:
            info = os.stat(path)
        except OSError:
            if index == 0:
                return None
            stamp.append(None)
            continue
        if now - max(info.st_mtime_ns, info.st_ctime_ns) < SETTLED_NS:
            return None
        stamp.append((info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size))
    return tuple(stamp)


def sqlite_store(path: Path) -> Optional[tuple]:
    """``settled`` for a SQLite file in WAL mode. An empty ``-wal`` holds
    nothing and counts as missing: opening the store to read it creates or
    touches one without changing what it holds, while a commit makes it
    longer, or rewrites it from its start after a checkpoint changed the file
    itself."""
    path = Path(path)
    wal = path.with_name(path.name + "-wal")
    try:
        empty = os.stat(wal).st_size == 0
    except OSError:
        empty = True
    return settled((path,) if empty else (path, wal))
