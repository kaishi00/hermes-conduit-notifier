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

## Review labels

A PR labeled `waiting on contributor` is waiting on its author. Push or comment
on the PR when you've answered it, and a workflow swaps the label for
`waiting on Eric`, so the maintainer knows it's his turn again. Putting
`waiting on contributor` back on takes `waiting on Eric` off. A reply inside a
review thread doesn't count, so also comment on the PR itself or push.
