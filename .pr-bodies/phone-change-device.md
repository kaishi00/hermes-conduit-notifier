## Problem

Found while testing Conduit with a family member's agent on an iPhone and an iPad (both sharing Calendar & phone):

1. **Changes land on the wrong device.** An unnamed `add_reminder` goes to the device that *uploaded* last (`last_seen`). The phone uploads only when something changed, so the device the user is holding often isn't that one. A reminder asked from the iPhone was queued to the iPad. A grocery list asked from the iPad went, as 12 reminders, to another iPhone on the same profile, which had briefly been the only device with an upload while the iPad was re-registering. The agent said "approve it on your phone" either way.
2. **Pending changes vanish silently on a new key.** `register_key` with a new key for a known device (reinstall, restore, sharing off and on) deleted that device's pending changes. Nothing told the agent, so it kept telling the user to approve a card that would never come.

## Change

- `GET /phone/status` (Conduit calls it at the start of every sync pass, including `messageSent`) records `devices.last_contact` for a known device. It never creates the store and never writes for an unknown id.
- Unnamed changes and reads go to the device last in touch: `MAX(last_contact, last_seen)`.
- For a change, if the device last in touch got a new key less than 10 minutes ago and hasn't uploaded yet, the tool answers `device_not_ready` instead of standing another device in. A named device is still reachable.
- A new key marks that device's pending changes `lost` instead of deleting them. The note says the phone never saw it, or, if it had already fetched it, to check before asking again. A late outcome for a lost change still lands. The `pending` note lists `lost`.
- Schema 3 adds the column, migrating v1 and v2. A 0.51.1 plugin refuses a schema-3 store, so a rollback needs the store upgraded again. Bump or renumber as you like; I set 0.51.2.

## Before

On `main` (2f08161) with only the new tests added:

```
E       ValueError: not enough values to unpack (expected 1, got 0)      # a re-keyed phone's change: no outcome at all
E       assert 'iPad' == "Eric's iPhone"                                  # the iPhone just checked in, the change went to the iPad
E       Failed: DID NOT RAISE PhoneError                                  # a re-registering iPad's change went to the iPhone
3 failed
```

## After

`tests/test_phone.py` and `tests/test_clarify_loop.py`: 211 passed. The full suite has the same 13 failures as `main` in my environment, all in unrelated live-voice tests, so they're environmental.

New tests:
- a re-key marks changes lost;
- a change the phone had fetched keeps a "check first" note and its late outcome;
- routing follows check-ins;
- no stand-in while a device sets up;
- the setting-up window ends;
- status never recreates a wiped store;
- the v2→v3 migration.

Fork CI: https://github.com/malaiwah/hermes-conduit-notifier/pull/1
