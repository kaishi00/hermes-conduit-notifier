# Phone scoped consent — protocol v1

**DRAFT SOURCE CANDIDATE. iOS compilation, Swift tests, rendered/accessibility UI
and physical paired-phone validation have not run. They block readiness.**

Companion source: [hermes-conduit](https://github.com/kaishi00/hermes-conduit),
`PhoneConsent.swift`, `PhoneConsentSection.swift`, and `PhoneConsentTests.swift`.
Link the two actual draft PRs at publication; no PR number is assumed here.

## Trust boundary

The existing `/pair`, notification encryption and generic `/boards/taps` routes
are not consent authorities. Knowing an installation/gateway ID, a notification
key, a service token or the anonymous dashboard bootstrap token cannot enroll a
consent device or approve a task. No model-facing tool issues phone receipts.

Enrollment is a **new explicit owner authorization** of a separate Ed25519 public
key for the profile's current pairing, not a claim that old E2E encryption proved
human consent. The phone first checks that this pairing belongs to its local
notification installation. The owner deliberately confirms enrollment in the Work
panel, using a revalidated interactive owner session (cookie or provider-verified
native bearer). The phone creates a fresh signing key, saves it with
`WhenUnlockedThisDeviceOnly` accessibility scoped to dashboard/profile, and proves
possession by signing a server enrollment challenge. Private signing material
never crosses the dashboard, reaches the push relay or enters a chat message.
A trusted owner can deliberately replace an enrollment; public IDs alone cannot.

This trusts the interactive owner session and uncompromised app/host. It is not
hardware key attestation, proof against a compromised owner session/device, an OS
sandbox, or a semantic command allowlist. Task intent and native command
approvals remain separate. No command-approval settings or APIs change.

## Routes and signed bytes

Capability: `phone-scoped-consent-v1`. All routes are on the existing plugin
router and profile-scoping adapter; phone callers use `conduit_profile` where
`dashboard-profiles` is available, as the Work panel does. Every route revalidates
`_grant_actor` through unchanged native owner/provider/profile checks. Writes
require one exact same-origin Origin and reject cross-site Fetch Metadata; the
native OAuth client supplies the same Origin for this route family only. All
responses are `Cache-Control: no-store`. Unknown capabilities/auth fail closed.

- `GET /phone-consent`: current pairing, opaque enrollment ID/public key, owner
  and profile binding, and server-backed grants involving this local profile.
  Each grant includes `snapshot_text`, the exact ASCII canonical JSON whose
  SHA-256 is `digest`; the phone checks it and decodes a typed full snapshot.
- `POST /phone-consent/challenge`: either
  `{action: "enroll", public_key, installation_id, gateway_id}` or
  `{action: "grant_approve" | "grant_decline" | "grant_revoke", device, grant, digest}`.
  Unknown/extra fields and wrong types are refused. This never admits work.
- `POST /phone-consent/receipt`: **only** `{challenge, signature}`. A valid owner
  session and valid enrolled-device signature are both required. A grant action
  can only consume its stored grant/digest/action; no supplied card or task is
  authoritative.
- `POST /phone-consent/revoke-device`: `{device}` through the owner session.
  This deliberately works without the lost phone's key, to permit owner recovery;
  it cannot enroll a replacement.

Challenge output is `{challenge, message}`. Sign the **exact UTF-8 bytes of the
returned message string**, without reserializing JSON. Verify the parsed context
against the displayed visit before signing. Signatures and public keys on the
wire are canonical unpadded base64url. Protocol is `conduit-phone-consent/1`.
The message contains a random 128-bit nonce, 60-second expiry, installation
identity, owner, exact profile, current pairing fingerprint, device enrollment,
public key, explicit action and (for grant actions) grant ID and snapshot digest.
The pairing fingerprint includes the local relay credential and IDs but exposes
only a SHA-256 digest, never the credential. Enrollment also binds the previous
enrollment so a delayed candidate cannot roll a newer enrollment back.

`consent_devices` and `consent_challenges` live in the existing installation grant
registry. Enrollment/replacement/revocation audit retains owner, public key,
pairing generation, time and revocation source. A write transaction verifies and
consumes a valid receipt before calling the reviewed one-use grant CAS. Invalid
signatures cannot consume someone else's valid challenge. Concurrent receipts,
multiple challenges for one grant, stale challenges and replay cannot admit a
second task. A valid receipt is never retried automatically after timeout or a
lost acknowledgement; backend admission remains consumed on uncertainty.

## Lifetime and stopping

Changing/removing the pairing or receiving a relay rejection retires its device;
restoring an old pairing file cannot undo observed revocation. Native pairing
writers and dashboard re-pairing notify this adapter outside the pairing lock.
Owner revocation and replacement persist device/grant revocation before native
interrupts, using the existing durable recipient-session binding even when worker
registration is missing. Native interrupt failure or in-flight creation is
reported as **unconfirmed**, never inferred as stopped from an empty row scan.
Worker admission callbacks and per-tool/turn checks independently revalidate the
phone enrollment/pairing and existing grant origin/participants. No lock spans
native RPC execution. Stop, pause, policy changes and terminal grants retain
the existing fail-closed behavior; no recursion, inherited grants or resume.

## Phone surface and siblings

The dedicated section in Bot Work shows task, scope and the **entire** immutable
snapshot, including sender/recipient, native origin session, turn/tool-call IDs,
identities and digest, as noneditable untruncated text. Its buttons are explicit
Approve this task once, Decline, Revoke and stop, plus owner enrollment/revocation.
It does not send a follow-up chat message or reuse native command approval cards.
The existing dashboard consent card and native work/command approvals are intact.

The section keeps no cached approval authority. Disconnect, backgrounding,
dashboard/profile/chat switches and failed reads clear it; unknown capability,
a different phone key/pairing or pending writes offer no approval. Its captured
bridge cannot silently switch servers. Reconnect rereads current state; it never
replays a write. Watch, CarPlay, voice, linked rooms and other installations have
no new controls. These are deliberately unsupported, not inherited consent paths.
Feature-map siblings are **Bot workstreams** and **Hermes is waiting on you**;
generic composed cards are unchanged.

## Evidence and remaining validation

Run `tests/test_phone_consent.py` with the plugin test interpreter, together with
the existing grants, work, registration, dashboard and native approval suites.
Run `tests/native_phone_consent_auth.py` with native Hermes' existing interpreter
and source on PYTHONPATH, in a disposable HOME/HERMES_HOME/TMPDIR. It uses actual
password/cookie issuance, provider bearer verification and unchanged native owner
middleware; only session RPC execution and hook registration use fixtures.
It is **not** an OAuth login UI or physical-phone gesture test.

Both repositories contain byte-identical public deterministic wire vectors:
`tests/fixtures/phone-consent-v1.json` and the app's
`ConduitTests/Fixtures/phone-consent-v1.json`. Python executes signature,
canonical-message and snapshot-digest checks. Swift source tests check those same
bytes, parsing, binding, offline/unknown capability, switches, reconnect and no
retry. Linux structural/localization/test-plan checks do not execute Swift.

Before readiness, the macOS lane must compile the app and tests, execute the new
BotModeTests, then render long/Unicode snapshots at accessibility sizes and verify
VoiceOver reads every field and identifies each action. On a disposable paired
phone, validate owner enrollment, wrong pairing/key, approve/deny, stop/revoke,
re-pair, offline/background/reconnect, dashboard/profile/chat switch during a
challenge, lost acknowledgement, no replay, and unchanged native approvals and
dashboard consent. No build/sign/install/account/live change is authorized by
these instructions. Obtain separate authorization for that validation lane.

Publication is by the coordinator after independent source/security review:
linked **draft** fork PRs, no merge/release/activation. App feature-map CI must
reference the actual PR numbers and requires the notifier paths to exist on main;
paired-source checks can use `--notifier-root` before that integration gate.
