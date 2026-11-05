# Bind the Gateway before publishing

Release offers and Story update requests belong to the Gateway that receives them. They are not copied across the fleet. A successful request to another Gateway can therefore leave the intended Story Mac unchanged.

Every ship/publish helper must bind `KH_GATEWAY_URL` to its already verified Gateway origin before its first request. There is no implicit local Gateway. The tracked helper is `scripts/gw.py`; the orchestration helper at `_orchestra/audit-20261011/vision/gateway/gw.py` is its byte-identical copy. An absent or malformed binding raises before reading credentials or making a request. Explicitly selecting the local Gateway remains valid.

The helper sends `X-Gateway-Origin` containing that origin. Gateway requires it on these admin mutations:

- `POST /admin/api/release/publish` (Studio Hub release publication).
- `POST /admin/api/story/release/publish`.
- `POST /admin/api/story/instances/{instance}/update`.

The shared browser console already sends its own `Origin` on these POSTs; it can use that same-origin binding. Authentication is still required independently. `Referer` alone is not accepted as publication intent. Read-only API routes do not require this header.

Missing or duplicate origin declarations return 422 `GATEWAY_ORIGIN_REQUIRED`. Malformed origins return 422 `GATEWAY_ORIGIN_INVALID`. A different scheme, host or effective port returns 409 `GATEWAY_ORIGIN_MISMATCH`, before any publication, rollout or queued update. Origin values must contain no credentials, non-root path, query or fragment. Default HTTP(S) ports compare equivalently. Refusals never echo the supplied origin or credentials.

For Story, verify the intended Gateway's signed offered version/hash and its Story instance check-in before requesting an update. A 200 update response only means that Gateway recorded the request. Delivery requires the Story Mac to check in to the same Gateway; `update_request_delivered`, the exact request identity and direct installed health are separate evidence.

After an ambiguous or wrong-origin request, preserve its intent and receipt. Reconcile that attempt before any further mutation. This guard neither redirects publication nor retries or cancels an existing request. Production's pause and quiet-point requirements remain unchanged.

This protocol is an unreleased source change after Gateway 0.2.234 until the normal coordinated release completes. Existing 233/234 servers accept the helper's additional header but do not enforce it. Native Fleet views remain retired; the active console is the shared web page.
