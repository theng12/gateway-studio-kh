# Browser account workers (schema 1)

Source contract for the existing Gateway `/api/workers/*` service. This is not an installed/live qualification. Browser/provider switches stay OFF. No token, sign-in session, observed quote or manager action grants paid spending by itself.

## Pairing and authentication

Gateway's own app uses existing `require_local_app` authentication to mint tokens. That means a loopback peer, plus the Gateway bearer key when Gateway binds beyond loopback. Relay must never extract this key from another app, a web view or Chrome. The Gateway key is not the manager credential.

- `POST /admin/api/extension-workers` with `{worker_id,kind}` still pairs image/video/audio workers. Each starts with dispatch disabled. Tokens remain hashed, scoped to one worker/kind, revocable and displayed once.
- `POST /admin/api/extension-workers/manager` with `{manager_id,worker_ids:[exact paired worker IDs]}` returns `{ok:true,manager_id,token}` once. The manager uses `Authorization: Bearer <token>` on the two manager routes below. Keep it in Relay's own Keychain, bound to the saved Gateway origin; never in URLs, logs or rendered pages.
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

`POST /api/workers/manager/control` accepts `{worker_ids:[exact scoped IDs],dispatchEnabled:boolean,closeRequested?:boolean,pacing?:{minSeconds,maxSeconds}}` and returns the same current view. Pause/close uses `dispatchEnabled:false`; closeRequested true with dispatchEnabled true refuses. Both image and video worker IDs must be paused before closing their shared profile. Pacing is persisted desired configuration, default random inter-generation 5–15 seconds; explicit zero and custom nonnegative finite ranges are preserved. Worker-reported pacing/next_eligible_at is the actual readback. Desired pacing is not proof of application and never shortens a provider cooldown.

Workers receive `control` on authenticated health, telemetry and successful/quote-preview claim replies. A pause acknowledgment requires fresh telemetry with `paused:true` and `controlRevision` exactly echoing the requested revision. `safeToClose` additionally requires dispatch disabled, known activity of idle/cooling/resting/waiting, and no in-memory active/held job or durable active/unknown job. Generating, recovering and unknown activity cannot acknowledge a safe close. Missing, stale, revoked or restarted telemetry always gives false. A close request never kills a browser, cancels an unknown provider outcome, releases a hold or resumes work. `safeToClose` describes only that exact worker, not a Chrome process or a whole account. Relay owns the explicit profile-to-worker mapping and waits for both workers' authoritative values before closing their shared profile; a manager missing either worker's scope must keep close pending. Account equality is not proof of physical profile identity, and the same account may be open in multiple profiles. Failed/unavailable readback stays pending. Refresh never enables a worker/account.

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
