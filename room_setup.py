"""Setting a Hermes up for rooms with other people's agents (designs/cross-owner-agents, N3c, decision 13).

A member's agent answers a room's turns through this Hermes' API server,
with the toolsets the API server may use for its profile: its room
ceiling, which each room pins as a policy digest. The switch turns the API
server on (the installation's, in the root home, whichever profile asks),
gives a named profile a key of its own for it (the API server serves a
profile only with that profile's key), saves the profile's ceiling exactly
(``no_mcp``, and every plugin toolset marked known, so a later MCP server,
plugin or Hermes update doesn't change the digest), and makes approvals
manual over off, which rooms refuse.

It writes only what changes something, and what it can't fix (YOLO mode, a
managed install, a setting an administrator pinned, a profile the host
gateway doesn't serve) is reported before anything is written. Whether the
API server runs is read from the gateway's own record of it
(``gateway_state.json``), so the key never leaves the files Hermes keeps it
in, and a key set where this can't read it (a service file's environment)
is never replaced.

Hermes' calls come in through ``Hermes``. Nothing here imports Hermes at
module level, so the dashboard loads it as a private package and the tests
give it a fake.
"""

from __future__ import annotations

import copy
import importlib
import inspect
import logging
import os
import re
import secrets
import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional
from urllib.parse import quote, urlsplit

from . import room_choices, room_identity

logger = logging.getLogger(__name__)

PLATFORM = "api_server"
NO_MCP = "no_mcp"
# Conduit's own tools (call the user, its settings) never run in rooms.
OWN_TOOLSET = "conduit"
# Configurable in Hermes' settings, but not a toolset a turn runs.
SETTINGS_ONLY = frozenset({"stt"})
# What the room choices can turn on: Look things up (web, vision, X search)
# and Search my past chats.
DEFAULT_CEILING = ("session_search", "vision", "web", "x_search")
MAX_CEILING = 64

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
LAST_PORT = 8699
PORT_CHECK_TIMEOUT_S = 0.5
KEY_ENV = "API_SERVER_KEY"
PORT_ENV = "API_SERVER_PORT"
HOST_ENV = "API_SERVER_HOST"
YOLO_ENV = "HERMES_YOLO_MODE"
STATUS_FILE = "gateway_state.json"
# The record's code for an API server its startup guard refused for its key
# (api_server._api_key_passes_startup_guard).
KEY_REFUSED = "api_server_key_invalid"
LOCK_NAME = ".setup.lock"
# --detach: with no service manager, the new gateway starts in the
# background instead of in the terminal that asked (930e5ed and later).
RESTART_ARGS = ("-p", "default", "gateway", "restart", "--detach")

# Where the gateway reads the API server's settings, merged in this order,
# the later winning (gateway.config_loader.merge_platform_sections).
BLOCKS = (("gateway", "platforms", PLATFORM), ("platforms", PLATFORM), ("gateway", PLATFORM))

_WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::", "[::]", "*"})
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_REFERENCE = re.compile(r"^\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}$")
# A terminal escape sequence: a CSI one, or ESC and the character after it.
_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|.)", re.DOTALL)
_thread_lock = threading.Lock()


class SetupError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Hermes:
    """The Hermes calls the switch makes. ``save_env`` writes one value to a
    home's ``.env``; ``toolset_tools`` names a toolset's tools; ``policy``
    is a profile's room execution policy; ``served`` says why the host
    gateway doesn't serve a named profile (``standalone``, ``parked``), or
    None."""

    read_config: Callable[[Path], dict]
    write_config: Callable[[Path, dict], None]
    read_env: Callable[[Path], Mapping[str, str]]
    save_env: Callable[[Path, str, str], None]
    env_refusal: Callable[[str], Optional[str]]
    managed: Callable[[], Optional[str]]
    key_managed: Callable[[str], bool]
    usable_key: Callable[[Any], bool]
    toolsets: Callable[[], list]
    plugin_toolsets: Callable[[], set]
    builtin_toolsets: Callable[[], set]
    toolset_tools: Callable[[str], list]
    allowed: Callable[[str], bool]
    label: Callable[[str], str]
    policy: Callable[[str, Path], Mapping[str, Any]]
    served: Callable[[Path], Optional[str]]
    runtime_status: Callable[[Path], Optional[Mapping[str, Any]]]
    gateway_live: Callable[[Mapping[str, Any]], bool]

    @classmethod
    def load(cls, env_scope: Optional[Callable[[Path], Any]] = None) -> "Hermes":
        """This Hermes' calls. ``env_scope(home)`` is the scope a ``.env``
        write runs in (the dashboard's for the default profile); without
        one, the write runs with the home as Hermes' home. A Hermes without
        one of the calls is refused (501), naming what it lacks in the log."""
        missing: list[str] = []

        def need(module_name: str, *names: str) -> list[Any]:
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                missing.append(module_name)
                return [None] * len(names)
            found = [getattr(module, name, None) for name in names]
            missing.extend(f"{module_name}.{name}" for name, value in zip(names, found) if not callable(value))
            return found

        read_raw, atomic_write, save_env_value, env_write_refusal, is_managed, managed_message = need(
            "hermes_cli.config", "read_user_config_raw", "atomic_config_write", "save_env_value",
            "env_write_refusal", "is_managed", "format_managed_message")
        is_key_managed, = need("hermes_cli.managed_scope", "is_key_managed")
        has_usable_secret, = need("hermes_cli.auth", "has_usable_secret")
        configurable, plugin_keys, builtin_keys, gui_label = need(
            "hermes_cli.tools_config", "_get_effective_configurable_toolsets", "_get_plugin_toolset_keys",
            "_configurable_keys", "gui_toolset_label")
        allowed_for, = need("hermes_cli.toolset_scope", "toolset_allowed_for_platform")
        resolve_toolset, = need("toolsets", "resolve_toolset")
        standalone, parked = need("hermes_cli.profiles", "profile_is_standalone", "profile_is_parked")
        effective_config, = need("hermes_cli.config_effective", "load_user_config_effective")
        policy_mapping, = need("gateway.hosted_room_execution_policy", "execution_policy_mapping")
        read_status, pid_live, stale = need("gateway.status", "read_runtime_status", "runtime_status_pid_is_live",
                                            "runtime_status_is_stale")
        load_env_file, = need("agent.secret_scope", "load_env_file")
        set_home, reset_home = need("hermes_constants", "set_hermes_home_override", "reset_hermes_home_override")
        if missing:
            logger.warning("Linked Rooms setup is off on this Hermes: it has no %s", ", ".join(missing))
            raise SetupError("unsupported", "This Hermes is missing what Linked Rooms setup needs; update Hermes",
                             501)

        @contextmanager
        def as_home(home: Path) -> Iterator[None]:
            token = set_home(home)
            try:
                yield
            finally:
                reset_home(token)

        def save_env(home: Path, key: str, value: str) -> None:
            with (env_scope(home) if env_scope is not None else as_home(home)):
                save_env_value(key, value)

        def served(home: Path) -> Optional[str]:
            if standalone(home):
                return "standalone"
            return "parked" if parked(home) else None

        return cls(
            read_config=lambda path: read_raw(path),
            write_config=lambda path, data: atomic_write(path, data),
            read_env=lambda path: load_env_file(path),
            save_env=save_env,
            env_refusal=lambda key: env_write_refusal(key, "set"),
            managed=lambda: managed_message("set up Linked Rooms") if is_managed() else None,
            key_managed=lambda key: bool(is_key_managed(key)),
            usable_key=lambda value: bool(has_usable_secret(value, min_length=16)),
            toolsets=lambda: list(configurable()),
            plugin_toolsets=lambda: set(plugin_keys()),
            builtin_toolsets=lambda: set(builtin_keys()),
            toolset_tools=lambda name: list(resolve_toolset(name)),
            allowed=lambda name: bool(allowed_for(name, PLATFORM)),
            label=lambda text: gui_label(text),
            policy=lambda profile, home: policy_mapping(
                target_profile=profile, config=effective_config(Path(home) / "config.yaml")),
            served=served,
            runtime_status=lambda root: read_status(Path(root) / STATUS_FILE),
            gateway_live=lambda record: bool(pid_live(record)) and not stale(record),
        )


# --- What the switch reads -----------------------------------------------------------


@dataclass(frozen=True)
class _Server:
    """This installation's API server: as config.yaml and the root .env set
    it, and as the gateway's own record says it runs."""

    enabled: bool          # the gateway starts it (or it runs)
    key_set: bool
    host: str
    port: int
    port_set: bool
    running: bool
    ran: bool              # the gateway's last record has it, running or not
    error: Optional[str]
    listener: Optional[str]
    key_usable: bool
    unseen: bool           # a key in the gateway's environment, which this can't read
    env_key: bool          # the key comes from the environment
    explicit: bool         # some block says enabled:
    flag: bool             # what the blocks' enabled: says

    @property
    def address(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"{host}:{self.port}"

    @property
    def url(self) -> str:
        # Hermes stamps its record's listener with a wildcard host as
        # 127.0.0.1 (shared_ingress.listener_base_url); one that wasn't gets
        # the same here.
        if self.listener and (urlsplit(self.listener).hostname or "") not in _WILDCARD_HOSTS:
            return self.listener.rstrip("/")
        host = DEFAULT_HOST if self.listener or self.host.strip() in _WILDCARD_HOSTS else self.host
        host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return f"http://{host}:{self.port}"


def _at(config: Any, path: tuple[str, ...]) -> Any:
    for name in path:
        if not isinstance(config, dict):
            return None
        config = config.get(name)
    return config


def _coerce_bool(value: Any, default: bool) -> bool:
    # gateway.config._coerce_bool
    if value is None:
        return default
    if isinstance(value, str):
        token = value.strip().lower()
        return True if token in _TRUE else False if token in _FALSE else default
    return bool(value)


def _truthy(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in _TRUE


def _port(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _resolved(value: Any, env: Mapping[str, str]) -> Any:
    # A ${VAR} in config.yaml is the gateway's to expand, from its environment.
    if isinstance(value, str):
        match = _REFERENCE.match(value.strip())
        if match:
            return env.get(match.group(1))
    return value


def _server(hermes: Hermes, root: Path, config: dict, env: Mapping[str, str]) -> _Server:
    top: dict = {}
    extra: dict = {}
    explicit = False
    for path in BLOCKS:
        block = _at(config, path)
        if not isinstance(block, dict):
            continue
        explicit = explicit or "enabled" in block
        if isinstance(block.get("extra"), dict):
            extra.update(block["extra"])
        top.update(block)
    # PlatformConfig.from_dict: the block's own keys, then extra over them.
    settings = {**{k: v for k, v in top.items() if k not in ("enabled", "extra")}, **extra}
    settings = {k: _resolved(v, env) for k, v in settings.items()}

    record = None
    try:
        record = hermes.runtime_status(root)
    except Exception as exc:  # noqa: BLE001 - an unreadable record reads as no gateway
        logger.warning("Linked Rooms setup couldn't read the gateway's status (%s)", type(exc).__name__)
    platforms = record.get("platforms") if isinstance(record, Mapping) else None
    platform = platforms.get(PLATFORM) if isinstance(platforms, Mapping) else None
    platform = platform if isinstance(platform, Mapping) else None
    live = False
    if platform is not None:
        try:
            live = bool(hermes.gateway_live(record))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Linked Rooms setup couldn't tell whether the gateway runs (%s)", type(exc).__name__)
    # Hermes keeps an earlier gateway's entry across a restart: only the one
    # the record's own gateway wrote counts (web_server_gateway's
    # _owned_profile_platforms).
    own = platform is not None and (platform.get("writer_pid"), platform.get("writer_start_time")) == (
        record.get("pid"), record.get("start_time"))
    running = live and own and platform.get("state") == "connected"
    listener = platform.get("listener_base") if running else None
    listener = listener if isinstance(listener, str) and listener.startswith("http") else None
    listener_port = None
    if listener:
        try:
            listener_port = urlsplit(listener).port
        except ValueError:  # a record this can't read: the API server is where config.yaml and the .env put it
            listener = None
    error = platform.get("error_message") if live and own and not running else None

    env_key = env.get(KEY_ENV)
    env_usable = hermes.usable_key(env_key)
    if env_usable:
        # gateway.config_env._api_server: the env's key, port and host win.
        settings["key"] = env_key
        if str(env.get(PORT_ENV) or "").strip():
            settings["port"] = _port(env[PORT_ENV], settings.get("port"))
        if str(env.get(HOST_ENV) or "").strip():
            settings["host"] = env[HOST_ENV]
    key_usable = hermes.usable_key(settings.get("key"))
    # A gateway that ran it had a key: one the root .env doesn't set comes
    # from its environment, which this can't read (a service file). One
    # still starting hasn't written its entry yet, and one whose startup
    # guard refused the key (missing or too weak) had none.
    starting = platform is not None and record.get("gateway_state") == "starting"
    refused = platform is not None and platform.get("error_code") == KEY_REFUSED
    unseen = (own or starting) and not refused and KEY_ENV not in env and not key_usable
    flag = _coerce_bool(top.get("enabled"), False)
    from_env = env_usable or unseen
    enabled = running or ((flag or (from_env and not explicit)) and (key_usable or unseen))

    host = settings["host"] if "host" in settings else env.get(HOST_ENV, DEFAULT_HOST)
    host = str(host if host is not None else DEFAULT_HOST)
    raw_port = settings.get("port")
    port_set = raw_port is not None or bool(str(env.get(PORT_ENV) or "").strip())
    port = _port(raw_port if raw_port is not None else (env.get(PORT_ENV) or DEFAULT_PORT), DEFAULT_PORT)
    port = listener_port or port
    return _Server(enabled=enabled, key_set=key_usable or unseen, host=host, port=port, port_set=port_set,
                   running=running, ran=platform is not None, error=_plain(error) if error else None,
                   listener=listener, key_usable=key_usable, unseen=unseen, env_key=from_env, explicit=explicit,
                   flag=flag)


def _plain(text: Any) -> Optional[str]:
    """Hermes' words for why the API server didn't start, without terminal
    escapes or control characters, which the app would show as they are."""
    plain = " ".join("".join(c if c.isprintable() else " " for c in _ESCAPE.sub("", str(text))).split())
    return plain or None


def _answers(host: str, port: int) -> bool:
    """Whether something already listens on ``host:port``."""
    try:
        with socket.create_connection((host, port), timeout=PORT_CHECK_TIMEOUT_S):
            return True
    except OSError:
        return False


def _never_used(hermes: Hermes, key: str) -> bool:
    """Whether every tool of a toolset is one rooms never allow (memory,
    running code, delegation, scheduled jobs). One Hermes can't list here (a
    plugin's, from the dashboard) stays a choice."""
    try:
        tools = {str(name) for name in hermes.toolset_tools(key)}
    except Exception:  # noqa: BLE001 - unknown, so it stays
        return False
    return bool(tools) and tools <= room_choices.NEVER


def _choices(hermes: Hermes) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for row in hermes.toolsets():
        key, label, description = (list(row) + ["", ""])[:3]
        key = str(key)
        if key in seen or key == OWN_TOOLSET or key == NO_MCP or key in SETTINGS_ONLY or not hermes.allowed(key):
            continue
        if _never_used(hermes, key):
            continue
        seen.add(key)
        rows.append({"id": key, "label": hermes.label(str(label)), "description": str(description)})
    return rows


def _checked_ceiling(ceiling: Any, choices: list[dict]) -> list[str]:
    if not isinstance(ceiling, list):
        raise SetupError("bad_ceiling", "The room ceiling is a list of toolset names")
    if len(ceiling) > MAX_CEILING:
        raise SetupError("bad_ceiling", f"The room ceiling names at most {MAX_CEILING} toolsets")
    ids = [choice["id"] for choice in choices]
    names: set[str] = set()
    for name in ceiling:
        if not isinstance(name, str):
            raise SetupError("bad_ceiling", "The room ceiling is a list of toolset names")
        name = name.strip()
        if name == OWN_TOOLSET:
            raise SetupError("bad_ceiling", "Conduit's own tools (conduit) never run in rooms, so they can't be in "
                                            "the room ceiling")
        if name not in ids:
            raise SetupError("bad_ceiling", f"{name[:64]!r} isn't a toolset rooms can use on this Hermes; the "
                                            f"choices are {', '.join(ids)}")
        names.add(name)
    return sorted(names)


def _strings(value: Any) -> Optional[list[str]]:
    return [str(item) for item in value] if isinstance(value, list) else None


@dataclass
class _Plan:
    root: Path
    profile_config: dict
    root_config: dict
    server: _Server
    ceiling: list[str]
    choices: list[dict]
    approvals: str
    profile_writes: dict      # dotted key -> value, in the profile's config.yaml
    root_writes: dict         # dotted key -> value, in the root's config.yaml
    new_key: bool
    profile_key: bool         # the profile has a key of its own for the API server
    new_profile_key: bool
    new_port: Optional[int]
    needs: list[str]
    blockers: list[dict]
    changed: list[str]


def _approval_mode(value: Any) -> str:
    # tools.approval_context._normalize_approval_mode
    if isinstance(value, bool):
        return "off" if value is False else "manual"
    if isinstance(value, str) and value.strip().lower() in ("manual", "smart", "off"):
        return value.strip().lower()
    return "manual"


def _plan(hermes: Hermes, home: Path, profile: str, ceiling: Any) -> _Plan:
    home = Path(home)
    root = room_identity.root_home(home)
    choices = _choices(hermes)
    asked = None if ceiling is None else _checked_ceiling(ceiling, choices)
    root_config = hermes.read_config(root / "config.yaml")
    profile_config = root_config if home == root else hermes.read_config(home / "config.yaml")
    env = dict(hermes.read_env(root / ".env"))
    server = _server(hermes, root, root_config, env)
    blockers: list[dict] = []

    why = hermes.served(home) if home != root else None
    if why == "standalone":
        blockers.append({"code": "standalone_profile", "message": (
            f"Profile {profile} runs its own gateway (gateway.standalone: true in its config.yaml), which the host's "
            "API server doesn't serve, so rooms can't reach its agent. Take gateway.standalone out of its config.yaml "
            "to set it up.")})
    elif why == "parked":
        blockers.append({"code": "parked_profile", "message": (
            f"The host gateway isn't serving profile {profile}: `hermes -p {profile} gateway stop` parked it. Start it "
            f"again with `hermes -p {profile} gateway start`, then set it up.")})
    # Hermes reads it once, in the gateway process (tools.approval's
    # _YOLO_MODE_FROZEN), from its environment and the root .env; the host
    # gateway never loads a named profile's .env into its environment. One
    # set only in a service's environment, which this can't read, Hermes
    # refuses rooms for itself (hosted_room_peer: approvals off).
    if _truthy(os.environ.get(YOLO_ENV)) or _truthy(env.get(YOLO_ENV)):
        blockers.append({"code": "yolo", "message": (
            f"{YOLO_ENV} is on for this Hermes, which turns approvals off for every turn, and rooms refuse that. Take "
            f"{YOLO_ENV} out of {root / '.env'} and the environment Hermes runs in, then restart the gateway.")})
    managed = hermes.managed()
    if managed:
        blockers.append({"code": "managed", "message": str(managed)})

    # The API server: a key where none is, a disable turned on, a port.
    root_writes: dict = {}
    for path in BLOCKS:
        block = _at(root_config, path)
        if isinstance(block, dict) and "enabled" in block and not _coerce_bool(block["enabled"], False):
            root_writes[".".join(path + ("enabled",))] = True
    new_key = not server.key_usable and not server.unseen
    # The API server serves a named profile (/p/<name>) only with a key of
    # its own, from the profile's .env, never the root's.
    profile_key = server.key_set if home == root else hermes.usable_key(
        dict(hermes.read_env(home / ".env")).get(KEY_ENV))
    new_profile_key = home != root and not profile_key
    yaml_only = server.key_usable and not server.env_key
    if yaml_only and not server.explicit:
        # Hermes starts it from config.yaml only when it says enabled: true.
        root_writes[f"platforms.{PLATFORM}.enabled"] = True
    new_port = None
    if not server.running and not server.ran and not server.port_set:
        check = DEFAULT_HOST if server.host.strip() in _WILDCARD_HOSTS else server.host
        if _answers(check, DEFAULT_PORT):
            new_port = next((port for port in range(DEFAULT_PORT + 1, LAST_PORT + 1) if not _answers(check, port)),
                            None)
            if new_port is None:
                blockers.append({"code": "no_port", "message": (
                    f"Something already answers on every port from {DEFAULT_PORT} to {LAST_PORT}, so the API server "
                    f"has none to use. Set {PORT_ENV} in {root / '.env'} to a free one, then set it up.")})

    # The ceiling, toolsets added later, approvals: the profile's own.
    saved = _strings(_at(profile_config, ("platform_toolsets", PLATFORM)))
    ours = saved is not None and NO_MCP in saved
    default = [name for name in DEFAULT_CEILING if name in {choice["id"] for choice in choices}]
    target = asked if asked is not None else (sorted(name for name in saved if name != NO_MCP) if ours else default)
    profile_writes: dict = {}
    if saved != sorted({*target, NO_MCP}):
        profile_writes[f"platform_toolsets.{PLATFORM}"] = sorted({*target, NO_MCP})
    known = _strings(_at(profile_config, ("known_plugin_toolsets", PLATFORM)))
    want = sorted({*(known or []), *map(str, hermes.plugin_toolsets()), OWN_TOOLSET})
    if known != want:
        profile_writes[f"known_plugin_toolsets.{PLATFORM}"] = want
    known_builtin = _strings(_at(profile_config, ("known_builtin_toolsets", PLATFORM)))
    want_builtin = sorted({*(known_builtin or []), *map(str, hermes.builtin_toolsets())})
    if known_builtin != want_builtin:
        profile_writes[f"known_builtin_toolsets.{PLATFORM}"] = want_builtin
    approvals_section = profile_config.get("approvals") if isinstance(profile_config, dict) else None
    approvals = _approval_mode(approvals_section.get("mode", "manual") if isinstance(approvals_section, dict)
                               else "manual")
    if approvals == "off":
        profile_writes["approvals.mode"] = "manual"

    # An administrator's pins (managed_scope's config and .env, in
    # HERMES_MANAGED_DIR or /etc/hermes) and a managed install are the
    # installation's, whichever profile asks.
    for dotted in [*profile_writes, *root_writes]:
        if hermes.key_managed(dotted):
            blockers.append({"code": "managed", "message": (
                f"An administrator pinned {dotted} on this Hermes, so the setup can't change it. Ask them to change "
                "it, or set Linked Rooms up where it isn't pinned.")})
    for key, writing in ((PORT_ENV, new_port is not None), (KEY_ENV, new_key or new_profile_key)):
        refusal = hermes.env_refusal(key) if writing else None
        if refusal:
            blockers.append({"code": "managed", "message": str(refusal)})

    changed = [name for name, done in (
        ("api_server_key", new_key), ("profile_key", new_profile_key), ("api_server_port", new_port is not None),
        ("api_server_enabled", bool(root_writes)),
        ("ceiling", f"platform_toolsets.{PLATFORM}" in profile_writes),
        ("plugin_tools", f"known_plugin_toolsets.{PLATFORM}" in profile_writes
         or f"known_builtin_toolsets.{PLATFORM}" in profile_writes),
        ("approvals", "approvals.mode" in profile_writes)) if done]
    # What a POST would do, in this order: turn the API server on (a key, an
    # explicit disable, a port), save the ceiling, keep toolsets added later
    # out, make approvals manual, restart the gateway to start the API server.
    needs = []
    if new_key or new_profile_key or new_port is not None or root_writes:
        needs.append("api_server")
    needs.extend(need for need in ("ceiling", "plugin_tools", "approvals") if need in changed)
    if not server.running:
        needs.append("restart")
    return _Plan(root=root, profile_config=profile_config, root_config=root_config, server=server, ceiling=target,
                 choices=choices, approvals=approvals, profile_writes=profile_writes, root_writes=root_writes,
                 new_key=new_key, profile_key=profile_key, new_profile_key=new_profile_key, new_port=new_port,
                 needs=needs, blockers=blockers, changed=changed)


def _policy(hermes: Hermes, home: Path, profile: str) -> Optional[dict]:
    try:
        return dict(hermes.policy(profile, Path(home)))
    except Exception as exc:  # noqa: BLE001 - the state still answers, without the policy
        logger.warning("Linked Rooms setup couldn't read profile %s's room policy (%s)", profile, type(exc).__name__)
        return None


def _view(hermes: Hermes, home: Path, profile: str, plan: _Plan) -> dict:
    server = plan.server
    # Where it runs, or where it will once set up (a taken port moves it).
    address = server.address if plan.new_port is None else replace(server, port=plan.new_port).address
    return {
        "ready": not plan.needs and not plan.blockers,
        "needs": list(plan.needs),
        "blockers": list(plan.blockers),
        "profile": profile,
        "api_server": {"enabled": server.enabled, "key_set": server.key_set,
                       "profile_key_set": plan.profile_key, "address": address, "running": server.running,
                       "error": server.error},
        "changes": list(plan.changed),
        "ceiling": list(plan.ceiling),
        "choices": plan.choices,
        "approvals": plan.approvals,
        "policy": _policy(hermes, home, profile),
    }


# --- The switch ------------------------------------------------------------------------


def state(hermes: Hermes, home: Path, profile: str, ceiling: Any = None) -> dict:
    """What setting ``profile`` up (in ``home``) would do, and what blocks
    it: ``needs`` and ``blockers``; ``ready`` when there's neither. A
    ``ceiling`` that isn't one is refused (``bad_ceiling``)."""
    return _view(hermes, home, profile, _plan(hermes, home, profile, ceiling))


def apply(hermes: Hermes, home: Path, profile: str, ceiling: Any = None) -> dict:
    """Sets ``profile`` up: the state after the writes, with what they
    ``changed``. A blocker is refused (409) before anything is written; one
    setup at a time per installation."""
    home = Path(home)
    root = room_identity.root_home(home)
    directory = root / room_identity.DIRECTORY
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _thread_lock, open(directory / LOCK_NAME, "a+b") as handle, room_identity._exclusive(handle):
        plan = _plan(hermes, home, profile, ceiling)
        if plan.blockers:
            first = plan.blockers[0]
            raise SetupError(first["code"], first["message"], 409)
        # The ceiling before the API server: a server that starts never runs
        # rooms on the toolsets it had before.
        same_file = home == root
        profile_config = copy.deepcopy(plan.profile_config)
        _set_all(profile_config, {**plan.profile_writes, **(plan.root_writes if same_file else {})})
        root_config = None
        if plan.root_writes and not same_file:
            root_config = copy.deepcopy(plan.root_config)
            _set_all(root_config, plan.root_writes)
        if plan.profile_writes or (same_file and plan.root_writes):
            hermes.write_config(home / "config.yaml", profile_config)
        if root_config is not None:
            hermes.write_config(root / "config.yaml", root_config)
        # The port before the key, which is what turns the API server on.
        if plan.new_port is not None:
            _save_env(hermes, root, PORT_ENV, str(plan.new_port))
        if plan.new_key:
            _save_env(hermes, root, KEY_ENV, secrets.token_hex(32))
        if plan.new_profile_key:
            _save_env(hermes, home, KEY_ENV, secrets.token_hex(32))
        # With the ceiling asked for: a saved one kept may name a toolset
        # this Hermes no longer has, which isn't this setup's to refuse.
        after = _plan(hermes, home, profile, ceiling)
    if plan.changed:
        logger.info("Linked Rooms setup for profile %s changed: %s", profile, ", ".join(plan.changed))
    return {**_view(hermes, home, profile, after), "changed": list(plan.changed)}


def _save_env(hermes: Hermes, home: Path, key: str, value: str) -> None:
    """Writes the value to the home's .env, and only there: Hermes also puts
    it in this process's environment, where a profile's key would read as
    the API server's own and pass to a gateway this process restarts. Under
    the dashboard's scope for a named profile, Hermes puts it only in that
    scope; in the CLI nothing else runs while it's there."""
    before = os.environ.get(key)
    try:
        hermes.save_env(home, key, value)
    finally:
        if before is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = before


def _set_all(config: dict, writes: Mapping[str, Any]) -> None:
    for dotted, value in writes.items():
        names = dotted.split(".")
        section = config
        for index, name in enumerate(names[:-1]):
            child = section.get(name)
            if child is None:
                child = section[name] = {}
            elif not isinstance(child, dict):
                raise SetupError("bad_config", f"config.yaml's {'.'.join(names[:index + 1])} isn't a section, so the "
                                               "setup can't save under it; fix it by hand first", 409)
            section = child
        section[names[-1]] = copy.deepcopy(value)


def local_api_url(hermes: Hermes, home: Path, profile: str) -> str:
    """This Hermes' API server, as this host reaches it: its own record's
    address while it runs, else where config.yaml and the root .env put it,
    with ``/p/<profile>`` for a named profile."""
    root = room_identity.root_home(Path(home))
    server = _server(hermes, root, hermes.read_config(root / "config.yaml"), dict(hermes.read_env(root / ".env")))
    return server.url if profile == "default" else f"{server.url}/p/{quote(profile, safe='')}"


def profile_name(home: Path) -> str:
    """Hermes' name for the profile whose home ``home`` is."""
    import hermes_constants

    name_of = getattr(hermes_constants, "profile_name_for_home", None)
    if callable(name_of):
        try:
            name = name_of(home)
        except Exception:  # noqa: BLE001 - the plain rule below
            name = None
        if name:
            return str(name)
    home = Path(home)
    return "default" if room_identity.root_home(home) == home else home.name


def restart_command() -> Optional[list[str]]:
    """``hermes -p default gateway restart --detach`` as Hermes runs its own
    commands, or None on a Hermes without ``--detach`` (9e01b44), whose
    restart of a gateway no service manager runs would hold the terminal."""
    try:
        from hermes_cli._launchers import runtime_command
        from hermes_cli.config import get_project_root
        from hermes_cli.gateway_runtime_start import start_detached_replacement  # noqa: F401 - what --detach runs
    except ImportError:
        return None
    try:
        return list(runtime_command(get_project_root(), list(RESTART_ARGS)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Linked Rooms setup couldn't make the gateway restart command (%s)", type(exc).__name__)
        return None


def takes_interrupt(spawn: Callable[..., Any]) -> bool:
    """Whether Hermes' ``_spawn_gateway_restart`` takes ``interrupt``
    (930e5ed and later)."""
    try:
        inspect.signature(spawn).bind("default", interrupt=True)
    except (TypeError, ValueError):
        return False
    return True
