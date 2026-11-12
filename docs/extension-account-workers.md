# Browser account workers (schema 1)

Source contract for the existing Gateway `/api/workers/*` service. This is not an installed/live qualification. Browser/provider switches stay OFF. No token, sign-in session, observed quote or manager action grants paid spending by itself.

## Pairing and authentication

Gateway's own app uses existing `require_local_app` authentication to mint tokens. That means a loopback peer, plus the Gateway bearer key when Gateway binds beyond loopback. Relay must never extract this key from another app, a web view or Chrome. The Gateway key is not the manager credential.

- `POST /admin/api/extension-workers` with `{worker_id,kind}` still pairs image/video/audio workers. Each starts with dispatch disabled. Tokens remain hashed, scoped to one worker/kind, revocable and displayed once.
- `POST /admin/api/extension-workers/manager` with `{manager_id,worker_ids:[exact paired worker IDs]}` returns `{ok:true,manager_id,token}` once. The manager uses `Authorization: Bearer <token>` on the two manager routes below. Keep it in Relay's own app data file (mode 0600), bound to the saved Gateway origin; never in URLs, logs or rendered pages.
- **Automatic pairing** (switch on by default, Settings → Local apps): the gateway writes `~/.kh/gateway-endpoint.json`, mode 0600 and rewritten on every launch, holding exactly `{url, machineId, pairingToken, schema}` — never the Gateway API key. The pairing token can do exactly one thing: `POST /admin/api/extension-workers/pair` `{kind, worker_id}`, **loopback only**, returns a fresh worker token through the same mint path. It cannot read providers, change settings, publish, or act as admin. Turning the switch off removes the file and revokes the token. Manual entry stays the fallback for a Gateway on another Mac.
- A manager token cannot claim jobs, submit telemetry/results, mint/revoke tokens, read unrelated workers, or enable account spending. Remint/revoke invalidates the credential on its next request.
- `PUT /admin/api/extension-workers/accounts/enabled` with `{provider,account_id,enabled:boolean}` is app-only. A newly observed account starts disabled. This stored switch survives restart. It is separate from worker dispatch and the profile's own enabled/paused state.

## Manager read and control

`GET /api/workers/manager` returns:

```json
{
  "schema": 1,
  "receivedAt": 1794350000.0,
  "maxAgeSeconds": 90,
  "workers": [{
    "worker_id": "profile-a-video",
    "kind": "video",
    "has_live_token": true,
    "control": {
      "credentialGeneration": "opaque-credential-generation",
      "dispatchEnabled": false,
      "closeRequested": true,
      "revision": "opaque-revision",
      "requestedAt": 1794350000.0,
      "pacing": {"minSeconds": 5, "maxSeconds": 15}
    },
    "state": null,
    "receivedAt": null,
    "fresh": false,
    "current_job": null,
    "held_jobs": [],
    "durableBusy": true,
    "safeToClose": false,
    "accountBinding": null
  }],
  "accounts": []
}
```

`state` is null until an authenticated worker reports it. Otherwise it uses the normalized names below: `connection`, `sign_in`, `readiness`, `activity`, `attention`, `enabled`, `paused`, `source`, `observed_at`, `next_eligible_at`, `control_revision`, `pacing`. Times in responses are epoch seconds. Connection/sign-in/readiness are independent from activity. An open Chrome process does not establish any of them.

`POST /api/workers/manager/control` accepts `{worker_ids:[exact scoped IDs],dispatchEnabled:boolean,closeRequested?:boolean,pacing?:{minSeconds,maxSeconds}}` or a pacing-only `{worker_ids:[exact scoped IDs],pacing:{minSeconds,maxSeconds}}`, and returns the same current view. Pacing-only requests omit **both** dispatchEnabled and closeRequested: under the existing write lock, they preserve each selected worker's latest dispatch/close flags rather than replaying cached flags. They still advance the revision, requiring a fresh matching acknowledgment before execution/close; desired pacing is not actual application. A missing dispatch without valid pacing, or closeRequested without dispatchEnabled, refuses without a write. Pause/close uses `dispatchEnabled:false`; closeRequested true with dispatchEnabled true refuses. Both image and video worker IDs must be paused before closing their shared profile. Pacing is persisted desired configuration, default random inter-generation 5–15 seconds; explicit zero and custom nonnegative finite ranges are preserved. Worker-reported pacing/next_eligible_at is the actual readback. Desired pacing is not proof of application and never shortens a provider cooldown.

Workers receive `control` on authenticated health, telemetry and successful/quote-preview claim replies. A pause acknowledgment requires fresh telemetry explicitly containing `paused:true` and `controlRevision` exactly echoing the requested revision. Normalized state includes `paused_observed`, computed from the presence of the validated worker `paused` field; a caller-supplied marker is ignored. Omitted `paused` retains the conservative `paused:true` claim-blocking default but has `paused_observed:false` and can never acknowledge safe close. This applies to both account observations and `telemetry.state`. `safeToClose` additionally requires dispatch disabled, known activity of idle/cooling/resting/waiting, and no in-memory active/held job or durable active/unknown job. Generating, recovering and unknown activity cannot acknowledge a safe close. Missing, stale, revoked or restarted telemetry always gives false. A close request never kills a browser, cancels an unknown provider outcome, releases a hold or resumes work. `safeToClose` describes only that exact worker, not a Chrome process or a whole account. Relay owns the explicit profile-to-worker mapping and waits for both workers' authoritative values before closing their shared profile; a manager missing either worker's scope must keep close pending. Account equality is not proof of physical profile identity, and the same account may be open in multiple profiles. Failed/unavailable readback stays pending. Refresh never enables a worker/account.

Account rows are deduplicated by `(provider,account_id)` and limited to accounts bound to this manager's worker scope. They contain observed `balance` (or null), `reset_at` (or null), observed `plan`/`credit_buckets` (or null), source/observed_at/received_at/fresh, `gateway_enabled`, `reserved`, `available` (or null), `reservation_states`, `needs_reconciliation`, and `workers`. No token digest or unrelated binding is returned. Observed balance is never multiplied by worker count or replaced with a quote. No universal daily 50 or timezone is inferred.

Manager responses use normalized snake_case observation fields. A response balance is `{value,unit,source,observed_at,received_at,fresh}`; each response credit bucket uses `observed_at` and `reset_at`. The camelCase fields in the observation example below are worker **request** fields. `available` is the observed balance minus unreconciled reservations/debits in matching units; it remains negative when a later observed balance falls below those commitments. Never clamp it to zero or treat it as permission to spend. Unknown balance or incomparable units gives `available:null`; `reserved` is also null if existing commitments themselves have mixed units. Freshness and eligibility are checked separately at claim time. New claims refuse incomparable units, and a differently denominated balance cannot reconcile an old debit.

Before the first manager control, `control.revision` and `control.requestedAt` are null. A revoked or absent scoped worker remains visible with `has_live_token:false`, `kind:null`, unknown state and `safeToClose:false`; do not infer its old kind or readiness.

## Authenticated observations

`POST /api/workers/telemetry` accepts `{workerId,kind,account?,telemetry?:{state?,extensionVersion?,credits?}}`. The same account/state additions are accepted by claim and progress. Report only a provider-observed opaque stable account identity; a local Chrome profile ID alone does not prove which account signed in. Until identity is known, omit account and report state independently.

```json
{
  "workerId": "profile-a-video",
  "kind": "video",
  "account": {
    "schema": 1,
    "provider": "flow",
    "accountId": "opaque-provider-account",
    "source": "flow.account",
    "observedAt": "2026-11-11T01:00:00Z",
    "connection": "connected",
    "signIn": "signed_in",
    "readiness": "ready",
    "activity": "idle",
    "attention": false,
    "enabled": false,
    "paused": true,
    "controlRevision": "opaque-revision",
    "nextEligibleAt": null,
    "pacing": {"minSeconds": 5, "maxSeconds": 15},
    "balance": null,
    "resetAt": null,
    "plan": null,
    "creditBuckets": null
  }
}
```

`telemetry.state` accepts the same state/provenance/time/control/pacing fields without account identity or credit fields. Account state and separately reported state must agree when both are sent. Telemetry response is `{ok:true,receivedAt:<server epoch>,control:{...}}`.

Enums:

- connection: connected, disconnected, offline, error, unknown.
- signIn: signed_in, signed_out, unknown.
- readiness: ready, not_ready, unknown.
- activity: idle, generating, cooling, resting, waiting, recovering, unknown.

`balance` is null or `{value:<finite nonnegative safe number>,unit:<slug>,source:<slug>,observedAt:<zoned ISO>}`. Real zero stays zero; missing/invalid/stale is never zero. `creditBuckets`, when actually observed, is an array of those amounts plus distinct `id:<slug>` and optional `resetAt`; it is descriptive, never summed into an invented balance. Plans/buckets are never inferred from names, quotes or an owner's anecdotal allowance.

Observation timestamps must be timezone-qualified and not future. Gateway stamps receive time independently and publishes its live freshness limit (`accountMaxAgeSeconds`, currently the configured worker online interval). Re-sending an old observation does not refresh its timestamp. Unknown reset remains null; reset countdown is displayed only from fresh observed reset data. Amounts never become spend-eligible merely because a reset time elapsed. Complete claim/telemetry bodies use the existing configurable inventory byte limit, published as `claimMaxBytes`.

## Quote and atomic paid claim

A new executable claim requires a known canonical Gateway job, exact worker token generation/account binding, explicit dispatch/account/profile enablement, fresh connected/signed-in/ready/idle-or-waiting state, nonpaused profile, and a known actual `nextEligibleAt` that has arrived. Model/capability validation still happens before an offer. Audio remains withheld without its existing compatible-pair/provider-limit evidence.

Without a quote, a pending eligible worker receives:

```json
{"job":null,"quoteRequired":{"job":{"jobId":"job_example","kind":"video","prompt":"example"},"payloadSha256":"64 lowercase hex characters","executionAuthorized":false},"control":{}}
```

This is an unclaimed preview for read-only provider quote inspection, never execution. The hash is SHA256 of UTF-8 JSON of the **entire exact server-built job including jobId**, sorted object keys, compact separators, Unicode unescaped; Relay should echo the supplied hash rather than recompute it. No new provider generation is authorized by a preview.

Next claim supplies `quote:{provider,accountId,payloadSha256,value,unit,source,observedAt}` alongside an actual `account` observation in that same request. Its state must acknowledge the current `controlRevision` before execution; cached telemetry alone cannot authorize the quoted claim. It must describe the observed cost for that exact payload/account and use the balance's units. Unknown/stale quote or balance refuses. Gateway atomically checks shared-account available credits, records a reservation and checkpoints the existing canonical job's upstream ID before returning:

```json
{"job":{"jobId":"job_example"},"accountBinding":{"provider":"flow","accountId":"opaque-provider-account"},"reservation":{"payloadSha256":"64 lowercase hex characters","value":1,"unit":"credits"},"control":{}}
```

These JSON examples abbreviate the existing model/control/reference payload; the actual payload stays intact. Relay must verify its actual signed-in account still matches accountBinding before action. Binding changes cannot move reservations, credits, active jobs or results to another account. A repeated claim while busy refuses rather than authorizing a second execution; report progress/result instead. A response lost after reservation remains an unknown outcome, held without re-offer/replay.

Legacy `credits`/multipart `creditsAfter` remain display-only telemetry: they lack account binding and never fund reservations. Known outcomes settle the recorded account reservation once; cost remains conservatively debited until a newer observed account balance reconciles it. An ambiguous failure, silent worker or restart holds its reservation; even a fresh balance alone does not prove that unknown job's outcome. Late valid results can still settle their exact original job/account through the existing ownership/integrity checks. Administratively closing a canonical job's recovery hold is not provider-outcome or charge evidence and does not release its credit hold. If actual outcome evidence cannot be recovered, that account remains blocked from new paid claims; neither a fresh balance nor a profile replacement clears it. No 402 is introduced.

## Native manager counterpart: version, identity and close barrier

The existing worker wire remains unchanged except for the explicitly documented account-schema additions. There is no new version header or URL version. Require `schema:1` on manager readback; execution workers advertise `accountSchema:1` on authenticated worker health. Credential-free Gateway `/health` is only service liveness. A manager credential does not authorize `/api/workers/health`, and service liveness does not prove pairing, readiness or admission. A 404 manager route is unavailable/off/unsupported, never an empty successful worker list.

Pair only through the Gateway app's deliberate app-only mint action above. A remote Gateway WKWebView, its browser session and Relay's credential-free native health request gain no app-only mint authority. Relay receives only the intentionally issued manager token and keeps it in its normal native secret storage for the saved Gateway origin. No cookie, web-view credential, raw Gateway key or token digest belongs in this exchange.

`control.credentialGeneration` is an opaque non-secret identifier independent of the token and its digest. It is minted with the credential, persisted in the existing token file and returned by worker health/control and manager readback. It survives broker/process restart and changes on re-pair; revoke/absent worker returns null. Older token records without this value return null and cannot produce `safeToClose:true`; they require a deliberate app-approved re-pair before generation-bound close, never an automatic re-pair that clears a charge hold. Existing valid credentials remain usable for their other scoped requests.

Relay owns the stable local `profile_id` and its explicit mapping to saved Gateway origin, each `worker_id`, kind and expected credential generation. Gateway does not accept or invent a physical `profile_id`. Bind actual provider account identity separately from that mapping; image/video on the same `(provider,account_id)` share one credit authority, while one account may occur in several physical profiles. A missing or changed generation, worker kind, origin or profile mapping keeps close pending until the binding is reconciled. Re-pair does not inherit old telemetry, revision acknowledgment or dispatch enablement. It cannot move old jobs/holds to the replacement credential/account.

For a close, send one control request naming **every** bound worker and `dispatchEnabled:false,closeRequested:true`. The token-file write and executable claim both hold `admin_session._lock`: whichever wins is authoritative. If pause wins, the later claim returns no executable job and creates no reservation; if a claim wins first, pause prevents further claims and readback retains that active job. It never kills or cancels it. Wait for its actual finish/drain, or retain a clear unknown/held state.

The returned `control.revision` acknowledges persisted desired state, not native/browser application. Each selected live worker gets the same new revision. Relay must keep close pending until a fresh authenticated observation echoes that revision with `paused:true`, the generation/profile mapping still matches, and **every** mapped worker has `safeToClose:true`. Check `has_live_token`, `current_job`, `held_jobs` and `durableBusy`; an empty in-memory list after restart is insufficient. Durable unknown work may be represented by `durableBusy:true` even when `held_jobs` is empty. A later authorized control changes the revision and invalidates the old acknowledgment; Relay serializes its own resume/open/close lifecycle and never closes from a cached earlier acknowledgment.

Bulk control is one persisted write for the selected live credentials. The response includes the manager's entire scope, including unselected workers. A revoked/absent selected worker is not changed: its `has_live_token:false`, null generation/revision and `safeToClose:false` expose partial application. HTTP 200 is not all-workers-drained success. An out-of-scope ID rejects the whole request before any write. Missing scope, missing rows, stale observations, failed reads, invalid JSON/schema, network failure and non-200 responses all leave the local close pending. No Resting label or OS process-liveness shortcut overrides that barrier.

Top-level manager `receivedAt` is the response snapshot time. Per-worker `receivedAt` is the authenticated observation receive time; `state.observed_at` is source observation time. `fresh` checks both against live `maxAgeSeconds`. Credit balance/bucket/reset provenance and freshness follow the separate account fields above. Account, worker and provider admission switches/eligibility stay separate; there is no invented aggregate `admitted` field, daily-free allowance or successful audio-admission promise.

## Exact manager errors and revocation

Manager requests use `Authorization: Bearer <intentionally paired manager token>`; control sends an object JSON body (`Content-Type: application/json`). There are no credential URL/query parameters. Standard manager refusals are `{ok:false,code,detail}`:

| Route/condition | Status | Code/result |
| --- | --- | --- |
| Either manager route, worker switch off | 404 | `{detail:"Not Found"}` |
| Either manager route, missing/wrong/revoked manager token | 401 | `MANAGER_TOKEN_REFUSED` |
| Control empty/invalid/out-of-scope worker list | 403 | `MANAGER_SCOPE_REFUSED`; no write |
| Control invalid JSON/object/booleans/pacing or close+dispatch together | 422 | `MANAGER_CONTROL_INVALID`; no write |
| Control body exceeds live configured byte limit | 413 | `MANAGER_CONTROL_INVALID`; no write |
| Manager token on execution-worker routes | 401 | `WORKER_TOKEN_REFUSED` |
| Presented worker/manager token on app-only/admin routes, including loopback, any Authorization scheme or whitespace | 403 | `WORKER_SCOPE_REFUSED` |
| Non-loopback client attempts manager mint or credential revoke | 403 | Existing `require_local_app` refusal |
| App-authorized mint with invalid manager/scope | 422 | `MANAGER_INVALID` |
| Internal read/write failure | 5xx | No successful acknowledgment; body may be framework text |

The Gateway app revokes a manager with `DELETE /admin/api/extension-workers/{manager_id}` using existing app-only authority. It returns `{ok:true,revoked:<count>}`; execution workers use the same route with their own worker ID. Manager/execution identities are distinct. Re-minting a manager ID revokes the old token and replaces its exact scope. No TTL is inferred: tokens are persisted and remain valid until revoked/replaced. Authentication reads the token file per request; control revalidates the manager under the write lock. Revocation stops subsequent requests, does not cancel active work and does not clear credit commitments.

On restart, persisted tokens, desired pause/OFF, pacing and control revisions remain. In-memory state disappears, so close is unsafe until new authenticated, current-generation observations arrive. Only the real queue owner recovers reservations; exact canonical job plus current upstream identity can recover a known outcome as a debit. Unknown/closed/cancelled/superseded attempts remain held. Late-result integrity, ownership, first-result rules and the 256 MiB artifact cap remain in force.
