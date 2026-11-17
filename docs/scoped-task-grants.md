# Approve once: scoped tasks between local bots

## Design

A scoped ask is a new explicit confirmation, not evidence recovered from a conversation or a shared proposal status. Ordinary asks, replies, reports and `work_propose(answer="approved")` remain data-only. No names or natural-language approval heuristics issue grants.

The plugin extends `work_ask` with an explicit scope. It stores a pending, immutable snapshot of the installation, sending profile and Bot Chat, originating work (when named), recipient profile and Bot Chat, exact task and scope. A random request identity distinguishes identical asks. The existing dashboard Conduit card shows the whole snapshot with Approve once and Decline; its action uses the existing workstream action endpoint with the snapshot identity. Unknown, absent or unsupported authenticated request provenance fails closed. A dashboard owner session is accepted only through Hermes' authenticated request boundary; automation/token-provider/native transport identities are not implicitly human consent. Cross-origin browser writes are refused.

Only this explicit authenticated action records the approving actor and consumes the grant. Backend admission reads the stored snapshot, checks current profile/session identity and both sides' live stop/pause/policy state, and starts one recipient workstream for that exact task and scope. Client parameters cannot replace the snapshot. A second tap or replay cannot admit another task. Crash/uncertain admission remains consumed, never automatically replayed. Revocation and stop invalidate the grant before stopping any admitted work; resume does not resurrect it.

The scoped workstream's brief identifies this newly approved exact task and scope without quoting another bot's report as the user's words. A grant never authorizes commands: existing native tool approval policy and request-specific approval APIs are unchanged. Further bot asks, work starts and delegate agents cannot inherit this grant. Scope is explicit task intent, not a new operating-system sandbox or command allowlist.

## Smallest implementation surface

- `work_tool.py`: ask schema/caller, pending ask handling, admission and scoped brief, lifecycle and tool boundary.
- A focused grant module and durable SQLite records: immutable snapshots, authenticated issuer audit, atomic one-use consumption and revocation.
- `dashboard/api/workstreams.py`: existing authenticated card/action boundary; distinct scoped actions so old approve/proposal flows cannot mint grants.
- `dashboard/dist/index.js`: extend the existing dashboard card with full pending preview and explicit approval/revocation controls; no new UI framework or dependency.
- Focused tests using the real plugin routes/store/work handler and existing Hermes session-API stand-ins, plus existing work/native approval regressions.
- Upstream integration contract and user documentation as required by the contribution rules.

## Alternatives and compatibility

Reading existing proposal status is rejected: machine and human answers share it. Rewriting global data-only prompts is rejected: it turns unrelated reports into instructions. Core changes are unnecessary: the owner authentication boundary is revalidated unchanged. A companion source change adds typed, signed phone consent; existing clients continue to use ordinary asks and their current cards unchanged.

The companion feature-map entries are **Bot workstreams** and **Hermes is waiting on you**. The dedicated typed phone card is not a generic conduit_show tap. See [phone scoped consent](phone-scoped-consent.md) for enrollment, receipts, sibling status and the mandatory macOS/iPhone validation gate.

## Verification contract

Exercise authenticated human event -> durable grant -> exact delegated-task admission, unauthenticated/automation refusal, wrong origin/actor/profile/task/scope, immutable-preview tampering, replay, machine claims and results, stop/pause/revocation, recursion and unchanged native command approvals. Use disposable homes only. No live settings, installation, relay, native app, or core changes.

## Implemented protocol

`work_ask({"bot": "recipient-profile", "text": "Exact task", "scope": "Exact limits", "for": "W1"})`
requests consent; `for` is optional. It requires the native pre-tool hook's
session provenance and the current sender Bot Chat. Native turn and tool-call
IDs are required and bound to the exact arguments, consumed even on refusal;
tool arguments cannot claim these. Unsupported or opaque bridges fail closed.
The request accepts only bot, text, scope and optional for. Text and scope are
bounded to the existing ask limit; task whitespace is preserved internally and
leading/trailing task whitespace is refused rather than silently changed.

The two ask rows stay in `needs_you`. Neither a wake nor actionable recipient
instructions are sent. The digest labels scoped asks as explicit owner-consent/backend-only;
`work_reply` cannot settle them. Ordinary asks are unchanged.

`GET /api/plugins/conduit_push/workstreams/grants` returns the complete immutable
snapshots, IDs, SHA-256 digests, terminal state and actor audit. The Sessions
page's existing Conduit slot renders every snapshot field as escaped text before
offering Approve once, Decline and Revoke and stop. The existing action route
accepts `grant_approve`, `grant_decline` and `grant_revoke`, with **only**
`{"digest": "the displayed digest"}`. A grant ID or digest never authorizes a
model-facing tool. Proposal approval remains a separate event.

Authentication is checked against core on the actual Request. Both grant listing
and actions require a revalidated dashboard session from a registered interactive
provider (`peek_session`), such as native password login and its session cookie.
`_session_mutation_context` and `host_owner_subject`
must agree on the owner and selected profile. Native HTTP principals, service
auth, local identities, server-internal sessions, session-token providers and
unknown providers are refused. The ungated standalone dashboard's session token
is published in anonymous HTML and is **not** interactive authentication: it
cannot list grants or issue consent, even with matching Origin/Fetch Metadata.
Use an interactive owner login; an ungated token-only dashboard cannot use this
feature. Origin headers are CSRF protection, not proof of human identity.
Unsupported core versions fail closed. Mutations require exactly one
Origin matching the request scheme/authority and reject other-page Fetch
Metadata. Browser GET previews may omit Origin; they still require owner auth.
Host-rewriting reverse proxies must preserve that origin relationship.

`work_grants.py` uses the existing Store transaction pattern with a separate
`scoped-task-grants.db` beside the installation's work registry. Snapshot fields
have no update API. An installation identity, profile-store identities, policy
epochs, the exact paired ask rows, Bot Chats and optional origin work identity
are rechecked before admission and by worker tool hooks. A stopped, interrupted,
closed or Leave alone origin, a policy change (including off then on), missing
profile/chat, altered task or invalid digest ends authority. Local installation
IDs do not depend on profile display names or relay pairing.

A conditional SQLite update commits `pending -> consumed` with the approving
actor **before** `_start` calls the existing broker. Only one contender can win.
Busy, failed, uncertain and crashed admissions remain consumed. No native RPC
holds the grant-store write lock: revocation can win during session creation.
The broker's created callback rechecks consent and live participants before
submitting the prompt; worker hooks and the existing late-stop path cover a
stop racing submission. A separate admitted-session binding in the installation
grant registry records the recipient profile, native stored session ID, work ref
and work identity before prompt submission. Missing, replaced or unreadable
recipient work registration does not turn that session into an ordinary chat:
all its tools fail closed. Revoke uses the independent native session binding,
not an empty worker-row scan. It is terminal before interrupting; an unavailable
native session, failed interrupt or still-in-flight creation is reported as
unconfirmed (409), not successful interruption. Restoring a work DB cannot
restore revoked consent; restart recovery never replays a scoped admission.

The worker receives a dedicated brief containing the exact snapshot and grant
provenance. It receives neither another bot's quoted report as user consent nor
global prompt replacements. Grant starts pass no yolo override and never answer
a native command approval. Existing command approval requests still require
their own request-specific event. Work starts, asks, sends, replies, proposals,
teammate messages and native delegate agents are blocked from a grant worker,
including bridged calls; execute_code and opaque/nested bridges are refused.
Existing worker memory restrictions continue to apply. Further delegated turns,
resume, recovery and outbox resubmission are refused. The admitted turn's result
settles the paired asks and returns to the sender Bot Chat as data only.

## Current limits and sibling status

This is task-intent consent, not an operating-system sandbox or a semantic
command allowlist. In-flight native operations still depend on Hermes' interrupt
semantics. The first implementation supports only the in-process dashboard
session API with inspectable create/submit/interrupt/close methods and registered
worker hooks in both profiles. Gateway-authority and isolated-host transports
are refused until they expose a concrete hook/stop capability contract. Unloading
either profile's hooks revokes its grants and attempts to interrupt admitted
workers. A stopped, paused or restart-interrupted admission requires a fresh
scoped ask and fresh approval; no approved card is automatically replayed.
Never-admitted cards surviving a crash still require explicit human review.

Feature-map **Bot workstreams** siblings: work_ask/schema, pending asks, scoped
worker brief/restrictions, paired results, dashboard work routes and Sessions card,
README and protocol documentation are implemented here. The companion change
adds a separate typed phone consent section, signing-key enrollment and tests;
its feature map tracks every sibling. Ordinary app work cards, briefs and native
command approval paths retain their existing behavior. Phone compilation,
rendering and physical validation are unexecuted draft gates, not source-test passes.

## Reproduction

Use a prepared test environment (`tests/requirements.txt`). Set `TEST_PYTHON` to
its Python, and `RUN_ROOT` to a disposable directory outside the repository (not
the operating system temporary directory):

```sh
mkdir -p "$RUN_ROOT/home" "$RUN_ROOT/hermes" "$RUN_ROOT/tmp"
export HOME="$RUN_ROOT/home" HERMES_HOME="$RUN_ROOT/hermes" TMPDIR="$RUN_ROOT/tmp"
"$TEST_PYTHON" -m pytest --rootdir=tests tests/test_work_grants.py tests/test_work_grant_boundaries.py tests/test_work_grant_registration.py tests/test_dashboard_scoped_tasks.py tests/test_work.py tests/test_work_coord.py -q
"$TEST_PYTHON" upstream/contract.py check
"$TEST_PYTHON" -m pytest --rootdir=tests tests/test_api_layout.py tests/test_upstream_contract.py tests/test_link_status.py tests/test_changelog.py -q
"$TEST_PYTHON" -m pytest --rootdir=tests tests -q -n 8 --dist worksteal --ignore=tests/test_pulse_personas.py
(cd relay && npm test)
(cd mailbox && npm test)
```

The topical tests exercise real FastAPI plugin routes, durable stores, work
handlers and the broker with FakeHermes. Their core request-auth functions are
explicit stand-ins. Separately run the native auth regression with an existing
Hermes installation's Python and source on PYTHONPATH, and a fresh disposable
HOME/HERMES_HOME/TMPDIR:

```sh
PYTHONPATH="$HERMES_CORE" "$HERMES_PYTHON" tests/native_scoped_grant_auth.py
```

It exercises native anonymous HTML token issuance -> preview/approve denial,
then actual password login/cookie -> exact one-use admission/revoke, with native
auth middleware and owner functions unchanged. Only session RPC execution and
hook registration use fixtures. Neither test claims a real-browser gesture,
live deployment, real model or phone run. No live installation or settings change.
