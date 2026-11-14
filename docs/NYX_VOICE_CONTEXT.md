**Optional voice startup context**

`GET /api/plugins/conduit_push/memory/context` now recognizes an explicitly
implemented, optional `voice_prompt_block()` method on an external memory
provider. It returns text only; the provider selects voice-appropriate facts,
not instructions for tools the live model cannot call. Generic
`system_prompt_block()` and empty-query recall are never fallbacks.

The capability is invoked through the existing per-profile provider entry,
serialization lock and deadline. Voice facts are packed first, then complete
builtin note lines fill the remaining 8,000-character budget. Each input block is inspected up to 64,000 characters, discarding any
unfinished line at that boundary. Legacy providers
retain their previous snapshot/recall behavior. No turn synchronization or
memory-writing method is called. GET responses remain `Cache-Control: no-store`.

Nyx's matching implementation returns at most 4,000 characters of current trusted
owner profile facts and due follow-ups, respecting correction, forgetting and
the follow-up switch. Both the provider and notifier changes are needed; the
Nyx 7.24.0 release implements this capability (7.23.1 does not).

**Connected feature plan**

Feature Map entry: **What a GPT-Live call is told**
(`hermes-conduit/docs/FEATURE_MAP.md`). Related entries are **A live call's
personality and your instructions** and **Ending a call and what gets saved**.

| Sibling | Status and reason |
| --- | --- |
| Notifier memory-context route | Changed: optional capability, same response fields, existing scoped execution and bounds |
| Nyx provider | Changed in its own repository: compact voice facts, no generic tool prompt |
| iPhone GPT-Live and calls from Hermes/CarPlay | No code change needed: already fetch `context` and place it in the briefing when memory is enabled |
| Watch | No code change needed for this phase: receives the phone-built briefing through the existing path |
| Subscription and API-key session routes | No protocol changes: existing seed/instruction append paths carry the startup briefing |
| Gemini/Qwen/Grok/Local consumers | Same endpoint/schema; capable providers gain facts; other providers retain existing behavior |
| Settings and consent | Unchanged: per-profile memory opt-in, applied from the next call |
| README and integration documentation | Updated to describe the capability and limitations |
| Per-turn recall/write lifecycle | Open gap, deliberately not claimed by this startup-only change |

The corresponding Feature Map update is supplied as a reviewable patch outside
this repository, alongside the cross-repository validation artifacts. No upstream
app source or published App Store build was changed by this implementation.

**Verification and limits**

Unit tests cover capable and legacy providers, invalid output, safe errors,
deadlines, hung-profile isolation, character bounds, ordering and no writes.
Nyx also supplies an optional real-provider/Conduit-HTTP integration check. Its
host configuration is isolated and synthetic; it does not validate microphone,
WebRTC, user consent UI, or spoken-model behavior on a real device.

The subscription transport is not the public Realtime API. Do not add
`response.create` or assume a per-turn hold mechanism here. Automatic dynamic
recall needs client-side stable transcript events, stale-result guards and a
bounded scheduler; live writes also need durable deduplication with call-end
archiving. An already-started call is not updated by reading this endpoint later.

Client version caveat: selecting Nyx under Memory and delegation configures the
Hermes provider. It is not proof that the client's voice-memory sharing is on.
The reviewed upstream GPT-Live settings expose Use Hermes memory only when the
mode is enabled; installed app versions may have different navigation or lack
that control. Server publication alone cannot add a missing native app setting.
