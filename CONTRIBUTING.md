# Contributing to hermes-conduit-notifier

This repo is the Conduit plugin for Hermes (`conduit_push`), the push relay
(`relay/`) and the rooms mailbox (`mailbox/`). The iPhone app is
[hermes-conduit](https://github.com/kaishi00/hermes-conduit).

## Connected features

Most features here have siblings in the app: a tool here and the voice modes
that call it there, a route here and the screen that reads it there. They are
listed in hermes-conduit's
[docs/FEATURE_MAP.md](https://github.com/kaishi00/hermes-conduit/blob/main/docs/FEATURE_MAP.md).
Nobody tends the map separately: each PR that changes a feature keeps its
entry current.

- Before changing a feature, find its entry there and put every sibling on
  your plan, the app's included, before writing code.
- Follow the entry's rulings. Changing a ruling is the maintainer's call;
  record their decision in the entry.
- In the PR, fill in Siblings: name the entry, or the hermes-conduit PR that
  adds or updates it, then for each sibling say done, not needed (and why),
  or a gap left open. "No mapped feature" is only for a change with no
  siblings in the app. A refactor that changes no behavior and no
  user-facing or model-facing text (tool descriptions and results included)
  has none and needs no new entry; if it moves a file the map lists, update
  that path in hermes-conduit.
- When the change alters an entry, update it in hermes-conduit. A feature
  with siblings in the app and no entry gets one there, and a fix that brings
  a sibling in line with an earlier change it missed adds that sibling to the
  entry.
- Text that describes a behavior is one of its siblings: tool descriptions and
  results the models read, README.md, and the app's briefings and Settings text.

## Dashboard routes

Hermes loads `dashboard/plugin_api.py` by path (`dashboard/manifest.json`'s
`api`). It only loads `dashboard/api/`, one module per feature, and lists
them: the routes and the code behind them are in the modules
(notifier#443). `tests/test_api_layout.py` holds these rules.

- A route goes in its feature's module. A new feature gets a module of its
  own: add it to `MODULES` in `dashboard/api/__init__.py` and to
  plugin_api.py's list, in the same place.
- A module takes another's names through the module: `from . import core` at
  the top, then `core.TokenError`, never `from .core import TokenError`. So a
  test that replaces `api.core._calls_home` replaces it for every module.
- Every module is created before the first one runs, then they run in
  `MODULES` order. A module's top-level statements (a base class, a default, a
  constant made from another module's) may use only the modules above it; its
  functions may use any of them.
- Every route is on core's router (`router = core.router`), behind
  `core._note_conduit_request`; no module makes a router of its own. No two
  routes answer the same request, so the order never decides one.
- What three or more features use goes in `core`.
- A module stays under 150 KB, small enough for an agent or a reviewer to
  read whole. Past that, give part of it a module of its own.
- Tests and probes reach a name through its module:
  `api.watch_jobs.WATCH_JOB_TOOLS`, `monkeypatch.setattr(api.core, "_calls_home", ...)`,
  a probe's `hasattr(module.watch_audio, "_WatchAudioBridge")`.

A branch that still has the single file merges main from just before the
split (f68d017), which doesn't have the script yet, so it takes it from main:
`git checkout origin/main -- scripts/split_plugin_api.py`. Then it runs
`python3 scripts/split_plugin_api.py write`, commits, and merges main: only
its own changes conflict.

## Plugin version

A version bump changes `plugin.yaml` and `dashboard/manifest.json` together
and adds a new entry on top of `CHANGELOG.md` (Conduit shows it as the update
notice). `events.py` reads its version from `plugin.yaml`, and the tests check
that the manifest and the changelog agree with it. Parallel PRs
often pick the same next number, so take the next free version on main right
before merging. CI fails a PR whose version main already has.

## Tests

CI (`.github/workflows/tests.yml`) runs all three suites on every PR and every
push to main. To run them locally:

    pip install -r tests/requirements.txt
    python -m pytest --rootdir=tests tests
    (cd relay && npm test)
    (cd mailbox && npm test)

## Upstream contract

`upstream/` runs the plugin against a real Hermes dashboard, so a Hermes change
that breaks us shows up from the daily canary rather than from users
(`.github/workflows/upstream-contract.yml`; it runs on PRs that touch
`dashboard/` or `upstream/`, and every morning on the newest canary and
release, opening one `upstream-break` issue per failure).

- `upstream/contract.yml` lists the Hermes modules and names the plugin imports
  and the routes it serves. That part is generated: after adding a Hermes
  import or a route, run `python3 upstream/contract.py write` and commit it
  (the plugin suite fails until you do). Say what a new integration point
  feeds in `notes`.
- A test of upstream behavior checks the behavior we need, never the broken
  one. When upstream breaks it and we work around it, list it under
  `known_failures` with its signature: it reports as a known failure until
  upstream fixes it, then fails so the workaround gets another look.
- Tests check where a request landed (which profile's settings it used), not
  only that it was answered.

To run them locally (Linux or macOS, about a minute):

    upstream/install-hermes.sh v0.22.5 /tmp/hermes
    HERMES_BIN=/tmp/hermes/.venv/bin/hermes /tmp/hermes/.venv/bin/python -m pytest --rootdir=upstream upstream

## Review labels

A PR labeled `waiting on contributor` is waiting on its author. Push or comment
on the PR when you've answered it, and a workflow swaps the label for
`waiting on Eric`, so the maintainer knows it's his turn again. Putting
`waiting on contributor` back on takes `waiting on Eric` off. A reply inside a
review thread doesn't count, so also comment on the PR itself or push.
