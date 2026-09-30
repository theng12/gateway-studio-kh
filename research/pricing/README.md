# Price & rate-limit research (for a research session)

You are a research agent. Task: find the OFFICIAL published prices and API rate limits for the AI models listed below, and deliver them as one JSON evidence file. This is research only — do not write application code, do not call any of these providers' APIs, do not sign up for anything.

## Ground rules (these decide whether your numbers are usable)
1. Only use the provider's OWN website/docs (allowed domains per provider are listed below; subdomains are fine). Never use third-party aggregators, blogs, news, Reddit, or comparison sites — not even to "confirm". If the official site doesn't state it, report it as not found.
2. Every number you report must come with the exact `source_url` you read it on and a `quote`: a short VERBATIM excerpt from that page that contains the number exactly as written (e.g. "$0.040 / image"). Numbers without a verbatim quote containing them will be rejected automatically.
3. Never estimate, derive, average, or convert. No "approximately". If a price is in credits, characters-per-dollar, megapixels, or per-minute, record it with `"unit": "other"`, `"unit_text"` verbatim, and `"usd": null` — unless the same official page states the credit→USD rate, in which case also include `"conversion_quote"`.
4. Prices often vary by size/quality/resolution/context length/tier: record each variant as its own entry with `"variant"` describing it. Don't pick one.
5. Model names change fast. Match the model ids below exactly; if the provider's page names it differently, use our `model_id` and mention the page's name in `variant` or `notes`. If you can't find a model at all, say so in `notes` and set the provider `status` to `partial` or `not_found`.
6. Rate limits: record the documented tiers (name, how a tier is reached, and RPM / TPM / RPD / concurrency / images-per-minute as stated), with source_url + quote. These are "up to" ceilings — record exactly what's documented.
7. Record `page_date` if the page shows a "last updated" date.
8. Be efficient: one pass per provider; stop searching a provider once you have its pricing page and rate-limit page.

## Models to research (provider_id → allowed domains → our model ids, grouped by modality)

- **anthropic-direct** — anthropic.com, claude.com — chat: `claude-haiku-4-5-20251001`, `claude-opus-5-5`, `claude-sonnet-5`
- **deepseek-main** — deepseek.com — chat: `deepseek-flash`, `deepseek-v4-pro`
- **elevenlabs-direct** — elevenlabs.io — tts: `eleven_flash_v2_5`, `eleven_turbo_v2_5` (API pricing, not consumer subscription plans; include concurrency limits per plan)
- **experiential-labs** — experientiallabs.ai — chat: `deepseek-v4-flash-latest`, `glm-5.3-flash`, `gpt-6-luna`, `mimo-v2.6-pro`, `space-bunny-alpha`
- **fish-audio** — fish.audio — tts: `s2.1-pro`, `s2.1-pro-free`
- **google-ai** and **google-ai-free** (Gemini API, paid tier vs free tier — report both) — ai.google.dev, cloud.google.com, google.com — chat: `gemini-2.5-flash-lite`, `gemini-3-flash-preview`, `gemini-3.1-flash-lite`, `gemini-3.5-flash`, `gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.7-flash`, `gemini-3.8-flash`, `gemini-flash-latest`, `gemini-flash-lite-latest`, `gemma-4-26b-a4b-it`, `gemma-4-31b-it` (Gemini API pricing page + rate-limits page, per tier: Free / Tier 1 / 2 / 3)
- **kie-ai-images** — kie.ai — music: `V6` (Suno-style); image: `nano-banana-2-lite`; video: `gemini-omni-video`
- **nvidia-nim-main** — nvidia.com — chat: `deepseek-ai/deepseek-v4.1-flash`, `z-ai/glm-5.3-flash` (build.nvidia.com API catalog: free credits / rate limits)
- **openai-direct** — openai.com — image: `gpt-image-2.5-flare-2026-09-08`, `gpt-image-2.5-sunburst-2026-09-08`; chat: `gpt-6-luna`
- **fal-ai** — fal.ai — tts: `fal-ai/elevenlabs/tts/turbo-v2.5`; image: `google/nano-banana-2-lite`, `openai/gpt-image-2.5/flare/text-to-image`, `openai/gpt-image-2.5/sunburst/text-to-image`; video: `bytedance/seedance-2.5/image-to-video`, `bytedance/seedance-2.5/text-to-video`, `google/gemini-omni-flash/v1.1/image-to-video`, `google/gemini-omni-flash/v1.1/text-to-video`, `minimax/h3-max/image-to-video`, `minimax/h3-max/text-to-video` (each model's fal.ai page states its price; also fal's concurrency limits)
- **xai-direct** — x.ai — image: `grok-imagine-image`, `grok-imagine-image-2.0`, `grok-imagine-image-quality`; video: `grok-imagine-video`, `grok-imagine-video-1.5` (docs.x.ai models/pricing + rate limits)

## Units (use exactly these keys)
chat: `input_per_1m_tokens`, `output_per_1m_tokens`, `cached_input_per_1m_tokens` (USD per 1,000,000 tokens — if the page states per 1K tokens, record `unit: "other"` with `unit_text`, do not convert) · image: `per_image` · video: `per_video` or `per_second` · tts: `per_1k_chars` · transcription: `per_second` · music: `per_request` · anything else: `other` + `unit_text`.

## Output
Copy `research/pricing/TEMPLATE.json` to `research/pricing/price-evidence-<YYYY-MM-DD>.json` and fill it in, keeping exactly this shape:

```json
{
  "schema": "gateway-kh.price-evidence.v1",
  "researched_at": "<ISO-8601 UTC now>",
  "providers": [
    {
      "provider_id": "<from the list>",
      "official_domains_used": ["..."],
      "status": "found | partial | not_found",
      "notes": "<short: what was missing, name mismatches, caveats>",
      "prices": [
        {"model_id": "<exact id from the list>", "modality": "chat|image|video|tts|transcription|music",
         "unit": "<unit key or other>", "unit_text": null, "usd": 0.0, "variant": null,
         "source_url": "https://...", "quote": "<verbatim, contains the number>", "page_date": null,
         "conversion_quote": null}
      ],
      "rate_limits": [
        {"tier": "<name>", "how_reached": null, "scope": "per model|per key|per org|per account",
         "rpm": null, "tpm": null, "rpd": null, "concurrency": null, "images_per_min": null,
         "source_url": "https://...", "quote": "<verbatim, contains the numbers>"}
      ]
    }
  ]
}
```
Include every provider in the list (use `not_found` with notes rather than omitting one). Valid JSON only in the file (no comments).

## Check, commit, push
- If `gateway/price_evidence.py` exists in this repo, run `python3 -m gateway.price_evidence check research/pricing/price-evidence-<YYYY-MM-DD>.json` and fix anything it rejects (it applies the same rules the gateway uses on import).
- Commit ONLY the new evidence file (no other file in this repo may change) on a new branch `research/pricing-<YYYY-MM-DD>` and push that branch. Do not open a pull request and do not touch `main`.

## Final message
After the JSON, give a short table: provider | models priced / total | rate-limit tiers found | anything suspicious or missing. Keep it brief.
