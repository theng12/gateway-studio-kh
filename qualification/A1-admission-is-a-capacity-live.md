# A1 receipt — admission is a capacity (live read)

Gateway **0.2.61**, build `230d552-202609280401`, installed and running on 8787.
`GET /admin/api/fleet` on 2026-09-28. `[MEASURED]`

The Hub's own free count sits beside the budget admission computed from it. A site with
one free physical Mac and nothing of ours running admits one job; a location that is not
reachable is given **zero**, which is what routing already assumed (audit M2).

| Location | reachable | `available_worker_slots` | `available_physical_machine_slots` | `eligible_physical_machine_slots_total` | computed budget | source | in flight |
|---|---|---|---|---|---|---|---|
| This Mac | True | 1 | 1 | 1 | **1** | hub | 0 |
| This Mac | True | 1 | 1 | 1 | **1** | hub | 0 |
| This Mac | True | 2 | 1 | 1 | **1** | hub | 0 |
| studio-1100-m4-16-256 | True | 1 | 1 | 1 | **1** | hub | 0 |
| studio-1100-m4-16-256 | True | 1 | 1 | 1 | **1** | hub | 0 |
| studio-1100-m4-16-256 | True | 2 | 1 | 1 | **1** | hub | 0 |
| studio-1200-m4-24-256 | True | 1 | 1 | 1 | **1** | hub | 0 |
| studio-1200-m4-24-256 | True | 1 | 1 | 1 | **1** | hub | 0 |
| studio-1200-m4-24-256 | True | 2 | 1 | 1 | **1** | hub | 0 |
| studio-1300-m4-24-256 | True | 1 | 1 | 1 | **1** | hub | 0 |
| studio-1300-m4-24-256 | True | 1 | 1 | 1 | **1** | hub | 0 |
| studio-1300-m4-24-256 | True | 2 | 1 | 1 | **1** | hub | 0 |
| studio-1400-m4-24-256 | False | None | None | None | **0** | hub | 0 |
| studio-1400-m4-24-256 | False | None | None | None | **0** | hub | 0 |
| studio-1400-m4-24-256 | False | None | None | None | **0** | hub | 0 |
| studio-1500-m4-24-256 | False | None | None | None | **0** | hub | 0 |
| studio-1500-m4-24-256 | False | None | None | None | **0** | hub | 0 |
| studio-1500-m4-24-256 | False | None | None | None | **0** | hub | 0 |

Raw rows, per location, for the lanes above:

```json
{
 "admission": {
  "chat": {
   "detail": "the site reports 1 available worker slot for chat.completion.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "chat.completion",
   "source": "hub"
  },
  "image": {
   "detail": "the site reports 1 available worker slot for image.text_to_image.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "image.text_to_image",
   "source": "hub"
  },
  "music": {
   "detail": "the site reports 1 available worker slot for music.generation.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "music.generation",
   "source": "hub"
  },
  "transcription": {
   "detail": "the site reports 1 available worker slot for audio.transcription.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "audio.transcription",
   "source": "hub"
  },
  "tts": {
   "detail": "the site reports 1 available worker slot for voice.tts.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "voice.tts",
   "source": "hub"
  },
  "video": {
   "detail": "the site reports 1 available worker slot for video.generation.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "video.generation",
   "source": "hub"
  }
 },
 "label": "This Mac",
 "reachable": true
}
{
 "admission": {
  "chat": {
   "detail": "the site reports 1 available worker slot for chat.completion.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "chat.completion",
   "source": "hub"
  },
  "image": {
   "detail": "the site reports 1 available worker slot for image.text_to_image.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "image.text_to_image",
   "source": "hub"
  },
  "music": {
   "detail": "the site reports 1 available worker slot for music.generation.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "music.generation",
   "source": "hub"
  },
  "transcription": {
   "detail": "the site reports 1 available worker slot for audio.transcription.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "audio.transcription",
   "source": "hub"
  },
  "tts": {
   "detail": "the site reports 1 available worker slot for voice.tts.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "voice.tts",
   "source": "hub"
  },
  "video": {
   "detail": "the site reports 1 available worker slot for video.generation.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "video.generation",
   "source": "hub"
  }
 },
 "label": "studio-1100-m4-16-256",
 "reachable": true
}
{
 "admission": {
  "chat": {
   "detail": "the site reports 1 available worker slot for chat.completion.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "chat.completion",
   "source": "hub"
  },
  "image": {
   "detail": "the site reports 1 available worker slot for image.text_to_image.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "image.text_to_image",
   "source": "hub"
  },
  "music": {
   "detail": "the site reports 1 available worker slot for music.generation.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "music.generation",
   "source": "hub"
  },
  "transcription": {
   "detail": "the site reports 1 available worker slot for audio.transcription.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "audio.transcription",
   "source": "hub"
  },
  "tts": {
   "detail": "the site reports 1 available worker slot for voice.tts.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "voice.tts",
   "source": "hub"
  },
  "video": {
   "detail": "the site reports 1 available worker slot for video.generation.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "video.generation",
   "source": "hub"
  }
 },
 "label": "studio-1200-m4-24-256",
 "reachable": true
}
{
 "admission": {
  "chat": {
   "detail": "the site reports 1 available worker slot for chat.completion.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "chat.completion",
   "source": "hub"
  },
  "image": {
   "detail": "the site reports 1 available worker slot for image.text_to_image.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "image.text_to_image",
   "source": "hub"
  },
  "music": {
   "detail": "the site reports 1 available worker slot for music.generation.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "music.generation",
   "source": "hub"
  },
  "transcription": {
   "detail": "the site reports 1 available worker slot for audio.transcription.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "audio.transcription",
   "source": "hub"
  },
  "tts": {
   "detail": "the site reports 1 available worker slot for voice.tts.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "voice.tts",
   "source": "hub"
  },
  "video": {
   "detail": "the site reports 1 available worker slot for video.generation.",
   "hub_free": 1,
   "hub_total": 1,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 1,
   "operation": "video.generation",
   "source": "hub"
  }
 },
 "label": "studio-1300-m4-24-256",
 "reachable": true
}
{
 "admission": {
  "chat": {
   "detail": "the site is not reachable, so this gateway claims no admission budget for it; routing skips it for the same reason",
   "hub_free": null,
   "hub_total": null,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 0,
   "operation": "chat.completion",
   "source": "hub"
  },
  "image": {
   "detail": "the site is not reachable, so this gateway claims no admission budget for it; routing skips it for the same reason",
   "hub_free": null,
   "hub_total": null,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 0,
   "operation": "image.text_to_image",
   "source": "hub"
  },
  "music": {
   "detail": "the site is not reachable, so this gateway claims no admission budget for it; routing skips it for the same reason",
   "hub_free": null,
   "hub_total": null,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 0,
   "operation": "music.generation",
   "source": "hub"
  },
  "transcription": {
   "detail": "the site is not reachable, so this gateway claims no admission budget for it; routing skips it for the same reason",
   "hub_free": null,
   "hub_total": null,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 0,
   "operation": "audio.transcription",
   "source": "hub"
  },
  "tts": {
   "detail": "the site is not reachable, so this gateway claims no admission budget for it; routing skips it for the same reason",
   "hub_free": null,
   "hub_total": null,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 0,
   "operation": "voice.tts",
   "source": "hub"
  },
  "video": {
   "detail": "the site is not reachable, so this gateway claims no admission budget for it; routing skips it for the same reason",
   "hub_free": null,
   "hub_total": null,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 0,
   "operation": "video.generation",
   "source": "hub"
  }
 },
 "label": "studio-1400-m4-24-256",
 "reachable": false
}
{
 "admission": {
  "chat": {
   "detail": "the site is not reachable, so this gateway claims no admission budget for it; routing skips it for the same reason",
   "hub_free": null,
   "hub_total": null,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 0,
   "operation": "chat.completion",
   "source": "hub"
  },
  "image": {
   "detail": "the site is not reachable, so this gateway claims no admission budget for it; routing skips it for the same reason",
   "hub_free": null,
   "hub_total": null,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 0,
   "operation": "image.text_to_image",
   "source": "hub"
  },
  "music": {
   "detail": "the site is not reachable, so this gateway claims no admission budget for it; routing skips it for the same reason",
   "hub_free": null,
   "hub_total": null,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 0,
   "operation": "music.generation",
   "source": "hub"
  },
  "transcription": {
   "detail": "the site is not reachable, so this gateway claims no admission budget for it; routing skips it for the same reason",
   "hub_free": null,
   "hub_total": null,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 0,
   "operation": "audio.transcription",
   "source": "hub"
  },
  "tts": {
   "detail": "the site is not reachable, so this gateway claims no admission budget for it; routing skips it for the same reason",
   "hub_free": null,
   "hub_total": null,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 0,
   "operation": "voice.tts",
   "source": "hub"
  },
  "video": {
   "detail": "the site is not reachable, so this gateway claims no admission budget for it; routing skips it for the same reason",
   "hub_free": null,
   "hub_total": null,
   "hub_unit": "physical_machine",
   "in_flight": 0,
   "limit": 0,
   "operation": "video.generation",
   "source": "hub"
  }
 },
 "label": "studio-1500-m4-24-256",
 "reachable": false
}
```

