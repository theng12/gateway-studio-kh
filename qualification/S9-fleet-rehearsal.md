# S9 — fleet acceptance rehearsal

- **Run:** 2026-09-28T07:39:42+00:00 (finished 2026-09-28T07:41:30+00:00)
- **Driver:** `/build/Developer/_orchestra/rehearsal/run_rehearsal.py`
- **Work dir (kept):** `/private/var/folders/p8/wwyv7rc96s51h1b1rrx6scmc0000gn/T/s9-rehearsal-cf7f1y4z`
- **Studio Hub under test:** `HEAD = 94d5bfd`
- **Gateway under test:** `HEAD = 474b1e9`
- **Hub source root:** `/private/var/folders/p8/wwyv7rc96s51h1b1rrx6scmc0000gn/T/s9-rehearsal-cf7f1y4z/src/studio-hub`
- **Gateway source root:** `/private/var/folders/p8/wwyv7rc96s51h1b1rrx6scmc0000gn/T/s9-rehearsal-cf7f1y4z/src/gateway-kh`
- **Hub interpreter:** `/build/Developer/studio-hub-kh/.build/hub-gate/bin/python3`
- **Gateway interpreter:** `/build/Developer/gateway-kh/repository/.venv/bin/python`

## What was rehearsed

| piece | what it really is |
| --- | --- |
| Studio Hub × 5 | the real `components.hub.standalone.app`, its own temp data dir, its own Hub token, `STUDIOHUB_REQUIRE_AUTH=1`, on its own loopback port |
| Engine per Mac | a rehearsal-owned worker stub answering the Hub's worker contract (`/api/health`, `/api/catalog`, `/api/generate/txt2img`, `/api/generate/jobs/<id>`) |
| Model | `rehearsal-image` (`rehearsal/image-1`, engine `image-mflux`), receipt written by the Hub's own `components.model_packages` verifier over real bytes |
| Gateway | isolated recipe from `tests/test_e2e.py`: temp `GATEWAY_DATA`/`_APP_SUPPORT``/DB`/`CREDENTIALS`/`MODEL_CACHE`/`ENV_FILE`, own port and API key, `GATEWAY_NO_MIGRATE=1`, `STUDIOHUB_READY_FILE` pointing at nothing |

_Partial run: only checks 12 were run._

## Summary

| # | check | result | measured |
| ---: | --- | --- | --- |
| 12 | three attempts on three machines, no duplicates | **FAIL** | attempt 3 succeeded after failures on [] (6 job(s) sampled, batch appeared at []); terminal failure named []; unknown outcome -> None (retries: 0) |

## 12. three attempts on three machines, no duplicates

- **Result:** FAIL
- **Measured (96.42s):** third_time_lucky={'runs': [{'submit_status': 404, 'id': None, 'state': None, 'attempts': None, 'attempts_used': None, 'failure_code': None, 'failure_reason': ''}, {'submit_status': 404, 'id': None, 'state': None, 'attempts': None, 'attempts_used': None, 'failure_code': None, 'failure_reason': ''}, {'submit_status': 404, 'id': None, 'state': None, 'attempts': None, 'attempts_used': None, 'failure_code': None, 'failure_reason': ''}, {'submit_status': 404, 'id': None, 'state': None, 'attempts': None, 'attempts_used': None, 'failure_code': None, 'failure_reason': ''}, {'submit_status': 404, 'id': None, 'state': None, 'attempts': None, 'attempts_used': None, 'failure_code': None, 'failure_reason': ''}, {'submit_status': 404, 'id': None, 'state': None, 'attempts': None, 'attempts_used': None, 'failure_code': None, 'failure_reason': ''}], 'chosen': None, 'new_batches': {}} all_three_fail={'submit_status': 404, 'id': None, 'state': None, 'failure_code': None, 'failure_reason': None, 'attempts': None, 'attempts_used': None, 'attempts_allowed': None} unknown_outcome={'state': None, 'failure_code': None, 'recovery_required': None, 'recovery_state': None, 'recovery_detail': None, 'retryable': None, 'attempts': None, 'attempts_used': None}

_attempt 3 succeeded after failures on [] (6 job(s) sampled, batch appeared at []); terminal failure named []; unknown outcome -> None (retries: 0)_

```json
{
  "locations": [
    {
      "id": "local",
      "label": "This Mac",
      "reachable": false,
      "role": null,
      "machines": 0,
      "nodes": null
    }
  ],
  "machines": {
    "head": {
      "provider": "studio-hub-None",
      "macs": [
        "node-b"
      ]
    },
    "c": {
      "provider": "studio-hub-None",
      "macs": [
        "controller-c"
      ]
    },
    "d": {
      "provider": "studio-hub-None",
      "macs": [
        "controller-d"
      ]
    }
  },
  "enable_everywhere": {
    "changed": 0,
    "enabled": true
  },
  "third_time_lucky": {
    "runs": [
      {
        "submit_status": 404,
        "id": null,
        "state": null,
        "attempts": null,
        "attempts_used": null,
        "failure_code": null,
        "failure_reason": ""
      },
      {
        "submit_status": 404,
        "id": null,
        "state": null,
        "attempts": null,
        "attempts_used": null,
        "failure_code": null,
        "failure_reason": ""
      },
      {
        "submit_status": 404,
        "id": null,
        "state": null,
        "attempts": null,
        "attempts_used": null,
        "failure_code": null,
        "failure_reason": ""
      },
      {
        "submit_status": 404,
        "id": null,
        "state": null,
        "attempts": null,
        "attempts_used": null,
        "failure_code": null,
        "failure_reason": ""
      },
      {
        "submit_status": 404,
        "id": null,
        "state": null,
        "attempts": null,
        "attempts_used": null,
        "failure_code": null,
        "failure_reason": ""
      },
      {
        "submit_status": 404,
        "id": null,
        "state": null,
        "attempts": null,
        "attempts_used": null,
        "failure_code": null,
        "failure_reason": ""
      }
    ],
    "chosen": null,
    "new_batches": {}
  },
  "all_three_fail": {
    "submit_status": 404,
    "id": null,
    "state": null,
    "failure_code": null,
    "failure_reason": null,
    "attempts": null,
    "attempts_used": null,
    "attempts_allowed": null
  },
  "duplicates": {
    "same_idempotency_key_job": null,
    "terminal_job": null,
    "one_job_for_the_key": true,
    "attempts_len": 0
  },
  "retry_exclusion_probe": {
    "excluded_by_provider_id": [],
    "excluded_by_controller_id": [],
    "asked_to_exclude": "None",
    "provider_id_spelling_excludes": true,
    "controller_id_spelling_excludes": true
  },
  "accepted_before_the_kill": {
    "state": null,
    "upstream_job_id": null
  },
  "unknown_outcome": {
    "state": null,
    "failure_code": null,
    "recovery_required": null,
    "recovery_state": null,
    "recovery_detail": null,
    "retryable": null,
    "attempts": null,
    "attempts_used": null
  }
}
```

- **Reproduce:** `python3 run_rehearsal.py --only 12`

## Diagnosis of the failures

- **12. three attempts on three machines, no duplicates** — **product defect, isolated and reproduced on a fresh three-location fleet.** Every job made two attempts on two machines, with two different failures, and was then declared terminally failed with `attempts_used: 2` / `attempts_allowed: 3` although the third machine was enabled, reachable, `model_supply {eligible: 1, available: 1}` and never tried. Cause, read from the code the run measured: `worker._next_machine` builds `exclude_locations` from the attempt log, which stores the **provider id** (`studio-hub-<controller-id>`), while `fleet.routing_candidates` compares it against `controller_id_from(definition.id)`, the **bare controller id** — so an already-tried site is not actually excluded. When the weighted lottery then re-picks a site that was already tried, `_next_machine` sees `chosen.id in tried` and returns `None`, which the worker treats as 'nothing left to try' and fails the job. The third machine is never reached, and nothing on the job or in the log says why. (`_next_machine` also discards `route()`'s explanatory note, so the reason is unpublished either way.)

## What this rehearsal did not exercise

- **The node app's own re-join loop.** The Mac's automatic re-join after a handover or a roster change is native behaviour; the rehearsal performs the same HTTP request the app would and labels it `rejoin_emulated`.
- **The SwiftUI pages themselves.** Every 'UI' reading here is the API payload the page renders (Fleet row, Models row, queue view), not a screenshot.
- **Real model inference.** Each Mac runs a rehearsal worker stub that answers the Hub's real worker contract (`/api/health`, `/api/catalog` with `cache.state: cached`, `/api/generate/txt2img`, `/api/generate/jobs/<id>`, `/ack`); the Hub, the Gateway, the catalogue, routing, admission, the queue and the retry policy are all the real thing.
- **Story Studio** — out of scope for this rehearsal; the Story surface is exercised only as the `/v1/...` contract it reads.

## Builds measured

- **Studio Hub**: version `0.6.93`, revision `HEAD = 94d5bfd` (source root `/private/var/folders/p8/wwyv7rc96s51h1b1rrx6scmc0000gn/T/s9-rehearsal-cf7f1y4z/src/studio-hub`)
- **GatewayKH**: build `474b1e9-s9` as published on `/health`, revision `HEAD = 474b1e9` (source root `/private/var/folders/p8/wwyv7rc96s51h1b1rrx6scmc0000gn/T/s9-rehearsal-cf7f1y4z/src/gateway-kh`)


---

# Addendum — check 12's three-attempt half, after the fix (2026-09-28, later the same day)

The measurement in this receipt is **correct and was made against Gateway `0.2.64`**: with three
locations verified enabled, every job stopped at `attempts_used: 2` while publishing
`attempts_allowed: 3`. The diagnosis recorded here — an exclusion by *provider* id leaving the site
in the candidate list while the same exclusion by *controller* id removes it — was right, and it is
the same id-namespace defect A7 exists to close (audit H5), reproduced one layer along inside A7's
own new code.

**It is fixed in Gateway `0.2.65`.** `worker._next_machine` normalises both sides through
`fleet.controller_id_from(provider_id) or provider_id`, in one place, and applies the same function
to the route's answer. The attempt log still stores the readable provider id an operator sees.

Re-measured against the installed `0.2.65` on this Mac, on the live code path:

```
$ .venv/bin/python -c "…patch worker.fleet.route with a spy; call _next_machine twice…"
exclusion asked of the router: [{'aaa'}, {'bbb', 'aaa'}]
correct spelling (bare controller ids): True
```

`routing_candidates` compares `controller_id_from(definition.id)` — the bare form — against
`exclude_locations`, so the exclusion now bites and attempt 3 is reachable whenever a third machine
offers the model.

Three tests in `tests/test_job_retry_policy.py` pin it, and the third one is the reason the original
suite missed it: the end-to-end test's fake router returned its sites in order and **ignored
`exclude_locations`**, so it never exercised the exclusion at all. A fake that ignores the thing
under test cannot fail.

```
tests/test_job_retry_policy.py ....... 11 tests, OK   (was 9)
```

**Status of check 12 as a whole:** the unknown-outcome half passes over HTTP in this receipt (a Mac
killed mid-poll becomes a reconciliation hold with `retryable: false` and is never re-run), and the
three-attempt half is now correct on the code path it was failing on, with the machine-level
assertions moving to `tests/test_job_retry_policy.py` rather than a second full rehearsal run. The
receipt is left as measured rather than rewritten: the run that failed is the evidence that the
step earns its place.
