"""Hermes loads dashboard/plugin_api.py, and the routes and the tools behind
them are in dashboard/api/, one module per feature (notifier#443). These hold
the layout's rules (CONTRIBUTING.md): plugin_api.py only loads the package,
a module takes another's names through the module, never by name, and uses
only the modules above it as it loads, and every route is on core's router."""

import ast
import builtins
import importlib.util
import re
import symtable
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
API = DASHBOARD / "api"
PROBES = sorted(p.stem for p in (ROOT / "probes").glob("*.py") if "def load_plugin_api(" in p.read_text())
MODULE_SIZE_LIMIT = 150_000  # bytes: what an agent or a reviewer still reads whole
MODULE_GLOBALS = {"__name__", "__file__", "__package__", "__spec__", "__doc__", "__loader__", "__builtins__"}


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_layout", DASHBOARD / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()
MODULES = api._api.MODULES


def _source(name):
    return (API / f"{name}.py").read_text(encoding="utf-8")


def _siblings(tree):
    """The modules a module takes: ``from . import core, rooms``."""
    return {a.asname or a.name for node in tree.body if isinstance(node, ast.ImportFrom) and node.level
            for a in node.names}


def _scopes(table):
    yield table
    for child in table.get_children():
        yield from _scopes(child)


def _annotation_names(tree):
    """The names in annotations a caller may evaluate in the module's globals
    (FastAPI does, for a route's parameters): a function's, and a module's or a
    class's own. ``from __future__ import annotations`` keeps them out of
    symtable."""
    annotations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            annotations += [arg.annotation for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]
                            if arg is not None and arg.annotation is not None]
            annotations += [node.returns] if node.returns is not None else []
        elif isinstance(node, (ast.Module, ast.ClassDef)):
            annotations += [s.annotation for s in node.body if isinstance(s, ast.AnnAssign)]
    names = set()
    for annotation in annotations:
        for node in ast.walk(annotation):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                names |= {n.id for n in ast.walk(ast.parse(node.value, mode="eval")) if isinstance(n, ast.Name)}
            elif isinstance(node, ast.Name):
                names.add(node.id)
    return names


def _run_as_it_loads(node):
    """What Python runs of a top-level statement as its module loads: all of
    it but the bodies of functions and lambdas. A def's decorators and
    defaults run, and so may its annotations (FastAPI reads a route's as its
    decorator runs); a class's body runs."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = node.args
        parts = getattr(node, "decorator_list", []) + a.defaults + [d for d in a.kw_defaults if d is not None]
        parts += [arg.annotation for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]
                  if arg is not None and arg.annotation is not None]
        parts += [node.returns] if getattr(node, "returns", None) is not None else []
        for part in parts:
            yield from _run_as_it_loads(part)
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _run_as_it_loads(child)


def _globals(name):
    """What a module defines at its top level (or through ``global`` in a
    function), and the global names its code takes."""
    source = _source(name)
    defined, used = set(), _annotation_names(ast.parse(source))
    for table in _scopes(symtable.symtable(source, f"{name}.py", "exec")):
        for symbol in table.get_symbols():
            if table.get_type() == "module":
                if symbol.is_assigned() or symbol.is_imported():
                    defined.add(symbol.get_name())
                if symbol.is_referenced():
                    used.add(symbol.get_name())
            else:
                if symbol.is_declared_global() and symbol.is_assigned():
                    defined.add(symbol.get_name())
                if symbol.is_global() and symbol.is_referenced():
                    used.add(symbol.get_name())
    return defined, used


def test_the_package_runs_every_module_and_plugin_api_lists_them_in_its_order():
    assert sorted(MODULES) == sorted(path.stem for path in API.glob("*.py") if path.stem != "__init__")
    assert tuple(re.findall(r"^    (\w+)  ", api.__doc__, re.M)) == MODULES


def test_plugin_api_only_loads_the_package():
    table = symtable.symtable((DASHBOARD / "plugin_api.py").read_text(encoding="utf-8"), "plugin_api.py", "exec")
    assert {s.get_name() for s in table.get_symbols() if s.is_assigned()} == {"_load_api", "_api", "router", "_name"}


@pytest.mark.parametrize("name", PROBES)
def test_each_probe_finds_what_it_needs_in_the_loaded_file(name, monkeypatch):
    # A probe checks the names it needs before it runs, and no other test runs
    # that check. aiortc and av, which only a call needs, stand in when absent.
    try:
        import aiortc.mediastreams  # noqa: F401
        import av  # noqa: F401
    except ImportError:
        for module, names in (("av", {}), ("aiortc", {"RTCPeerConnection": object, "RTCSessionDescription": object}),
                              ("aiortc.mediastreams", {"AudioStreamTrack": object, "MediaStreamError": Exception})):
            monkeypatch.setitem(sys.modules, module, types.SimpleNamespace(**names))
    spec = importlib.util.spec_from_file_location(f"conduit_layout_probe_{name}", ROOT / "probes" / f"{name}.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    loaded = probe.load_plugin_api()
    assert loaded.router is loaded.core.router


def test_a_module_takes_another_by_module():
    for name in MODULES:
        for node in ast.walk(ast.parse(_source(name))):
            if isinstance(node, ast.ImportFrom) and node.level:
                taken = ", ".join(a.name for a in node.names)
                assert node.level == 1 and node.module is None and node.col_offset == 0, \
                    f"{name}.py:{node.lineno}: from {'.' * node.level}{node.module or ''} import {taken}; " \
                    "take the module at the top instead (from . import core) and use core.TokenError"
                assert all(a.name in MODULES and a.asname is None for a in node.names), f"{name}.py:{node.lineno}"
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                assert all(a.name != "*" for a in node.names), f"{name}.py:{node.lineno}: a star import"


def test_a_module_uses_only_the_modules_above_it_as_it_loads():
    # Every module is created before the first one runs, so a function may
    # use any of them; what runs as a module loads (a base class, a default,
    # a constant made from another module's) finds a module below it not yet
    # run. A branch counts whether it's taken or not.
    for position, name in enumerate(MODULES):
        tree = ast.parse(_source(name))
        below = _siblings(tree) & set(MODULES[position + 1:])
        early = sorted({f"line {n.lineno}: {n.id}" for statement in tree.body for n in _run_as_it_loads(statement)
                        if isinstance(n, ast.Name) and n.id in below})
        assert not early, f"{name}.py uses modules below it as it loads: {early}"


def test_no_name_in_a_module_is_defined_twice():
    # A second def of a name silently replaces the first: both are kept as
    # written, and every caller gets the second.
    for name in MODULES:
        seen = {}
        for node in ast.parse(_source(name)).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                targets = [node.name]
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = [t.id for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
                           if isinstance(t, ast.Name)]
            else:
                continue
            for target in targets:
                assert target not in seen, f"{name}.py:{node.lineno}: {target} again (line {seen[target]})"
                seen[target] = node.lineno


def test_every_name_a_module_takes_is_defined():
    known = set(dir(builtins)) | MODULE_GLOBALS
    for name in MODULES:
        defined, used = _globals(name)
        assert not used - defined - known, f"{name}.py takes names it doesn't define: {sorted(used - defined - known)}"


def test_every_name_taken_from_another_module_is_there():
    defined = {name: _globals(name)[0] for name in MODULES}
    for name in MODULES:
        tree = ast.parse(_source(name))
        siblings = _siblings(tree)
        missing = sorted({f"{n.value.id}.{n.attr}" for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                          and isinstance(n.value, ast.Name) and n.value.id in siblings
                          and n.attr not in defined[n.value.id]})
        assert not missing, f"{name}.py takes what its modules don't define: {missing}"


def test_no_module_is_taken_under_a_name_given_to_something_else():
    # ``for pulse in ...`` in a function of a module that takes pulse makes
    # pulse.X in that function the loop's value, not the module's.
    for name in MODULES:
        source = _source(name)
        tree = ast.parse(source)
        siblings = _siblings(tree)
        blocks = {(n.name, n.lineno): n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        for table in _scopes(symtable.symtable(source, f"{name}.py", "exec")):
            for symbol in table.get_symbols():
                module = symbol.get_name()
                if module not in siblings:
                    continue
                if table.get_type() == "module":
                    assert not symbol.is_assigned(), f"{name}.py gives {module} a value of its own"
                    continue
                if not (symbol.is_assigned() or symbol.is_parameter() or symbol.is_imported()):
                    continue
                block = blocks.get((table.get_name(), table.get_lineno()))
                assert block is not None, f"{name}.py:{table.get_lineno()}: a {table.get_type()} names something {module}"
                assert not any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == module
                               for n in ast.walk(block)), \
                    f"{name}.py:{block.lineno}: {block.name} names something {module} and takes {module}.X"


def test_each_module_stays_small_enough_to_read_whole():
    for name in MODULES:
        size = len(_source(name).encode("utf-8"))
        assert size <= MODULE_SIZE_LIMIT, f"{name}.py is {size} bytes: give part of it a module of its own"


def test_every_route_is_on_cores_router_behind_its_dependency():
    assert api.router is api.core.router
    decorators = 0
    for name in MODULES:
        source = _source(name)
        assert name == "core" or "APIRouter(" not in source, f"{name}.py makes a router of its own"
        decorators += len(re.findall(r"^@router\.(?:get|post|put|patch|delete|websocket)\(", source, re.M))
    assert len(api.router.routes) == decorators
    for route in api.router.routes:
        assert api.core._note_conduit_request in [d.dependency for d in route.dependencies], route.path


def _segment_pattern(segment):
    return "".join(".*" if part.startswith("{") and ":path" in part else "[^/]+" if part.startswith("{")
                   else re.escape(part) for part in re.split(r"(\{[^}]+\})", segment))


def _paths_overlap(first, second):
    a, b = first.strip("/").split("/"), second.strip("/").split("/")
    if ":path}" in first + second:
        return True
    return len(a) == len(b) and all(
        x == y or re.fullmatch(_segment_pattern(x), y) or re.fullmatch(_segment_pattern(y), x) for x, y in zip(a, b))


def test_no_two_routes_answer_the_same_request():
    # So the order the modules run in never decides which route answers.
    routes = [(set(getattr(route, "methods", None) or {"WEBSOCKET"}), route.path) for route in api.router.routes]
    for i, (methods, path) in enumerate(routes):
        for other_methods, other_path in routes[i + 1:]:
            assert not (methods & other_methods and _paths_overlap(path, other_path)), f"{path} and {other_path}"
