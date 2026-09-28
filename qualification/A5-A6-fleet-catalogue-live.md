# A5 + A6 receipt — what the fleet offers, and the geometry the Macs actually render

Gateway **0.2.62**, build `0eb3a58-202609280436`, installed and running on 8787.
Read live on 2026-09-28. `[MEASURED]`

## A6 — geometry (audit H3)

`GET /v1/image/models`, `studio-hub::` rows. The **Hub's own lane contract** decides the
ratios and the pixels; the cloud table is untouched.

| row | ratios | 16:9 | 16:9 small | 4:3 | 1:1 | 9:16 | available | lock_reason |
|---|---|---|---|---|---|---|---|---|

| `studio-hub::AITRADER/FLUX2-klein-4B-mlx-4bit` | 13 | 1280x720 | 1024x576 | 1152x864 | 1024x1024 | 720x1280 | True | None |
| `studio-hub::filipstrand/Z-Image-Turbo-mflux-4bit` | 13 | 1280x720 | 1024x576 | 1152x864 | 1024x1024 | 720x1280 | True | None |

**Before:** 16:9 was published as 1536x1024 and 4:3 as 1536x1024, with 5 ratios. **After:** 16:9 is **1280x720**, `16:9 small` is **1024x576** (the video-valid frame the Image studio added on purpose), 4:3 is **1152x864**, and the row carries all **13** ratios the Hub accepts.

The cloud lanes are unchanged, which is the other half of the proof — a `studio-hub` contract must not leak onto a vendor:

| cloud row | 16:9 | 4:3 | ratios |
|---|---|---|---|
| `openai-direct::gpt-image-2.5-flare-2026-09-08` | 1536x1024 | 1536x1024 | 5 |
| `openai-direct::gpt-image-2.5-sunburst-2026-09-08` | 1536x1024 | 1536x1024 | 5 |
| `xai-direct::grok-imagine-image` | 1536x1024 | 1536x1024 | 5 |

## A5 — one row per model, "N of M machines" (audit H1, H2)

`GET /admin/api/models?provider_id=studio-hub-local`. **13 rows for 13 Hub models** (the audit
measured 108 rows for 21 models), and **11 of the 13** now name more than one machine where
only 5 did before.

| model | machines enabled | enabled |
|---|---|---|

| `AITRADER/FLUX2-klein-4B-mlx-4bit` | 3 of 6 | True |
| `khmerttsopensource/khmer-tts` | 1 of 6 | True |
| `sengtha/khmer-tts-female-v2` | 1 of 6 | True |
| `baa-ai/LTX-2.3-22B-RAM-12GB-MLX` | 1 of 1 | True |
| `vanch007/mlx-Yue2-3B` | 1 of 1 | True |
| `mlx-community/pocket-tts-4bit` | 1 of 6 | True |
| `mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit` | 3 of 6 | True |
| `mlx-community/Qwen3.5-9B-4bit` | 6 of 6 | True |
| `stabilityai/stable-audio-3-optimized` | 1 of 6 | True |
| `mlx-community/VoxCPM2-8bit` | 3 of 6 | True |
| `mlx-community/whisper-large-v3-turbo` | 1 of 6 | True |
| `mlx-community/whisper-small-mlx` | 1 of 6 | True |
| `filipstrand/Z-Image-Turbo-mflux-4bit` | 6 of 6 | True |

One row in full — `AITRADER/FLUX2-klein-4B-mlx-4bit`:

```json
{
 "model_id": "AITRADER/FLUX2-klein-4B-mlx-4bit",
 "is_location": true,
 "locations_enabled": 3,
 "locations_total": 6,
 "enabled": true,
 "provider_ids": [
  "studio-hub-local",
  "studio-hub-de7250e146a7",
  "studio-hub-9701220bf30f",
  "studio-hub-a311a0a9f4f7",
  "studio-hub-eda08bae6325",
  "studio-hub-b8ccda585b97"
 ]
}
```

| machine | enabled | modality |
|---|---|---|
| Studio Hub (this Mac) | True | image |
| terranash-1100-m4-16-256 | True | image |
| terranash-1200-m4-24-256 | True | image |
| terranash-1300-m4-24-256 | False | image |
| terranash-1400-m4-24-256 | False | image |
| terranash-1500-m4-24-256 | False | image |

**A new location now inherits this Mac's Hub's own decisions for the same models** (`enable_seed.sentence` on the add result reports the real counts), so the operator ticks a model once for the fleet instead of once per Mac. Existing locations keep their own per-Mac choices, and the model row's tick is the one-click remedy.

