# Browser audio discovery (wire v1, schema 1)

Browser workers remain off by default. These fields add authenticated discovery to the existing wire; they do not enable audio generation.

`GET /api/workers/health` advertises `onlineSeconds`, `audioInventoryMaxAgeSeconds` (both the live `GATEWAY_EXTENSION_WORKER_ONLINE_SECONDS`, default 90 seconds), and `audioInventoryMaxBytes` (the live `GATEWAY_EXTENSION_WORKER_INVENTORY_MAX_BYTES`, default 1048576 bytes). Invalid configured values use those finite positive defaults. The byte limit covers the complete audio claim, including its envelope and telemetry.

An audio bearer token may attach this to its existing `POST /api/workers/claim` body:

```json
{
  "workerId": "audio-worker",
  "kind": "audio",
  "audioInventory": {
    "schema": 1,
    "providers": {
      "fish": {
        "source": "live",
        "complete": true,
        "fetchedAt": 1794288000000,
        "models": [{"id": "exact-model-id"}],
        "voices": [{"id": "exact-voice-id"}]
      }
    }
  }
}
```

`fetchedAt` is the actual observation's epoch milliseconds, a positive JavaScript safe integer, no later than Gateway receipt time and strictly younger than the advertised window. Preserve it when repeating cached observations. Only `fish` and `elevenlabs_web` are known providers. Each listed provider must have a complete live observation and nonempty model and voice ID lists. Missing `audioInventory` or an empty `providers` object clears the worker's prior discovery. Snapshots replace prior providers; they never union with them.

Discovery belongs to the authenticated worker ID, kind and current token issuance. It is held only in the existing in-memory broker. Remint, revoke and restart invalidate it. Health, progress and result requests do not renew its observation time. An audio token that authenticated before a revoke cannot assign work afterward. Malformed discovery returns 422 (`WORKER_INVENTORY_INVALID`) and an oversized body returns 413, both before any assignment; prior discovery is cleared. A wrong worker identity returns 403, and a non-audio token cannot attach audio discovery.

For pending synthetic broker jobs, the actual claiming worker must currently discover both exact requested IDs. Discovery expiry or an absent snapshot does not abandon a job already claimed or held. Result delivery and unknown-outcome holds retain their existing rules.

## Remaining admission dependency

Independent model and voice lists do not establish compatible pairs or required provider limits. Gateway therefore continues to refuse actual audio generation with `WORKER_INVENTORY_UNAVAILABLE`, before offering any job. Caller parameters such as `_worker_inventory` cannot authorize it. The copied Relay catalogue's limits are identified as Gateway policy, not observed provider limits. Admin status reports discovery freshness/counts and explicitly reports audio admission as unavailable. The modality table advertises video and image; browser TTS remains withheld until this dependency is resolved.

The Relay TTS discovery checkpoint reviewed for this contract is `eddeb1b83b0ee22b54f8f698513d62b2fe4f91ef`. Its IDs-only projection matches schema 1. Successful audio admission requires reviewed actual provider compatibility and required-limit evidence from the Gateway/Relay integration owners. This is an integration dependency, not a request for another owner approval.

`tests/test_extension_audio_inventory.py` uses an in-process HTTP fixture with synthetic offers. It proves discovery/auth/assignment fencing and refusal; it does not prove a provider handshake or generation.
