"""Regression guard: Conduit workstream sessions must get terminal/file/code_execution.

Runs the real work_broker params through the real gateway_chats._request rewrite and
asserts the explicit toolsets survive to the wire, and that every name is a real
toolset. Pure offline check — no gateway needed.

    python3 test_workstream_toolsets.py     # exit 0 = pass
"""
import glob
import sys

for p in glob.glob("/home/hermes/.hermes/cache/uv/archive-v0/*/"):
    sys.path.append(p.rstrip("/"))
sys.path.insert(0, "/home/hermes/.hermes/hermes-agent")
sys.path.insert(0, "/home/hermes/.hermes/plugins")

import threading

from conduit_push import gateway_chats as gc
from conduit_push import work_broker as wb
from toolsets import resolve_toolset, validate_toolset

# 1. The list is explicit and only names real toolsets.
assert "terminal" in wb.WORKSTREAM_TOOLSETS
assert "file" in wb.WORKSTREAM_TOOLSETS
assert "code_execution" in wb.WORKSTREAM_TOOLSETS
invalid = [t for t in wb.WORKSTREAM_TOOLSETS if not validate_toolset(t)]
assert not invalid, f"unknown toolset names: {invalid}"

# 2. The tools those toolsets bring actually include what workstreams need.
tools = {t for name in wb.WORKSTREAM_TOOLSETS for t in resolve_toolset(name)}
for need in ("terminal", "read_file", "write_file", "execute_code"):
    assert need in tools, f"{need} missing from resolved toolset"

# 3. Broker.start() must actually send them on session.create.
params = {"cols": 96, "source": "desktop", "title": "t", "hidden": True,
          "idempotency_key": "k", "toolsets": list(wb.WORKSTREAM_TOOLSETS)}
link = gc._AuthorityLink.__new__(gc._AuthorityLink)
link.lock = threading.Lock()
method, wire = link._request("session.create", dict(params))
assert method == "session.create"
assert wire["source"] == "gui", wire["source"]          # desktop is rewritten, not sent raw
assert wire["toolsets"] == list(wb.WORKSTREAM_TOOLSETS)  # survives the rewrite
assert "cols" not in wire and "idempotency_key" not in wire

print("ok: workstream sessions send terminal/file/code_execution and they resolve")
