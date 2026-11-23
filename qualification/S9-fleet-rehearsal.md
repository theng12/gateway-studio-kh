# S9 — fleet acceptance rehearsal

- **Run:** 2026-09-28T07:52:06+00:00 (finished 2026-09-28T07:58:04+00:00)
- **Driver:** `~/Developer/_orchestra/rehearsal/run_rehearsal.py`
- **Work dir (kept):** `/private/var/folders/p8/wwyv7rc96s51h1b1rrx6scmc0000gn/T/s9-rehearsal-3149t3zc`
- **Studio Hub under test:** `HEAD = 94d5bfd`
- **Gateway under test:** `HEAD = 89a1880`
- **Hub source root:** `/private/var/folders/p8/wwyv7rc96s51h1b1rrx6scmc0000gn/T/s9-rehearsal-3149t3zc/src/studio-hub`
- **Gateway source root:** `/private/var/folders/p8/wwyv7rc96s51h1b1rrx6scmc0000gn/T/s9-rehearsal-3149t3zc/src/gateway-kh`
- **Hub interpreter:** `~/Developer/studio-hub-kh/.build/hub-gate/bin/python3`
- **Gateway interpreter:** `~/Developer/gateway-kh/repository/.venv/bin/python`

## What was rehearsed

| piece | what it really is |
| --- | --- |
| Studio Hub × 5 | the real `components.hub.standalone.app`, its own temp data dir, its own Hub token, `STUDIOHUB_REQUIRE_AUTH=1`, on its own loopback port |
| Engine per Mac | a rehearsal-owned worker stub answering the Hub's worker contract (`/api/health`, `/api/catalog`, `/api/generate/txt2img`, `/api/generate/jobs/<id>`) |
| Model | `rehearsal-image` (`rehearsal/image-1`, engine `image-mflux`), receipt written by the Hub's own `components.model_packages` verifier over real bytes |
| Gateway | isolated recipe from `tests/test_e2e.py`: temp `GATEWAY_DATA`/`_APP_SUPPORT``/DB`/`CREDENTIALS`/`MODEL_CACHE`/`ENV_FILE`, own port and API key, `GATEWAY_NO_MIGRATE=1`, `STUDIOHUB_READY_FILE` pointing at nothing |



## Summary

| # | check | result | measured |
| ---: | --- | --- | --- |
| 1 | a node joins and both UIs show it | PASS | controller roster: {'total': 3, 'reachable': 3, 'unreachable': 0, 'unauthorized': 0, 'error': 0, 'removed': 0}; gateway node_summary: {'total': 3, 'reachable': 3, 'unreachable': 0, 'unauthorized': 0, 'error': 0, 'removed |
| 2 | one model row, N of M machines, one /v1/models row | PASS | 2 locations, one row; one enable action -> 2 of 2 enabled; /v1/models rows for the model: 1 |
| 3 | a site of M Macs accepts M concurrent jobs | PASS | M=4 Macs at Site A: admission limit 4, peak concurrent running 4 (gateway) / 4 (site) |
| 4 | a node leaves; a removed node stays removed | PASS | leave HTTP 200 -> gateway node_summary {'total': 2, 'reachable': 2, 'unreachable': 0, 'unauthorized': 0, 'error': 0, 'removed': 1}; removed=['node-e']; re-join HTTP 403 |
| 5 | the location is re-pointed and keeps routing + enablement | PASS | location 11861ec47da8 -> 11861ec47da8 at http://127.0.0.1:50567; model enabled on 2 of 2; job routed there: succeeded |
| 6 | a failed Mac is re-run on another Mac and succeeds | PASS | inside the site: succeeded on machines ['local', 'node-b', 'node-c', 'node-e'] (9 item readings); across locations: succeeded after 0 recorded failure(s) on [] |
| 7 | queue position, its bound, and /health concurrency | PASS | queue position(s) [1, 2, 3] of [3, 3, 3], bound max_queued_jobs=256; published 30 == live fleet 30: True; /health concurrency={'max_queued_jobs': 256, 'generation_submissions_per_minute': 60, 'paid_concurrency_limit': 30 |
| 8 | node promotion + handover, zero touches per node | PASS | handover to http://127.0.0.1:50545: {'node-c': 'verified'}, verified 1/1; followers ['node-c'] with {'node-c': 0} touches; Gateway model still offered+enabled after the re-point: {'model_id': 'rehearsal/image-1', 'locati |
| 9 | eviction is permanent, the neighbour still works | PASS | restart -> own credential HTTP 401; join with the Fleet token refused HTTP 403 (this controller removed this Mac on 2026-09-28; re-admit it from Connections to join again); neighbour job succeeded on ['local', 'local', ' |
| 10 | one cluster down does not hurt the others | PASS | cluster down: reachable=False, node_summary={'total': 1, 'reachable': 1, 'unreachable': 0, 'unauthorized': 0, 'error': 0, 'removed': 0}, members_from=1790582069.295191, stale=38.040940046310425s; catalogue rows for the m |
| 11 | published capacity is the fleet's, live | PASS | paid_concurrency_limit 36 -> 18 (two Macs stopped) -> 36 (brought back) |
| 12 | three attempts on three machines, no duplicates | PASS | attempt 3 succeeded after failures on ['studio-hub-2af7a7aa3992', 'studio-hub-11861ec47da8'] (1 job(s) sampled, batch appeared at ['node-b', 'controller-c', 'controller-d']); terminal failure named ['studio-hub-2af7a7aa3 |

## 1. a node joins and both UIs show it

- **Result:** PASS
- **Measured (0.65s):** 3 nodes joined; the controller lists 3/3 with a version (['0.6.93', '0.6.93', '0.6.93']); the Gateway's row for Site A carries node_summary={'total': 3, 'reachable': 3, 'unreachable': 0, 'unauthorized': 0, 'error': 0, 'removed': 0}

_controller roster: {'total': 3, 'reachable': 3, 'unreachable': 0, 'unauthorized': 0, 'error': 0, 'removed': 0}; gateway node_summary: {'total': 3, 'reachable': 3, 'unreachable': 0, 'unauthorized': 0, 'error': 0, 'removed': 0}_

```json
{
  "join": {
    "node-b": {
      "join": {
        "node": {
          "id": "node-b",
          "name": "Node B",
          "address": "http://127.0.0.1:50545",
          "role": "agent",
          "studios": [
            {
              "id": "image",
              "modality": "image"
            }
          ],
          "joined_at": 1790581936.3199282,
          "updated_at": 1790581936.3199282,
          "last_seen_at": 1790581936.3199282,
          "credential_hash": "7130328a8b16949ac77bd8d1db6366ce2746c90274730da4cd7e1807884a59a4",
          "credential_issued_at": 1790581936.3199282
        },
        "credential": "5u4Zjd6EKNhFPZ4wk-q-edcWlZNFzxeogQvOCvA1Ppw"
      },
      "adopt": {
        "ok": true,
        "adopted": true,
        "controller_url": "http://127.0.0.1:50537"
      }
    },
    "node-c": {
      "join": {
        "node": {
          "id": "node-c",
          "name": "Node C",
          "address": "http://127.0.0.1:50549",
          "role": "agent",
          "studios": [
            {
              "id": "image",
              "modality": "image"
            }
          ],
          "joined_at": 1790581936.3297439,
          "updated_at": 1790581936.3297439,
          "last_seen_at": 1790581936.3297439,
          "credential_hash": "1162ab2f6bfe441cac5e6c336ff8a64fc5ed4c51d107c61434678a908c4289aa",
          "credential_issued_at": 1790581936.3297439
        },
        "credential": "z6ABpVgtJ5DCzoK5V-GLPLwjHudG_ORzgDt2zpiZkIQ"
      },
      "adopt": {
        "ok": true,
        "adopted": true,
        "controller_url": "http://127.0.0.1:50537"
      }
    },
    "node-e": {
      "join": {
        "node": {
          "id": "node-e",
          "name": "Node E",
          "address": "http://127.0.0.1:50563",
          "role": "agent",
          "studios": [
            {
              "id": "image",
              "modality": "image"
            }
          ],
          "joined_at": 1790581936.337899,
          "updated_at": 1790581936.337899,
          "last_seen_at": 1790581936.337899,
          "credential_hash": "82fc5df35aed8d019cb9cd0a46075f3adb90af58476cf69a11bc52f636cf0db1",
          "credential_issued_at": 1790581936.337899
        },
        "credential": "JEAz8kG3ucnFgXidI-HNxNRbJpzHWhruQDXG82j0BZI"
      },
      "adopt": {
        "ok": true,
        "adopted": true,
        "controller_url": "http://127.0.0.1:50537"
      }
    },
    "node-f": {
      "join": {
        "node": {
          "id": "node-f",
          "name": "Node F",
          "address": "http://127.0.0.1:50576",
          "role": "agent",
          "studios": [
            {
              "id": "image",
              "modality": "image"
            }
          ],
          "joined_at": 1790581936.345454,
          "updated_at": 1790581936.345454,
          "last_seen_at": 1790581936.345454,
          "credential_hash": "51ce02ddbc5cef910cbcea0c60642714dd8d967b2ccede40c0be7174d539682c",
          "credential_issued_at": 1790581936.345454
        },
        "credential": "u76ogA4tdN9cnwpVw-7PNZJYq1FQwKx11dOxEABc5j8"
      },
      "adopt": {
        "ok": true,
        "adopted": true,
        "controller_url": "http://127.0.0.1:50567"
      }
    }
  },
  "controller_a_nodes": [
    {
      "id": "node-b",
      "name": "Node B",
      "address": "http://127.0.0.1:50545",
      "role": "agent",
      "studios": [
        {
          "id": "image",
          "modality": "image"
        }
      ],
      "joined_at": 1790581936.3199282,
      "last_seen_at": 1790581936.445239,
      "updated_at": 1790581936.3199282,
      "credential_issued_at": 1790581936.3199282,
      "has_credential": true,
      "state": "reachable",
      "detail": null,
      "host": {
        "chip": "Apple M2 Max",
        "machine_type": "Mac Studio",
        "total_gb": 34.36,
        "used_gb": 10.1,
        "available_gb": 22.18,
        "percent": 35.4,
        "cpu_percent": 33.9,
        "cpu_count": 12,
        "load_avg": [
          2.74,
          2.81,
          2.59
        ],
        "disk_free_gb": 126.43,
        "disk_total_gb": 494.38
      },
      "version": "0.6.93",
      "source": null
    },
    {
      "id": "node-c",
      "name": "Node C",
      "address": "http://127.0.0.1:50549",
      "role": "agent",
      "studios": [
        {
          "id": "image",
          "modality": "image"
        }
      ],
      "joined_at": 1790581936.3297439,
      "last_seen_at": 1790581936.445239,
      "updated_at": 1790581936.3297439,
      "credential_issued_at": 1790581936.3297439,
      "has_credential": true,
      "state": "reachable",
      "detail": null,
      "host": {
        "chip": "Apple M2 Max",
        "machine_type": "Mac Studio",
        "total_gb": 34.36,
        "used_gb": 10.1,
        "available_gb": 22.18,
        "percent": 35.4,
        "cpu_percent": 33.9,
        "cpu_count": 12,
        "load_avg": [
          2.74,
          2.81,
          2.59
        ],
        "disk_free_gb": 126.43,
        "disk_total_gb": 494.38
      },
      "version": "0.6.93",
      "source": null
    },
    {
      "id": "node-e",
      "name": "Node E",
      "address": "http://127.0.0.1:50563",
      "role": "agent",
      "studios": [
        {
          "id": "image",
          "modality": "image"
        }
      ],
      "joined_at": 1790581936.337899,
      "last_seen_at": 1790581936.445239,
      "updated_at": 1790581936.337899,
      "credential_issued_at": 1790581936.337899,
      "has_credential": true,
      "state": "reachable",
      "detail": null,
      "host": {
        "chip": "Apple M2 Max",
        "machine_type": "Mac Studio",
        "total_gb": 34.36,
        "used_gb": 10.1,
        "available_gb": 22.18,
        "percent": 35.4,
        "cpu_percent": 33.9,
        "cpu_count": 12,
        "load_avg": [
          2.74,
          2.81,
          2.59
        ],
        "disk_free_gb": 126.43,
        "disk_total_gb": 494.38
      },
      "version": "0.6.93",
      "source": null
    }
  ],
  "controller_a_summary": {
    "total": 3,
    "reachable": 3,
    "unreachable": 0,
    "unauthorized": 0,
    "error": 0,
    "removed": 0
  },
  "gateway_add_a": {
    "controller": {
      "id": "2af7a7aa3992",
      "label": "Site A",
      "url": "http://127.0.0.1:50537",
      "site_id": "s9-site-a",
      "token_set": true,
      "token_hint": "48vV\u2026QX_F",
      "added_at": 1790581936.615804,
      "created": true
    },
    "credential": {
      "scope": "fleet",
      "detail": "A valid Fleet token \u2014 the work badge, and the credential this gateway runs work with."
    },
    "discovered": 1,
    "discovery_error": null,
    "enable_seed": {
      "provider_id": "studio-hub-2af7a7aa3992",
      "source_provider_id": "studio-hub-local",
      "models_discovered": 1,
      "enabled": 0,
      "disabled": 0,
      "no_decision": 1,
      "sentence": "0 of 1 models enabled here, from this Mac's Hub's own decisions; 0 are off there too, and 1 the local Hub has never seen stays off."
    },
    "controllers": [
      {
        "id": "2af7a7aa3992",
        "label": "Site A",
        "url": "http://127.0.0.1:50537",
        "site_id": "s9-site-a",
        "token_set": true,
        "token_hint": "48vV\u2026QX_F",
        "added_at": 1790581936.615804
      }
    ]
  },
  "gateway_add_c": {
    "controller": {
      "id": "11861ec47da8",
      "label": "Controller C",
      "url": "http://127.0.0.1:50567",
      "site_id": "s9-site-c",
      "token_set": true,
      "token_hint": "-dyc\u2026LLMK",
      "added_at": 1790581936.7207732,
      "created": true
    },
    "credential": {
      "scope": "fleet",
      "detail": "A valid Fleet token \u2014 the work badge, and the credential this gateway runs work with."
    },
    "discovered": 1,
    "discovery_error": null,
    "enable_seed": {
      "provider_id": "studio-hub-11861ec47da8",
      "source_provider_id": "studio-hub-local",
      "models_discovered": 1,
      "enabled": 0,
      "disabled": 0,
      "no_decision": 1,
      "sentence": "0 of 1 models enabled here, from this Mac's Hub's own decisions; 0 are off there too, and 1 the local Hub has never seen stays off."
    },
    "controllers": [
      {
        "id": "2af7a7aa3992",
        "label": "Site A",
        "url": "http://127.0.0.1:50537",
        "site_id": "s9-site-a",
        "token_set": true,
        "token_hint": "48vV\u2026QX_F",
        "added_at": 1790581936.615804
      },
      {
        "id": "11861ec47da8",
        "label": "Controller C",
        "url": "http://127.0.0.1:50567",
        "site_id": "s9-site-c",
        "token_set": true,
        "token_hint": "-dyc\u2026LLMK",
        "added_at": 1790581936.7207732
      }
    ]
  },
  "gateway_location_a": {
    "id": "2af7a7aa3992",
    "reachable": true,
    "role": "controller",
    "version": "0.6.93",
    "site_id": "s9-site-a",
    "controller_id": "s9-controller-a",
    "nodes": [
      {
        "id": "node-b",
        "name": "Node B",
        "state": "reachable",
        "version": "0.6.93",
        "role": "agent"
      },
      {
        "id": "node-c",
        "name": "Node C",
        "state": "reachable",
        "version": "0.6.93",
        "role": "agent"
      },
      {
        "id": "node-e",
        "name": "Node E",
        "state": "reachable",
        "version": "0.6.93",
        "role": "agent"
      }
    ],
    "node_summary": {
      "total": 3,
      "reachable": 3,
      "unreachable": 0,
      "unauthorized": 0,
      "error": 0,
      "removed": 0
    }
  }
}
```

- **Reproduce:** `python3 run_rehearsal.py --only 1`

## 2. one model row, N of M machines, one /v1/models row

- **Result:** PASS
- **Measured (0.01s):** one row per model (locations_total=2, locations_enabled 0 -> 2), one enable action changed 2 locations; /v1/models returns 1 row with legacy_ids=['studio-hub-2af7a7aa3992::rehearsal/image-1', 'studio-hub-11861ec47da8::rehearsal/image-1']

_2 locations, one row; one enable action -> 2 of 2 enabled; /v1/models rows for the model: 1_

```json
{
  "row_before": {
    "model_id": "rehearsal/image-1",
    "modality": "image",
    "locations_total": 2,
    "locations_enabled": 0,
    "provider_id": "studio-hub-2af7a7aa3992",
    "provider_ids": [
      "studio-hub-2af7a7aa3992",
      "studio-hub-11861ec47da8"
    ],
    "enabled": false
  },
  "rows_for_this_model": 1,
  "enable_action": {
    "changed": 2,
    "enabled": true
  },
  "row_after": {
    "locations_total": 2,
    "locations_enabled": 2,
    "enabled": true,
    "locations": [
      {
        "provider_id": "studio-hub-2af7a7aa3992",
        "provider_name": "Site A",
        "provider_enabled": true,
        "display_name": "image-1",
        "modality": "image",
        "source": "discovered",
        "enabled": true,
        "free_lane": false,
        "data_use": "unverified"
      },
      {
        "provider_id": "studio-hub-11861ec47da8",
        "provider_name": "Controller C",
        "provider_enabled": true,
        "display_name": "image-1",
        "modality": "image",
        "source": "discovered",
        "enabled": true,
        "free_lane": false,
        "data_use": "unverified"
      }
    ]
  },
  "catalogue_after_enable_seconds": 0.0,
  "v1_models_status": 200,
  "v1_models_rows_for_model": 1,
  "v1_models_row": {
    "id": "studio-hub::rehearsal/image-1",
    "model_id": null,
    "availability": "available",
    "legacy_ids": [
      "studio-hub-2af7a7aa3992::rehearsal/image-1",
      "studio-hub-11861ec47da8::rehearsal/image-1"
    ],
    "provider_name": "Studio Hub",
    "locations": null,
    "execution_label": "Local"
  },
  "v1_image_models_rows_for_model": 1
}
```

- **Reproduce:** `python3 run_rehearsal.py --only 2`

## 3. a site of M Macs accepts M concurrent jobs

- **Result:** PASS
- **Measured (17.16s):** M=4 Macs at the site; admission for the image lane is 4 (hub_free=4, hub_total=4), and 4 jobs ran at once (gateway peak 4, site peak 4)

_M=4 Macs at Site A: admission limit 4, peak concurrent running 4 (gateway) / 4 (site)_

```json
{
  "locations_enabled_before": [
    "studio-hub-2af7a7aa3992",
    "studio-hub-11861ec47da8"
  ],
  "disable_c": {
    "provider": {
      "id": "studio-hub-11861ec47da8",
      "display_name": "Controller C",
      "category": "multi",
      "kind": "studio_hub",
      "scope": "local",
      "description": "Studio Hub controller at http://127.0.0.1:50567.",
      "docs_url": "",
      "notes": "Added on the Fleet page.",
      "modalities": [
        "chat",
        "image",
        "tts",
        "music",
        "transcription",
        "video"
      ],
      "base_url": "http://127.0.0.1:50567",
      "discovery": "studio_hub",
      "enabled": false,
      "configured": true,
      "configured_fields": [
        {
          "key": "hub_token",
          "label": "Fleet token",
          "env_var": "FLEET_11861EC47DA8_HUB_TOKEN",
          "secret": true,
          "required": true,
          "placeholder": "the controller's Fleet token",
          "help_text": "The work badge for the group. From the controller's Connections page, the token labelled Fleet token. It sends work, follows the job, downloads and acknowledges the result, cancels or discards an uncertain one, and reads the model and fleet lists; it cannot change settings, install or remove models, delete output, manage shared voices, or make a Mac join or leave. This is the token GatewayKH needs to run work.",
          "source": "stored",
          "present": true,
          "masked": "-dyc\u2022\u2022\u2022\u2022\u2022\u2022LLMK"
        },
        {
          "key": "admin_token",
          "label": "Hub token (shared voices only)",
          "env_var": "FLEET_11861EC47DA8_ADMIN_TOKEN",
          "secret": true,
          "required": false,
          "placeholder": "the controller's Hub token",
          "help_text": "The Mac's own key. From the controller's Connections page, the token labelled Hub token. Optional, and needed only to create, list or remove shared voices on that controller \u2014 the Hub refuses those routes to a fleet token on purpose, so a node's credential can never administer a controller. Never send it unless GatewayKH will manage shared voices there; anyone who holds it can do everything on that Mac.",
          "source": "none",
          "present": false,
          "masked": ""
        }
      ],
      "model_count": 1,
      "enabled_model_count": 1,
      "publishable_model_count": 1,
      "last_refresh": 1790581936.7317271,
      "last_error": null,
      "skipped": []
    }
  },
  "machine_summary": {
    "total": 4,
    "reachable": 4,
    "unreachable": 0
  },
  "capacity": {
    "queue_depth": 0,
    "available_physical_machine_slots": 4,
    "eligible_physical_machine_slots_total": 4,
    "eligible_worker_service_slots_total": 4,
    "eligible_worker_services": 4,
    "shared_physical_machine_slots": true,
    "by_operation": {
      "image.text_to_image": {
        "workers_total": 4,
        "workers_online": 4,
        "workers_ready": 4,
        "available_worker_slots": 4,
        "eligible_physical_machine_slots_total": 4,
        "eligible_worker_service_slots_total": 4,
        "available_physical_machine_slots": 4
      }
    }
  },
  "admission_image": {
    "limit": 4,
    "source": "hub",
    "detail": "the site reports 4 available worker slots for image.text_to_image.",
    "operation": "image.text_to_image",
    "hub_free": 4,
    "hub_total": 4,
    "hub_unit": "physical_machine",
    "in_flight": 0
  },
  "budget_arithmetic": {
    "free": 4,
    "ceiling": 4,
    "in_flight": 2,
    "budget": {
      "limit": 4,
      "source": "hub",
      "detail": "the site reports 4 available worker slots for image.text_to_image. 2 of those are already this gateway's own jobs, added back because the site's free count is net of them \u2014 a site is not charged twice for the Macs it is already running work on. The site's own total is 4, so that is the ceiling: a stale reading is not licence to over-schedule it."
    },
    "stderr": ""
  },
  "submitted": [
    {
      "status": 202,
      "id": "4c9c1b59-2112-4466-baf4-f01f3aad38e1",
      "queue_position": 1,
      "detail": null
    },
    {
      "status": 202,
      "id": "9d8ea565-819b-4c29-a578-955034253ac8",
      "queue_position": 2,
      "detail": null
    },
    {
      "status": 202,
      "id": "c316dd38-90e3-4c02-9e03-04cc368b1b28",
      "queue_position": 3,
      "detail": null
    },
    {
      "status": 202,
      "id": "3126200f-e7a4-450b-a74b-b6f3bd6c76d1",
      "queue_position": 4,
      "detail": null
    }
  ],
  "peak": {
    "gateway_peak_running": 4,
    "site_peak_running": 4,
    "samples": 46
  },
  "jobs": [
    {
      "id": "4c9c1b59-2112-4466-baf4-f01f3aad38e1",
      "state": "succeeded"
    },
    {
      "id": "9d8ea565-819b-4c29-a578-955034253ac8",
      "state": "succeeded"
    },
    {
      "id": "c316dd38-90e3-4c02-9e03-04cc368b1b28",
      "state": "succeeded"
    },
    {
      "id": "3126200f-e7a4-450b-a74b-b6f3bd6c76d1",
      "state": "succeeded"
    }
  ],
  "final_states": [
    "succeeded",
    "succeeded",
    "succeeded",
    "succeeded"
  ],
  "enable_c": {
    "provider": {
      "id": "studio-hub-11861ec47da8",
      "display_name": "Controller C",
      "category": "multi",
      "kind": "studio_hub",
      "scope": "local",
      "description": "Studio Hub controller at http://127.0.0.1:50567.",
      "docs_url": "",
      "notes": "Added on the Fleet page.",
      "modalities": [
        "chat",
        "image",
        "tts",
        "music",
        "transcription",
        "video"
      ],
      "base_url": "http://127.0.0.1:50567",
      "discovery": "studio_hub",
      "enabled": true,
      "configured": true,
      "configured_fields": [
        {
          "key": "hub_token",
          "label": "Fleet token",
          "env_var": "FLEET_11861EC47DA8_HUB_TOKEN",
          "secret": true,
          "required": true,
          "placeholder": "the controller's Fleet token",
          "help_text": "The work badge for the group. From the controller's Connections page, the token labelled Fleet token. It sends work, follows the job, downloads and acknowledges the result, cancels or discards an uncertain one, and reads the model and fleet lists; it cannot change settings, install or remove models, delete output, manage shared voices, or make a Mac join or leave. This is the token GatewayKH needs to run work.",
          "source": "stored",
          "present": true,
          "masked": "-dyc\u2022\u2022\u2022\u2022\u2022\u2022LLMK"
        },
        {
          "key": "admin_token",
          "label": "Hub token (shared voices only)",
          "env_var": "FLEET_11861EC47DA8_ADMIN_TOKEN",
          "secret": true,
          "required": false,
          "placeholder": "the controller's Hub token",
          "help_text": "The Mac's own key. From the controller's Connections page, the token labelled Hub token. Optional, and needed only to create, list or remove shared voices on that controller \u2014 the Hub refuses those routes to a fleet token on purpose, so a node's credential can never administer a controller. Never send it unless GatewayKH will manage shared voices there; anyone who holds it can do everything on that Mac.",
          "source": "none",
          "present": false,
          "masked": ""
        }
      ],
      "model_count": 1,
      "enabled_model_count": 1,
      "publishable_model_count": 1,
      "last_refresh": 1790581951.2906759,
      "last_error": null,
      "skipped": []
    }
  }
}
```

- **Reproduce:** `python3 run_rehearsal.py --only 3`

## 4. a node leaves; a removed node stays removed

- **Result:** PASS
- **Measured (2.42s):** the node removed itself with its own credential (HTTP 200) and vanished from the controller roster and the Gateway's row at once ({'total': 2, 'reachable': 2, 'unreachable': 0, 'unauthorized': 0, 'error': 0, 'removed': 1}); the controller recorded it as removed ([{'id': 'node-e', 'name': 'Node E', 'removed_at': 1790581954.1522508, 'by': None}]) and refused its next join by name (HTTP 403: this controller removed this Mac on 2026-09-28; re-admit it from Connections to join again)

_leave HTTP 200 -> gateway node_summary {'total': 2, 'reachable': 2, 'unreachable': 0, 'unauthorized': 0, 'error': 0, 'removed': 1}; removed=['node-e']; re-join HTTP 403_

```json
{
  "before": {
    "controller": {
      "total": 3,
      "reachable": 3,
      "unreachable": 0,
      "unauthorized": 0,
      "error": 0,
      "removed": 0
    },
    "in_roster": true,
    "gateway": {
      "total": 3,
      "reachable": 3,
      "unreachable": 0,
      "unauthorized": 0,
      "error": 0,
      "removed": 0
    }
  },
  "leave": [
    200,
    {
      "ok": true,
      "removed": {
        "id": "node-e",
        "name": "Node E",
        "address": "http://127.0.0.1:50563",
        "removed_at": 1790581954.1522508,
        "removed_by": "node-e",
        "role": "agent",
        "studios": [
          {
            "id": "image",
            "modality": "image"
          }
        ],
        "joined_at": 1790581936.337899
      }
    }
  ],
  "controller_after_leave": {
    "total": 2,
    "reachable": 2,
    "unreachable": 0,
    "unauthorized": 0,
    "error": 0,
    "removed": 1
  },
  "controller_removed_list": [
    {
      "id": "node-e",
      "name": "Node E",
      "removed_at": 1790581954.1522508,
      "by": null
    }
  ],
  "gateway_after_leave": {
    "total": 2,
    "reachable": 2,
    "unreachable": 0,
    "unauthorized": 0,
    "error": 0,
    "removed": 1
  },
  "rejoin_attempt": {
    "status": 403,
    "payload": {
      "detail": "this controller removed this Mac on 2026-09-28; re-admit it from Connections to join again"
    }
  },
  "controller_after_attempt": {
    "total": 2,
    "reachable": 2,
    "unreachable": 0,
    "unauthorized": 0,
    "error": 0,
    "removed": 1
  }
}
```

- **Reproduce:** `python3 run_rehearsal.py --only 4`

## 5. the location is re-pointed and keeps routing + enablement

- **Result:** PASS
- **Measured (6.4s):** the same controller at a new address kept its location row (11861ec47da8 == 11861ec47da8), its provider id and its enablement (2 of 2 locations), and a job routed there succeeded

_location 11861ec47da8 -> 11861ec47da8 at http://127.0.0.1:50567; model enabled on 2 of 2; job routed there: succeeded_

```json
{
  "before": {
    "location_id": "11861ec47da8",
    "provider_id": "studio-hub-11861ec47da8",
    "url": "http://127.0.0.1:50567",
    "model": {
      "locations_total": 2,
      "locations_enabled": 2,
      "locations": [
        {
          "provider_id": "studio-hub-2af7a7aa3992",
          "provider_name": "Site A",
          "provider_enabled": true,
          "display_name": "image-1",
          "modality": "image",
          "source": "discovered",
          "enabled": true,
          "free_lane": false,
          "data_use": "unverified"
        },
        {
          "provider_id": "studio-hub-11861ec47da8",
          "provider_name": "Controller C",
          "provider_enabled": true,
          "display_name": "image-1",
          "modality": "image",
          "source": "discovered",
          "enabled": true,
          "free_lane": false,
          "data_use": "unverified"
        }
      ]
    }
  },
  "restart": {
    "old_url": "http://127.0.0.1:50567",
    "new_url": "http://127.0.0.1:50567",
    "same_port": true
  },
  "replace_address_action": {
    "status": 200,
    "created": false,
    "controller": {
      "id": "11861ec47da8",
      "label": "Controller C",
      "url": "http://127.0.0.1:50567",
      "site_id": "s9-site-c",
      "token_set": true,
      "token_hint": "-dyc\u2026LLMK",
      "added_at": 1790581936.7207732,
      "created": false
    },
    "discovery_error": null
  },
  "after": {
    "location_id": "11861ec47da8",
    "url": "http://127.0.0.1:50567",
    "provider_id": "studio-hub-11861ec47da8",
    "model": {
      "locations_total": 2,
      "locations_enabled": 2,
      "locations": [
        {
          "provider_id": "studio-hub-2af7a7aa3992",
          "provider_name": "Site A",
          "provider_enabled": true,
          "display_name": "image-1",
          "modality": "image",
          "source": "discovered",
          "enabled": true,
          "free_lane": false,
          "data_use": "unverified"
        },
        {
          "provider_id": "studio-hub-11861ec47da8",
          "provider_name": "Controller C",
          "provider_enabled": true,
          "display_name": "image-1",
          "modality": "image",
          "source": "discovered",
          "enabled": true,
          "free_lane": false,
          "data_use": "unverified"
        }
      ]
    },
    "admission_image": {
      "limit": 2,
      "source": "hub",
      "detail": "the site reports 2 available worker slots for image.text_to_image.",
      "operation": "image.text_to_image",
      "hub_free": 2,
      "hub_total": 2,
      "hub_unit": "physical_machine",
      "in_flight": 0
    }
  },
  "disable_a": {
    "provider": {
      "id": "studio-hub-2af7a7aa3992",
      "display_name": "Site A",
      "category": "multi",
      "kind": "studio_hub",
      "scope": "local",
      "description": "Studio Hub controller at http://127.0.0.1:50537.",
      "docs_url": "",
      "notes": "Added on the Fleet page.",
      "modalities": [
        "chat",
        "image",
        "tts",
        "music",
        "transcription",
        "video"
      ],
      "base_url": "http://127.0.0.1:50537",
      "discovery": "studio_hub",
      "enabled": false,
      "configured": true,
      "configured_fields": [
        {
          "key": "hub_token",
          "label": "Fleet token",
          "env_var": "FLEET_2AF7A7AA3992_HUB_TOKEN",
          "secret": true,
          "required": true,
          "placeholder": "the controller's Fleet token",
          "help_text": "The work badge for the group. From the controller's Connections page, the token labelled Fleet token. It sends work, follows the job, downloads and acknowledges the result, cancels or discards an uncertain one, and reads the model and fleet lists; it cannot change settings, install or remove models, delete output, manage shared voices, or make a Mac join or leave. This is the token GatewayKH needs to run work.",
          "source": "stored",
          "present": true,
          "masked": "48vV\u2022\u2022\u2022\u2022\u2022\u2022QX_F"
        },
        {
          "key": "admin_token",
          "label": "Hub token (shared voices only)",
          "env_var": "FLEET_2AF7A7AA3992_ADMIN_TOKEN",
          "secret": true,
          "required": false,
          "placeholder": "the controller's Hub token",
          "help_text": "The Mac's own key. From the controller's Connections page, the token labelled Hub token. Optional, and needed only to create, list or remove shared voices on that controller \u2014 the Hub refuses those routes to a fleet token on purpose, so a node's credential can never administer a controller. Never send it unless GatewayKH will manage shared voices there; anyone who holds it can do everything on that Mac.",
          "source": "none",
          "present": false,
          "masked": ""
        }
      ],
      "model_count": 1,
      "enabled_model_count": 1,
      "publishable_model_count": 1,
      "last_refresh": 1790581951.2802188,
      "last_error": null,
      "skipped": []
    }
  },
  "routing_job": {
    "submit_status": 202,
    "id": "c0661d83-6731-4b7f-92e9-05daba642f94",
    "state": "succeeded",
    "provider_id": null,
    "attempts": null
  },
  "enable_a": {
    "provider": {
      "id": "studio-hub-2af7a7aa3992",
      "display_name": "Site A",
      "category": "multi",
      "kind": "studio_hub",
      "scope": "local",
      "description": "Studio Hub controller at http://127.0.0.1:50537.",
      "docs_url": "",
      "notes": "Added on the Fleet page.",
      "modalities": [
        "chat",
        "image",
        "tts",
        "music",
        "transcription",
        "video"
      ],
      "base_url": "http://127.0.0.1:50537",
      "discovery": "studio_hub",
      "enabled": true,
      "configured": true,
      "configured_fields": [
        {
          "key": "hub_token",
          "label": "Fleet token",
          "env_var": "FLEET_2AF7A7AA3992_HUB_TOKEN",
          "secret": true,
          "required": true,
          "placeholder": "the controller's Fleet token",
          "help_text": "The work badge for the group. From the controller's Connections page, the token labelled Fleet token. It sends work, follows the job, downloads and acknowledges the result, cancels or discards an uncertain one, and reads the model and fleet lists; it cannot change settings, install or remove models, delete output, manage shared voices, or make a Mac join or leave. This is the token GatewayKH needs to run work.",
          "source": "stored",
          "present": true,
          "masked": "48vV\u2022\u2022\u2022\u2022\u2022\u2022QX_F"
        },
        {
          "key": "admin_token",
          "label": "Hub token (shared voices only)",
          "env_var": "FLEET_2AF7A7AA3992_ADMIN_TOKEN",
          "secret": true,
          "required": false,
          "placeholder": "the controller's Hub token",
          "help_text": "The Mac's own key. From the controller's Connections page, the token labelled Hub token. Optional, and needed only to create, list or remove shared voices on that controller \u2014 the Hub refuses those routes to a fleet token on purpose, so a node's credential can never administer a controller. Never send it unless GatewayKH will manage shared voices there; anyone who holds it can do everything on that Mac.",
          "source": "none",
          "present": false,
          "masked": ""
        }
      ],
      "model_count": 1,
      "enabled_model_count": 1,
      "publishable_model_count": 1,
      "last_refresh": 1790581951.2802188,
      "last_error": null,
      "skipped": []
    }
  },
  "different_mac": {
    "add_status": 200,
    "created": true,
    "new_location_id": "f199c6cdd168",
    "discovery": 1,
    "enable_seed": {
      "provider_id": "studio-hub-f199c6cdd168",
      "source_provider_id": "studio-hub-local",
      "models_discovered": 1,
      "enabled": 0,
      "disabled": 0,
      "no_decision": 1,
      "sentence": "0 of 1 models enabled here, from this Mac's Hub's own decisions; 0 are off there too, and 1 the local Hub has never seen stays off."
    },
    "model_on_new_location": {
      "model_id": null,
      "locations_total": null,
      "locations_enabled": null
    }
  }
}
```

- **Reproduce:** `python3 run_rehearsal.py --only 5`

## 6. a failed Mac is re-run on another Mac and succeeds

- **Result:** PASS
- **Measured (43.75s):** the Mac that stopped answering was avoided and the item completed on another Mac of the same site (succeeded); across locations no cross-location re-run to success (succeeded: None; attempts=[]; new batches={'controller-a': [], 'controller-c': ['4f1e489a07']})

_inside the site: succeeded on machines ['local', 'node-b', 'node-c', 'node-e'] (9 item readings); across locations: succeeded after 0 recorded failure(s) on []_

```json
{
  "disable_c": {
    "provider": {
      "id": "studio-hub-11861ec47da8",
      "display_name": "Controller C",
      "category": "multi",
      "kind": "studio_hub",
      "scope": "local",
      "description": "Studio Hub controller at http://127.0.0.1:50567.",
      "docs_url": "",
      "notes": "Added on the Fleet page.",
      "modalities": [
        "chat",
        "image",
        "tts",
        "music",
        "transcription",
        "video"
      ],
      "base_url": "http://127.0.0.1:50567",
      "discovery": "studio_hub",
      "enabled": false,
      "configured": true,
      "configured_fields": [
        {
          "key": "hub_token",
          "label": "Fleet token",
          "env_var": "FLEET_11861EC47DA8_HUB_TOKEN",
          "secret": true,
          "required": true,
          "placeholder": "the controller's Fleet token",
          "help_text": "The work badge for the group. From the controller's Connections page, the token labelled Fleet token. It sends work, follows the job, downloads and acknowledges the result, cancels or discards an uncertain one, and reads the model and fleet lists; it cannot change settings, install or remove models, delete output, manage shared voices, or make a Mac join or leave. This is the token GatewayKH needs to run work.",
          "source": "stored",
          "present": true,
          "masked": "-dyc\u2022\u2022\u2022\u2022\u2022\u2022LLMK"
        },
        {
          "key": "admin_token",
          "label": "Hub token (shared voices only)",
          "env_var": "FLEET_11861EC47DA8_ADMIN_TOKEN",
          "secret": true,
          "required": false,
          "placeholder": "the controller's Hub token",
          "help_text": "The Mac's own key. From the controller's Connections page, the token labelled Hub token. Optional, and needed only to create, list or remove shared voices on that controller \u2014 the Hub refuses those routes to a fleet token on purpose, so a node's credential can never administer a controller. Never send it unless GatewayKH will manage shared voices there; anyone who holds it can do everything on that Mac.",
          "source": "none",
          "present": false,
          "masked": ""
        }
      ],
      "model_count": 1,
      "enabled_model_count": 1,
      "publishable_model_count": 1,
      "last_refresh": 1790581958.055158,
      "last_error": null,
      "skipped": []
    }
  },
  "inside_site": {
    "submit_status": 202,
    "id": "4a52ddae-6bd1-48fc-a4b0-8b4c3fe10ec7",
    "state": "succeeded",
    "failure_code": null,
    "failure_reason": null,
    "attempts": null,
    "item_watch": [
      {
        "index": 0,
        "state": "done",
        "machine": "node-e",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": null,
        "runtime_s": 8.07,
        "hub": "controller-a",
        "batch": "f3c230ef29",
        "at": 1790581963.0
      },
      {
        "index": 0,
        "state": "done",
        "machine": "node-c",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": null,
        "runtime_s": 8.08,
        "hub": "controller-a",
        "batch": "0fb8fa7978",
        "at": 1790581963.0
      },
      {
        "index": 0,
        "state": "done",
        "machine": "node-b",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": null,
        "runtime_s": 8.09,
        "hub": "controller-a",
        "batch": "f5977be779",
        "at": 1790581963.0
      },
      {
        "index": 0,
        "state": "done",
        "machine": "local",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": null,
        "runtime_s": 8.02,
        "hub": "controller-a",
        "batch": "9176f521f0",
        "at": 1790581963.0
      },
      {
        "index": 0,
        "state": "done",
        "machine": "local",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": null,
        "runtime_s": 2.01,
        "hub": "controller-c",
        "batch": "6db4c0453a",
        "at": 1790581963.0
      },
      {
        "index": 0,
        "state": "running",
        "machine": "local",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": null,
        "runtime_s": null,
        "hub": "controller-a",
        "batch": "9aa7e52150",
        "at": 1790581964.3
      },
      {
        "index": 0,
        "state": "queued",
        "machine": "local",
        "worker_id": "image",
        "tries": 0,
        "error": "waiting for capacity: MemoryGuardError: not enough memory to load this model right now",
        "avoid_machines": {
          "local": 1790582056.236345
        },
        "runtime_s": 2.01,
        "hub": "controller-a",
        "batch": "9aa7e52150",
        "at": 1790581966.4
      },
      {
        "index": 0,
        "state": "running",
        "machine": "node-b",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": {
          "local": 1790582056.236345
        },
        "runtime_s": null,
        "hub": "controller-a",
        "batch": "9aa7e52150",
        "at": 1790581999.6
      },
      {
        "index": 0,
        "state": "done",
        "machine": "node-b",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": {
          "local": 1790582056.236345
        },
        "runtime_s": 2.02,
        "hub": "controller-a",
        "batch": "9aa7e52150",
        "at": 1790582001.3
      }
    ],
    "new_batches": {
      "controller-a": [
        "9aa7e52150"
      ],
      "controller-c": []
    }
  },
  "enable_c": {
    "provider": {
      "id": "studio-hub-11861ec47da8",
      "display_name": "Controller C",
      "category": "multi",
      "kind": "studio_hub",
      "scope": "local",
      "description": "Studio Hub controller at http://127.0.0.1:50567.",
      "docs_url": "",
      "notes": "Added on the Fleet page.",
      "modalities": [
        "chat",
        "image",
        "tts",
        "music",
        "transcription",
        "video"
      ],
      "base_url": "http://127.0.0.1:50567",
      "discovery": "studio_hub",
      "enabled": true,
      "configured": true,
      "configured_fields": [
        {
          "key": "hub_token",
          "label": "Fleet token",
          "env_var": "FLEET_11861EC47DA8_HUB_TOKEN",
          "secret": true,
          "required": true,
          "placeholder": "the controller's Fleet token",
          "help_text": "The work badge for the group. From the controller's Connections page, the token labelled Fleet token. It sends work, follows the job, downloads and acknowledges the result, cancels or discards an uncertain one, and reads the model and fleet lists; it cannot change settings, install or remove models, delete output, manage shared voices, or make a Mac join or leave. This is the token GatewayKH needs to run work.",
          "source": "stored",
          "present": true,
          "masked": "-dyc\u2022\u2022\u2022\u2022\u2022\u2022LLMK"
        },
        {
          "key": "admin_token",
          "label": "Hub token (shared voices only)",
          "env_var": "FLEET_11861EC47DA8_ADMIN_TOKEN",
          "secret": true,
          "required": false,
          "placeholder": "the controller's Hub token",
          "help_text": "The Mac's own key. From the controller's Connections page, the token labelled Hub token. Optional, and needed only to create, list or remove shared voices on that controller \u2014 the Hub refuses those routes to a fleet token on purpose, so a node's credential can never administer a controller. Never send it unless GatewayKH will manage shared voices there; anyone who holds it can do everything on that Mac.",
          "source": "none",
          "present": false,
          "masked": ""
        }
      ],
      "model_count": 1,
      "enabled_model_count": 1,
      "publishable_model_count": 1,
      "last_refresh": 1790581958.055158,
      "last_error": null,
      "skipped": []
    }
  },
  "across_locations": {
    "submit_status": 202,
    "id": "4eef376d-e10b-486e-bcfd-573ba67ce5ab",
    "state": "succeeded",
    "failure_code": null,
    "failure_reason": null,
    "attempts": null,
    "attempts_used": null,
    "attempts_allowed": null,
    "provider_id": null,
    "item_watch": [
      {
        "index": 0,
        "state": "done",
        "machine": "node-b",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": {
          "local": 1790582056.236345
        },
        "runtime_s": 2.02,
        "hub": "controller-a",
        "batch": "9aa7e52150",
        "at": 1790582003.3
      },
      {
        "index": 0,
        "state": "done",
        "machine": "node-e",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": null,
        "runtime_s": 8.07,
        "hub": "controller-a",
        "batch": "f3c230ef29",
        "at": 1790582003.3
      },
      {
        "index": 0,
        "state": "done",
        "machine": "node-c",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": null,
        "runtime_s": 8.08,
        "hub": "controller-a",
        "batch": "0fb8fa7978",
        "at": 1790582003.3
      },
      {
        "index": 0,
        "state": "done",
        "machine": "node-b",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": null,
        "runtime_s": 8.09,
        "hub": "controller-a",
        "batch": "f5977be779",
        "at": 1790582003.3
      },
      {
        "index": 0,
        "state": "done",
        "machine": "local",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": null,
        "runtime_s": 8.02,
        "hub": "controller-a",
        "batch": "9176f521f0",
        "at": 1790582003.3
      },
      {
        "index": 0,
        "state": "done",
        "machine": "local",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": null,
        "runtime_s": 2.01,
        "hub": "controller-c",
        "batch": "6db4c0453a",
        "at": 1790582003.3
      },
      {
        "index": 0,
        "state": "running",
        "machine": "local",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": null,
        "runtime_s": null,
        "hub": "controller-c",
        "batch": "4f1e489a07",
        "at": 1790582004.6
      },
      {
        "index": 0,
        "state": "done",
        "machine": "local",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": null,
        "runtime_s": 2.01,
        "hub": "controller-c",
        "batch": "4f1e489a07",
        "at": 1790582006.7
      }
    ],
    "new_batches": {
      "controller-a": [],
      "controller-c": [
        "4f1e489a07"
      ]
    }
  }
}
```

- **Reproduce:** `python3 run_rehearsal.py --only 6`

## 7. queue position, its bound, and /health concurrency

- **Result:** PASS
- **Measured (13.43s):** the queue view shows 3 waiting job(s) with a position and the queue size bounding them ([(1, 3), (2, 3), (3, 3)]), the lane bound is [{'modality': 'image', 'waiting': 3, 'in_flight': 3, 'limit': 4, 'source': 'gateway', 'detail': 'no lane cap set, so this lane may use the whole pool'}] and max_queued_jobs is 256; the published concurrency (30) equals the live fleet capacity (30) computed from the same fleet read, and /health's own `concurrency` block publishes the same figure (30)

_queue position(s) [1, 2, 3] of [3, 3, 3], bound max_queued_jobs=256; published 30 == live fleet 30: True; /health concurrency={'max_queued_jobs': 256, 'generation_submissions_per_minute': 60, 'paid_concurrency_limit': 30, 'concurrency_source': 'the live fleet, summed over reachable locations', 'concurrency_by_modality': {'chat': 5, 'image': 5, 'video': 5, 'tts': 5, 'transcription': 5, 'music': 5}, 'concurrency_pool': 4, 'in_flight': 0}_

```json
{
  "capacity_samples": [
    {
      "published_total": 30,
      "published_by_modality": {
        "chat": 5,
        "image": 5,
        "video": 5,
        "tts": 5,
        "transcription": 5,
        "music": 5
      },
      "fleet_total": 30,
      "fleet_by_modality": {
        "chat": 5,
        "image": 5,
        "music": 5,
        "tts": 5,
        "transcription": 5,
        "video": 5
      },
      "locations": [
        {
          "label": "This Mac",
          "reachable": false,
          "image": 0
        },
        {
          "label": "Site A",
          "reachable": true,
          "image": 3
        },
        {
          "label": "Controller C",
          "reachable": true,
          "image": 2
        }
      ]
    },
    {
      "published_total": 30,
      "published_by_modality": {
        "chat": 5,
        "image": 5,
        "video": 5,
        "tts": 5,
        "transcription": 5,
        "music": 5
      },
      "fleet_total": 30,
      "fleet_by_modality": {
        "chat": 5,
        "image": 5,
        "music": 5,
        "tts": 5,
        "transcription": 5,
        "video": 5
      },
      "locations": [
        {
          "label": "This Mac",
          "reachable": false,
          "image": 0
        },
        {
          "label": "Site A",
          "reachable": true,
          "image": 3
        },
        {
          "label": "Controller C",
          "reachable": true,
          "image": 2
        }
      ]
    },
    {
      "published_total": 30,
      "published_by_modality": {
        "chat": 5,
        "image": 5,
        "video": 5,
        "tts": 5,
        "transcription": 5,
        "music": 5
      },
      "fleet_total": 30,
      "fleet_by_modality": {
        "chat": 5,
        "image": 5,
        "music": 5,
        "tts": 5,
        "transcription": 5,
        "video": 5
      },
      "locations": [
        {
          "label": "This Mac",
          "reachable": false,
          "image": 0
        },
        {
          "label": "Site A",
          "reachable": true,
          "image": 3
        },
        {
          "label": "Controller C",
          "reachable": true,
          "image": 2
        }
      ]
    }
  ],
  "health": {
    "status": 200,
    "keys": [
      "build",
      "concurrency",
      "jobs_processed",
      "models",
      "pid",
      "providers",
      "service",
      "status",
      "studio_hub",
      "worker"
    ],
    "concurrency": {
      "max_queued_jobs": 256,
      "generation_submissions_per_minute": 60,
      "paid_concurrency_limit": 30,
      "concurrency_source": "the live fleet, summed over reachable locations",
      "concurrency_by_modality": {
        "chat": 5,
        "image": 5,
        "video": 5,
        "tts": 5,
        "transcription": 5,
        "music": 5
      },
      "concurrency_pool": 4,
      "in_flight": 0
    },
    "paid_concurrency_limit": null,
    "concurrency_number": 30
  },
  "submitted": [
    {
      "status": 202,
      "id": "4d5e0530-cab6-4847-a9d1-05d509ed5b46",
      "queue_position": 1,
      "queue_reason": null,
      "detail": null
    },
    {
      "status": 202,
      "id": "3f271a26-d4ed-4fbc-a78c-52aeef0afe0c",
      "queue_position": 2,
      "queue_reason": null,
      "detail": null
    },
    {
      "status": 202,
      "id": "1e92e1de-b908-4f8b-aee3-c885a5c0a590",
      "queue_position": 3,
      "queue_reason": null,
      "detail": null
    },
    {
      "status": 202,
      "id": "f209cd0a-4db3-460a-b6b6-c23329023a30",
      "queue_position": 4,
      "queue_reason": null,
      "detail": null
    },
    {
      "status": 202,
      "id": "c265570f-9cc7-4d47-9fa1-22739149190d",
      "queue_position": 5,
      "queue_reason": null,
      "detail": null
    },
    {
      "status": 202,
      "id": "c0664ee4-74c0-4abe-a883-1b2b0edbfbd4",
      "queue_position": 6,
      "queue_reason": null,
      "detail": null
    }
  ],
  "queue_view_status": 200,
  "queue_jobs": [
    {
      "id": "f209cd0a-4db3-460a-b6b6-c23329023a30",
      "state": "accepted",
      "queue_position": 1,
      "queue_size": 3,
      "defer_reason": null,
      "attempts_used": 0,
      "attempts_allowed": 3
    },
    {
      "id": "c265570f-9cc7-4d47-9fa1-22739149190d",
      "state": "accepted",
      "queue_position": 2,
      "queue_size": 3,
      "defer_reason": null,
      "attempts_used": 0,
      "attempts_allowed": 3
    },
    {
      "id": "c0664ee4-74c0-4abe-a883-1b2b0edbfbd4",
      "state": "accepted",
      "queue_position": 3,
      "queue_size": 3,
      "defer_reason": null,
      "attempts_used": 0,
      "attempts_allowed": 3
    }
  ],
  "queue_lanes": [
    {
      "modality": "image",
      "waiting": 3,
      "in_flight": 3,
      "limit": 4,
      "source": "gateway",
      "detail": "no lane cap set, so this lane may use the whole pool"
    }
  ],
  "queue_in_flight": {
    "studio-hub-2af7a7aa3992": 3
  },
  "queue_budgets": {
    "studio-hub-2af7a7aa3992": {
      "limit": 3,
      "source": "hub",
      "detail": "the site reports 3 available worker slots for image.text_to_image. 2 of those are already this gateway's own jobs, added back because the site's free count is net of them \u2014 a site is not charged twice for the Macs it is already running work on. The site's own total is 3, so that is the ceiling: a stale reading is not licence to over-schedule it."
    },
    "studio-hub-11861ec47da8": {
      "limit": 2,
      "source": "hub",
      "detail": "the site reports 2 available worker slots for image.text_to_image."
    }
  },
  "published_while_busy": {
    "paid_concurrency_limit": 30,
    "concurrency_by_modality": {
      "chat": 5,
      "image": 5,
      "video": 5,
      "tts": 5,
      "transcription": 5,
      "music": 5
    },
    "max_queued_jobs": 256,
    "concurrency_source": "the live fleet, summed over reachable locations"
  },
  "final_states": [
    "succeeded",
    "succeeded",
    "succeeded",
    "succeeded",
    "succeeded",
    "succeeded"
  ]
}
```

- **Reproduce:** `python3 run_rehearsal.py --only 7`

## 8. node promotion + handover, zero touches per node

- **Result:** PASS
- **Measured (9.88s):** promotion minted a fresh Fleet token (rBzot1ZX…); the handover reached every node ({'node-c': 'verified'}); followers=['node-c'] with {'node-c': 0} write touches per node; the Gateway's location was re-pointed at the new head and the model is still offered and enabled there ({'model_id': 'rehearsal/image-1', 'locations_total': 2, 'locations_enabled': 2})

_handover to http://127.0.0.1:50545: {'node-c': 'verified'}, verified 1/1; followers ['node-c'] with {'node-c': 0} touches; Gateway model still offered+enabled after the re-point: {'model_id': 'rehearsal/image-1', 'locations_total': 2, 'locations_enabled': 2}_

```json
{
  "promote": {
    "before_role": "agent",
    "status": 200,
    "settings": null,
    "token_changed": true,
    "token_hint": "rBzot1ZX\u2026"
  },
  "handover": {
    "status": 200,
    "payload": {
      "ok": true,
      "handed_over_to": "http://127.0.0.1:50545",
      "handed_over_at": 1790582020.209997,
      "promoted": [
        "node-b"
      ],
      "total": 1,
      "verified": 1,
      "manual": 0,
      "pending": 0,
      "machines": {
        "node-c": {
          "ok": true,
          "status": "verified",
          "detail": "saved and verified"
        }
      }
    }
  },
  "touches_per_node": {
    "node-c": 0
  },
  "followers": {
    "node-c": {
      "parent_controller_url": "http://127.0.0.1:50545",
      "role": "agent",
      "holds_the_new_token": true
    }
  },
  "new_head_roster": {
    "role": "controller",
    "machine": {
      "id": "s9-node-b",
      "name": "studio-Mac-Studio"
    },
    "nodes": [
      {
        "id": "s9-node-c",
        "name": "s9-node-c",
        "address": "http://127.0.0.1:50549",
        "role": "agent",
        "studios": [
          {
            "id": "image",
            "modality": "image"
          }
        ],
        "joined_at": 1790582020.1812842,
        "last_seen_at": 1790582022.27647,
        "updated_at": 1790582020.1812842,
        "credential_issued_at": 1790582020.1812842,
        "has_credential": true,
        "state": "reachable",
        "detail": null,
        "host": {
          "chip": "Apple M2 Max",
          "machine_type": "Mac Studio",
          "total_gb": 34.36,
          "used_gb": 10.06,
          "available_gb": 22.22,
          "percent": 35.3,
          "cpu_percent": 5.6,
          "cpu_count": 12,
          "load_avg": [
            2.6,
            2.78,
            2.6
          ],
          "disk_free_gb": 126.43,
          "disk_total_gb": 494.38
        },
        "version": "0.6.93",
        "source": null
      }
    ],
    "removed": [],
    "summary": {
      "total": 1,
      "reachable": 1,
      "unreachable": 0,
      "unauthorized": 0,
      "error": 0,
      "removed": 0
    }
  },
  "gateway_repoint": {
    "status": 200,
    "payload": {
      "controller": {
        "id": "2af7a7aa3992",
        "label": "Site A",
        "url": "http://127.0.0.1:50545",
        "site_id": null,
        "token_set": true,
        "token_hint": "rBzo\u2026HR2H",
        "added_at": 1790581936.615804
      },
      "discovered": 1,
      "discovery_error": null,
      "controllers": [
        {
          "id": "2af7a7aa3992",
          "label": "Site A",
          "url": "http://127.0.0.1:50545",
          "site_id": null,
          "token_set": true,
          "token_hint": "rBzo\u2026HR2H",
          "added_at": 1790581936.615804
        },
        {
          "id": "11861ec47da8",
          "label": "Controller C",
          "url": "http://127.0.0.1:50567",
          "site_id": "s9-site-c",
          "token_set": true,
          "token_hint": "-dyc\u2026LLMK",
          "added_at": 1790581936.7207732
        }
      ]
    },
    "old_row_id": "2af7a7aa3992",
    "enabled_before": 2
  },
  "rejoin_emulated": {
    "node-c": {
      "status": 201,
      "credential_issued": true
    }
  },
  "new_head_roster_after_rejoin": {
    "total": 2,
    "reachable": 2,
    "unreachable": 0,
    "unauthorized": 0,
    "error": 0,
    "removed": 0
  },
  "gateway_after": {
    "location_id": "2af7a7aa3992",
    "old_location_id": "2af7a7aa3992",
    "same_row": true,
    "same_provider": true,
    "provider_id": "studio-hub-2af7a7aa3992",
    "reachable": true,
    "role": "controller",
    "node_summary": {
      "total": 1,
      "reachable": 1,
      "unreachable": 0,
      "unauthorized": 0,
      "error": 0,
      "removed": 0
    },
    "admission_image": {
      "limit": 2,
      "source": "hub",
      "detail": "the site reports 2 available worker slots for image.text_to_image.",
      "operation": "image.text_to_image",
      "hub_free": 2,
      "hub_total": 2,
      "hub_unit": "physical_machine",
      "in_flight": 0
    },
    "model": {
      "model_id": "rehearsal/image-1",
      "locations_total": 2,
      "locations_enabled": 2
    }
  }
}
```

- **Reproduce:** `python3 run_rehearsal.py --only 8`

## 9. eviction is permanent, the neighbour still works

- **Result:** PASS
- **Measured (40.36s):** the evicted Mac's own credential answers HTTP 401 and the join is refused by name (HTTP 403: this controller removed this Mac on 2026-09-28; re-admit it from Connections to join again), its own view cannot read the group (roster_readable=False), its neighbour node-c still ran the work (['local', 'local', 'node-c', 'node-c']), and a deliberate re-admit let it join again ({'total': 2, 'reachable': 1, 'unreachable': 0, 'unauthorized': 1, 'error': 0, 'removed': 0})

_restart -> own credential HTTP 401; join with the Fleet token refused HTTP 403 (this controller removed this Mac on 2026-09-28; re-admit it from Connections to join again); neighbour job succeeded on ['local', 'local', 'node-c', 'node-c']; the new head accepted the evicted Mac: 201_

```json
{
  "restart": {
    "before": "http://127.0.0.1:50563",
    "after": "http://127.0.0.1:50563",
    "same_address": true
  },
  "stored_membership": {
    "version": 2,
    "role": "agent",
    "site_id": "s9-node-e",
    "site_name": "Node E",
    "controller_id": "s9-node-e",
    "database_mode": "off",
    "parent_controller_url": "http://127.0.0.1:50537",
    "fleet_credential_origin": "adopted",
    "handed_over_to": null,
    "handed_over_at": null,
    "machine_name": "studio-Mac-Studio",
    "database_configured": false,
    "database_endpoint": null,
    "database_source": null,
    "sqlite_authoritative": true,
    "global_job_claiming": false,
    "global_authority": "genstudio",
    "migration_stage": "local"
  },
  "join_attempt_own_credential": {
    "target": "controller-a",
    "status": 401,
    "payload": {
      "detail": "POST /api/hub/fleet/nodes was refused by studio-Mac-Studio (s9-controller-a): a credential that is neither this Mac's Hub token nor the Fleet token this Mac holds. The route requires this Mac's Hub token, or a Fleet token matching the one this Mac holds. If the caller presented a Fleet token, the two Macs hold different Fleet tokens: re-join this Mac from its controller with the current Fleet Token."
    }
  },
  "join_attempt": {
    "target": "controller-a",
    "status": 403,
    "payload": {
      "detail": "this controller removed this Mac on 2026-09-28; re-admit it from Connections to join again"
    }
  },
  "evictor_roster": {
    "total": 1,
    "reachable": 0,
    "unreachable": 0,
    "unauthorized": 1,
    "error": 0,
    "removed": 1
  },
  "evictor_removed_list": [
    "node-e"
  ],
  "own_view": {
    "roster_readable": false,
    "controller": {
      "machine": "controller",
      "label": "Controller",
      "role": "controller",
      "address": "http://127.0.0.1:50537",
      "state": "reachable",
      "detail": null,
      "version": "0.6.93",
      "host": {
        "chip": "Apple M2 Max",
        "machine_type": "Mac Studio",
        "total_gb": 34.36,
        "used_gb": 10.22,
        "available_gb": 22.06,
        "percent": 35.8,
        "cpu_percent": 0.0,
        "cpu_count": 12,
        "load_avg": [
          2.51,
          2.76,
          2.6
        ],
        "disk_free_gb": 126.43,
        "disk_total_gb": 494.38
      },
      "studios": []
    },
    "summary": {
      "total": 2,
      "reachable": 2,
      "unreachable": 0
    }
  },
  "enable_head_model": {
    "changed": 1,
    "enabled": true
  },
  "neighbour_job": {
    "submit_status": 202,
    "id": "2ed14e14-047c-4226-bf82-3f31e4468851",
    "state": "succeeded",
    "detail": null,
    "failure_reason": null,
    "item_watch": [
      {
        "index": 0,
        "state": "running",
        "machine": "local",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": null,
        "runtime_s": null,
        "hub": "node-b",
        "batch": "706915dedc",
        "at": 1790582034.8
      },
      {
        "index": 0,
        "state": "queued",
        "machine": "local",
        "worker_id": "image",
        "tries": 0,
        "error": "waiting for capacity: MemoryGuardError: not enough memory to load this model right now",
        "avoid_machines": {
          "local": 1790582126.6316218
        },
        "runtime_s": 2.01,
        "hub": "node-b",
        "batch": "706915dedc",
        "at": 1790582036.9
      },
      {
        "index": 0,
        "state": "running",
        "machine": "node-c",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": {
          "local": 1790582126.6316218
        },
        "runtime_s": null,
        "hub": "node-b",
        "batch": "706915dedc",
        "at": 1790582066.7
      },
      {
        "index": 0,
        "state": "done",
        "machine": "node-c",
        "worker_id": "image",
        "tries": 1,
        "error": null,
        "avoid_machines": {
          "local": 1790582126.6316218
        },
        "runtime_s": 2.02,
        "hub": "node-b",
        "batch": "706915dedc",
        "at": 1790582068.8
      }
    ]
  },
  "new_head_join_probe": {
    "target": "node-b",
    "status": 201,
    "payload": {
      "node": {
        "id": "node-e",
        "name": "Node E",
        "address": "http://127.0.0.1:50563",
        "role": "agent",
        "studios": [
          {
            "id": "image",
            "modality": "image"
          }
        ],
        "joined_at": 1790582069.310766,
        "updated_at": 1790582069.310766,
        "last_seen_at": 1790582069.310766,
        "credential_hash": "6caea40ae3c6ee2441260a745339ebe76dc800e3e0f936dc25bdfbef438c998c",
        "credential_issued_at": 1790582069.310766
      },
      "credential": "yJ1rP94xmCEDmNrXy7ILniCEnNqr26LQz3fymJflvn4"
    }
  },
  "new_head_cleanup": 200,
  "readmit": [
    200,
    {
      "ok": true,
      "readmitted": {
        "address": "http://127.0.0.1:50563",
        "id": "node-e",
        "joined_at": 1790581936.337899,
        "name": "Node E",
        "removed_at": 1790581954.1522508,
        "removed_by": "node-e",
        "role": "agent",
        "studios": [
          {
            "id": "image",
            "modality": "image"
          }
        ]
      }
    }
  ],
  "rejoin_after_readmit": {
    "join": {
      "node": {
        "id": "node-e",
        "name": "Node E",
        "address": "http://127.0.0.1:50563",
        "role": "agent",
        "studios": [
          {
            "id": "image",
            "modality": "image"
          }
        ],
        "joined_at": 1790582070.323375,
        "updated_at": 1790582070.323375,
        "last_seen_at": 1790582070.323375,
        "credential_hash": "4ab31817ffab45bdaf62d4938a86c3a5830dd7171a07142363475a37675e74fc",
        "credential_issued_at": 1790582070.323375
      },
      "credential": "Qi3T5aWzgMzExOuodta6FKM3mXh3lB6WliJ2otjOXe4"
    },
    "adopt": {
      "ok": true,
      "adopted": true,
      "controller_url": "http://127.0.0.1:50537"
    }
  },
  "roster_after_readmit": {
    "total": 2,
    "reachable": 1,
    "unreachable": 0,
    "unauthorized": 1,
    "error": 0,
    "removed": 0
  }
}
```

- **Reproduce:** `python3 run_rehearsal.py --only 9`

## 10. one cluster down does not hurt the others

- **Result:** PASS
- **Measured (67.2s):** with Controller C's Hub stopped the catalogue still offers the model (rows=1, legacy_ids=['studio-hub-2af7a7aa3992::rehearsal/image-1', 'studio-hub-11861ec47da8::rehearsal/image-1']), work was routed to the surviving site and succeeded, and the Gateway shows that cluster down (Nothing is listening on that address. If Studio Hub is running there, it is answering that Mac only — open Studio Hub, stop its services, tick its Tailscale network under "Listening on", and start them again. Confirm the port is 47873.)

_cluster down: reachable=False, node_summary={'total': 1, 'reachable': 1, 'unreachable': 0, 'unauthorized': 0, 'error': 0, 'removed': 0}, members_from=1790582069.295191, stale=38.040940046310425s; catalogue rows for the model 1; work routed: succeeded_

```json
{
  "before": {
    "location": {
      "id": "11861ec47da8",
      "label": "Controller C",
      "reachable": true,
      "node_summary": {
        "total": 1,
        "reachable": 1,
        "unreachable": 0,
        "unauthorized": 0,
        "error": 0,
        "removed": 0
      },
      "machine_summary": {
        "total": 2,
        "reachable": 2,
        "unreachable": 0
      },
      "nodes": [
        {
          "id": "node-f",
          "name": "Node F",
          "address": "http://127.0.0.1:50576",
          "role": "agent",
          "studios": [
            {
              "id": "image",
              "modality": "image"
            }
          ],
          "joined_at": 1790581936.345454,
          "last_seen_at": 1790582069.2185118,
          "updated_at": 1790581936.345454,
          "credential_issued_at": 1790581936.345454,
          "has_credential": true,
          "host": {
            "chip": "Apple M2 Max",
            "machine_type": "Mac Studio",
            "total_gb": 34.36,
            "used_gb": 10.19,
            "available_gb": 22.09,
            "percent": 35.7,
            "cpu_percent": 14.9,
            "cpu_count": 12,
            "load_avg": [
              2.05,
              2.62,
              2.55
            ],
            "disk_free_gb": 126.43,
            "disk_total_gb": 494.38
          },
          "version": "0.6.93",
          "state": "reachable",
          "detail": null
        }
      ],
      "members_from": null,
      "members_stale_seconds": null
    },
    "models": [
      "flux-2/flex-text-to-image",
      "flux-2/flex-image-to-image",
      "flux-2/pro-text-to-image",
      "flux-2/pro-image-to-image",
      "gemini-3-pro-image",
      "gemini-omni-video",
      "gpt-image-2-text-to-image",
      "gpt-image-2-image-to-image",
      "grok-imagine-video-1-5-preview",
      "happyhorse-1-1/text-to-video",
      "happyhorse-1-1/image-to-video",
      "kling/v3-turbo-text-to-video",
      "kling/v3-turbo-image-to-video",
      "nano-banana-2",
      "nano-banana-2-lite",
      "pixverse-v6/text-to-video",
      "qwen2/text-to-image",
      "bytedance/seedance-2-mini",
      "seedream/5-lite-text-to-image",
      "seedream/5-pro-text-to-image",
      "V6",
      "V6_MINI",
      "V6_WILD",
      "wan/2-7-image",
      "z-image",
      "flux-pro-1.1",
      "flux-dev",
      "flux-kontext-pro",
      "flux-pro",
      "image-01",
      "music-01",
      "speech-2.8-hd",
      "speech-2.8-turbo",
      "video-01",
      "s1",
      "s2-pro",
      "s2.1-pro",
      "s2.1-pro-free",
      "rehearsal/image-1"
    ]
  },
  "down_location": {
    "id": "11861ec47da8",
    "label": "Controller C",
    "reachable": false,
    "error": "Nothing is listening on that address. If Studio Hub is running there, it is answering that Mac only \u2014 open Studio Hub, stop its services, tick its Tailscale network under \"Listening on\", and start them again. Confirm the port is 47873.",
    "role": null,
    "version": null,
    "machines": [],
    "machine_summary": null,
    "nodes": [
      {
        "id": "node-f",
        "name": "Node F",
        "address": "http://127.0.0.1:50576",
        "role": "agent",
        "studios": [
          {
            "id": "image",
            "modality": "image"
          }
        ],
        "joined_at": 1790581936.345454,
        "last_seen_at": 1790582069.2185118,
        "updated_at": 1790581936.345454,
        "credential_issued_at": 1790581936.345454,
        "has_credential": true,
        "host": {
          "chip": "Apple M2 Max",
          "machine_type": "Mac Studio",
          "total_gb": 34.36,
          "used_gb": 10.19,
          "available_gb": 22.09,
          "percent": 35.7,
          "cpu_percent": 14.9,
          "cpu_count": 12,
          "load_avg": [
            2.05,
            2.62,
            2.55
          ],
          "disk_free_gb": 126.43,
          "disk_total_gb": 494.38
        },
        "version": "0.6.93",
        "state": "reachable",
        "detail": null
      }
    ],
    "node_summary": {
      "total": 1,
      "reachable": 1,
      "unreachable": 0,
      "unauthorized": 0,
      "error": 0,
      "removed": 0
    },
    "members_from": 1790582069.295191,
    "members_stale_seconds": 38.040940046310425,
    "activity": {
      "pulse": {},
      "machines": [],
      "observed_at": null
    },
    "latency_ms": null,
    "running_jobs": [],
    "local_models": []
  },
  "v1_models_immediately": {
    "rows": 1,
    "legacy_ids": [
      "studio-hub-2af7a7aa3992::rehearsal/image-1",
      "studio-hub-11861ec47da8::rehearsal/image-1"
    ],
    "availability": "available"
  },
  "v1_models_after": {
    "rows": 1,
    "legacy_ids": [
      "studio-hub-2af7a7aa3992::rehearsal/image-1",
      "studio-hub-11861ec47da8::rehearsal/image-1"
    ],
    "availability": "available"
  },
  "routed_job": {
    "submit_status": 202,
    "id": "543128a8-3c87-4548-b90d-a0c9f3da8a30",
    "state": "succeeded",
    "attempts": null
  },
  "restart": {
    "base": "http://127.0.0.1:50567"
  }
}
```

- **Reproduce:** `python3 run_rehearsal.py --only 10`

## 11. published capacity is the fleet's, live

- **Result:** PASS
- **Measured (24.93s):** the number Story is offered fell from 36 to 18 when two Macs stopped and rose back to 36 when they returned, with no settings edited between readings (source: the live fleet, summed over reachable locations)

_paid_concurrency_limit 36 -> 18 (two Macs stopped) -> 36 (brought back)_

```json
{
  "add_controller_d": {
    "controller": {
      "id": "58ab8658a4e4",
      "label": "Controller D",
      "url": "http://127.0.0.1:50580",
      "site_id": "s9-site-d",
      "token_set": true,
      "token_hint": "7w0M\u2026uU8r",
      "added_at": 1790582137.630343,
      "created": true
    },
    "credential": {
      "scope": "fleet",
      "detail": "A valid Fleet token \u2014 the work badge, and the credential this gateway runs work with."
    },
    "discovered": 1,
    "discovery_error": null,
    "enable_seed": {
      "provider_id": "studio-hub-58ab8658a4e4",
      "source_provider_id": "studio-hub-local",
      "models_discovered": 1,
      "enabled": 0,
      "disabled": 0,
      "no_decision": 1,
      "sentence": "0 of 1 models enabled here, from this Mac's Hub's own decisions; 0 are off there too, and 1 the local Hub has never seen stays off."
    },
    "controllers": [
      {
        "id": "2af7a7aa3992",
        "label": "Site A",
        "url": "http://127.0.0.1:50545",
        "site_id": null,
        "token_set": true,
        "token_hint": "rBzo\u2026HR2H",
        "added_at": 1790581936.615804
      },
      {
        "id": "11861ec47da8",
        "label": "Controller C",
        "url": "http://127.0.0.1:50567",
        "site_id": "s9-site-c",
        "token_set": true,
        "token_hint": "-dyc\u2026LLMK",
        "added_at": 1790581936.7207732
      },
      {
        "id": "58ab8658a4e4",
        "label": "Controller D",
        "url": "http://127.0.0.1:50580",
        "site_id": "s9-site-d",
        "token_set": true,
        "token_hint": "7w0M\u2026uU8r",
        "added_at": 1790582137.630343
      }
    ]
  },
  "first": {
    "paid_concurrency_limit": 36,
    "concurrency_source": "the live fleet, summed over reachable locations",
    "concurrency_by_modality": {
      "chat": 6,
      "image": 6,
      "video": 6,
      "tts": 6,
      "transcription": 6,
      "music": 6
    },
    "concurrency_pool": 4
  },
  "locations_before": [
    "Controller C",
    "Controller D",
    "Site A",
    "This Mac"
  ],
  "after_stopping_two": {
    "paid_concurrency_limit": 18,
    "concurrency_source": "the live fleet, summed over reachable locations",
    "concurrency_by_modality": {
      "chat": 3,
      "image": 3,
      "video": 3,
      "tts": 3,
      "transcription": 3,
      "music": 3
    }
  },
  "stopped": [
    "controller-c",
    "controller-d"
  ],
  "after_bringing_them_back": {
    "paid_concurrency_limit": 36,
    "concurrency_source": "the live fleet, summed over reachable locations",
    "concurrency_by_modality": {
      "chat": 6,
      "image": 6,
      "video": 6,
      "tts": 6,
      "transcription": 6,
      "music": 6
    }
  },
  "no_settings_edited": true
}
```

- **Reproduce:** `python3 run_rehearsal.py --only 11`

## 12. three attempts on three machines, no duplicates

- **Result:** PASS
- **Measured (118.58s):** two forced failures were re-run and attempt 3 succeeded on a third machine (failed at ['studio-hub-2af7a7aa3992', 'studio-hub-11861ec47da8'], ran at ['node-b', 'controller-c', 'controller-d']); three forced failures produced one terminal failure naming all three; a machine killed mid-run was not retried and is a recovery hold (unknown_outcome: lost contact with http://127.0.0.1:50580 while polling batch c370225512 (item 0): [Errno 61] Connection refused Upstream)

_attempt 3 succeeded after failures on ['studio-hub-2af7a7aa3992', 'studio-hub-11861ec47da8'] (1 job(s) sampled, batch appeared at ['node-b', 'controller-c', 'controller-d']); terminal failure named ['studio-hub-2af7a7aa3992', 'studio-hub-11861ec47da8', 'studio-hub-58ab8658a4e4']; unknown outcome -> unknown_outcome (retries: 0)_

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
    },
    {
      "id": "2af7a7aa3992",
      "label": "Site A",
      "reachable": true,
      "role": "controller",
      "machines": 3,
      "nodes": 2
    },
    {
      "id": "11861ec47da8",
      "label": "Controller C",
      "reachable": true,
      "role": "standalone",
      "machines": 2,
      "nodes": 1
    },
    {
      "id": "58ab8658a4e4",
      "label": "Controller D",
      "reachable": true,
      "role": "standalone",
      "machines": 1,
      "nodes": 0
    }
  ],
  "machines": {
    "head": {
      "provider": "studio-hub-2af7a7aa3992",
      "macs": [
        "node-b",
        "node-c"
      ]
    },
    "c": {
      "provider": "studio-hub-11861ec47da8",
      "macs": [
        "controller-c"
      ]
    },
    "d": {
      "provider": "studio-hub-58ab8658a4e4",
      "macs": [
        "controller-d"
      ]
    }
  },
  "enable_everywhere": {
    "changed": 3,
    "enabled": true
  },
  "third_time_lucky": {
    "runs": [
      {
        "submit_status": 202,
        "id": "059d17e8-fa58-4782-9c96-3b19ef9a7eb0",
        "state": "succeeded",
        "attempts": [
          {
            "number": 1,
            "machine": "studio-hub-2af7a7aa3992",
            "failure_code": "PROVIDER_FAILED",
            "failure_reason": "hub item error: the sampler diverged while denoising",
            "at": 1790582186.523836
          },
          {
            "number": 2,
            "machine": "studio-hub-11861ec47da8",
            "failure_code": "PROVIDER_FAILED",
            "failure_reason": "hub item error: the engine ran out of unified memory",
            "at": 1790582207.810293
          }
        ],
        "attempts_used": 2,
        "failure_code": null,
        "failure_reason": ""
      }
    ],
    "chosen": {
      "submit_status": 202,
      "id": "059d17e8-fa58-4782-9c96-3b19ef9a7eb0",
      "state": "succeeded",
      "attempts": [
        {
          "number": 1,
          "machine": "studio-hub-2af7a7aa3992",
          "failure_code": "PROVIDER_FAILED",
          "failure_reason": "hub item error: the sampler diverged while denoising",
          "at": 1790582186.523836
        },
        {
          "number": 2,
          "machine": "studio-hub-11861ec47da8",
          "failure_code": "PROVIDER_FAILED",
          "failure_reason": "hub item error: the engine ran out of unified memory",
          "at": 1790582207.810293
        }
      ],
      "attempts_used": 2,
      "failure_code": null,
      "failure_reason": ""
    },
    "new_batches": {
      "node-b": [
        "da323cfe6c"
      ],
      "controller-c": [
        "c97a5efff7"
      ],
      "controller-d": [
        "2ae9079979"
      ]
    }
  },
  "all_three_fail": {
    "submit_status": 202,
    "id": "c87c7be9-9d23-48eb-b7bf-a8aefcbb1a48",
    "state": "failed",
    "failure_code": "PROVIDER_FAILED",
    "failure_reason": "tried 3 machines and every one failed - studio-hub-2af7a7aa3992 (PROVIDER_FAILED: hub item error: the sampler diverged while denoising); studio-hub-11861ec47da8 (PROVIDER_FAILED: hub item error: the engine ran out of unified memory); studio-hub-58ab8658a4e4 (PROVIDER_FAILED: hub item error: the sampler diverged while denoising). This is the end of the retry policy, not a queue wait.",
    "attempts": [
      {
        "number": 1,
        "machine": "studio-hub-2af7a7aa3992",
        "failure_code": "PROVIDER_FAILED",
        "failure_reason": "hub item error: the sampler diverged while denoising",
        "at": 1790582232.19354
      },
      {
        "number": 2,
        "machine": "studio-hub-11861ec47da8",
        "failure_code": "PROVIDER_FAILED",
        "failure_reason": "hub item error: the engine ran out of unified memory",
        "at": 1790582253.4576168
      },
      {
        "number": 3,
        "machine": "studio-hub-58ab8658a4e4",
        "failure_code": "PROVIDER_FAILED",
        "failure_reason": "hub item error: the sampler diverged while denoising",
        "at": 1790582274.7250938
      }
    ],
    "attempts_used": 3,
    "attempts_allowed": 3
  },
  "duplicates": {
    "same_idempotency_key_job": "c87c7be9-9d23-48eb-b7bf-a8aefcbb1a48",
    "terminal_job": "c87c7be9-9d23-48eb-b7bf-a8aefcbb1a48",
    "one_job_for_the_key": true,
    "attempts_len": 3
  },
  "retry_exclusion_probe": {
    "excluded_by_provider_id": [
      "11861ec47da8",
      "2af7a7aa3992",
      "58ab8658a4e4"
    ],
    "excluded_by_controller_id": [
      "11861ec47da8",
      "2af7a7aa3992"
    ],
    "asked_to_exclude": "58ab8658a4e4",
    "provider_id_spelling_excludes": false,
    "controller_id_spelling_excludes": true
  },
  "accepted_before_the_kill": {
    "state": "running",
    "upstream_job_id": "c370225512"
  },
  "unknown_outcome": {
    "state": "failed",
    "failure_code": "PROVIDER_SUBMISSION_UNCERTAIN",
    "recovery_required": true,
    "recovery_state": "unknown_outcome",
    "recovery_detail": "lost contact with http://127.0.0.1:50580 while polling batch c370225512 (item 0): [Errno 61] Connection refused Upstream outcome is unknown; reconcile the provider request before retrying; the gateway will not resubmit it automatically. {\"batch_id\": \"c370225512\", \"item_index\": 0, \"jobs_path\": \"/api/hub/jobs\", \"location\": \"http://127.0.0.1:50580\", \"reason\": \"[Errno 61] Connection refused\"}",
    "retryable": false,
    "attempts": null,
    "attempts_used": null
  }
}
```

- **Reproduce:** `python3 run_rehearsal.py --only 12`

## What this rehearsal did not exercise

- **The node app's own re-join loop.** The Mac's automatic re-join after a handover or a roster change is native behaviour; the rehearsal performs the same HTTP request the app would and labels it `rejoin_emulated`.
- **The SwiftUI pages themselves.** Every 'UI' reading here is the API payload the page renders (Fleet row, Models row, queue view), not a screenshot.
- **Real model inference.** Each Mac runs a rehearsal worker stub that answers the Hub's real worker contract (`/api/health`, `/api/catalog` with `cache.state: cached`, `/api/generate/txt2img`, `/api/generate/jobs/<id>`, `/ack`); the Hub, the Gateway, the catalogue, routing, admission, the queue and the retry policy are all the real thing.
- **Story Studio** — out of scope for this rehearsal; the Story surface is exercised only as the `/v1/...` contract it reads.

## Builds measured

- **Studio Hub**: version `0.6.93`, revision `HEAD = 94d5bfd` (source root `/private/var/folders/p8/wwyv7rc96s51h1b1rrx6scmc0000gn/T/s9-rehearsal-3149t3zc/src/studio-hub`)
- **GatewayKH**: build `89a1880-s9` as published on `/health`, revision `HEAD = 89a1880` (source root `/private/var/folders/p8/wwyv7rc96s51h1b1rrx6scmc0000gn/T/s9-rehearsal-3149t3zc/src/gateway-kh`)

**The gateway revision above is `89a1880`, which is the 0.2.65 code** — the run was made after the
id-spelling fix (0.2.65) and before the What's New page (0.2.66), and the fleet behaviour the twelve
checks exercise is identical in both. The two commits between them are the receipt you are reading
and a UI page, neither of which touches routing, admission, the queue or the retry policy.

**This run is the first with all twelve passing.** The run before it failed check 12 and found the
id-spelling defect A7 had reintroduced in its own code; that fix is `474b1e9`, and its addendum is
below. The receipt is kept whole rather than tidied: the run that failed is the evidence that the
step earns its place.

