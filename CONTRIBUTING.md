# Contributing to hermes-conduit-notifier

This repo is the Conduit plugin for Hermes (`conduit_push`), the push relay
(`relay/`) and the rooms mailbox (`mailbox/`). The iPhone app is
[hermes-conduit](https://github.com/kaishi00/hermes-conduit).

## Connected features

Most features here have siblings in the app: a tool here and the voice modes
that call it there, a route here and the screen that reads it there. They are
listed in hermes-conduit's
[docs/FEATURE_MAP.md](https://github.com/kaishi00/hermes-conduit/blob/main/docs/FEATURE_MAP.md).

- Before changing a feature, find its entry there and put every sibling on
  your plan, the app's included, before writing code.
- Follow the entry's rulings. Changing a ruling is the maintainer's call.
- In the PR, fill in Siblings: each one done, not needed (and why), or a gap
  left open. When the change alters an entry, update it in hermes-conduit.
- Text that describes a behavior is one of its siblings: tool descriptions and
  results the models read, README.md, and the app's briefings and Settings text.

## Plugin version

A version bump changes five files together: `plugin.yaml`, `events.py`,
`dashboard/manifest.json`, `tests/test_clarify_loop.py`, and a new entry on
top of `CHANGELOG.md` (Conduit shows it as the update notice). Parallel PRs
often pick the same next number, so take the next free version on main right
before merging. CI fails a PR whose version main already has.

## Tests

CI (`.github/workflows/tests.yml`) runs all three suites on every PR and every
push to main. To run them locally:

    pip install -r tests/requirements.txt
    python -m pytest --rootdir=tests tests
    (cd relay && npm test)
    (cd mailbox && npm test)
