# Locations are providers — but not separate products

Moved from `AGENTS.md` on 2026-11-08 (owner: "Trim everything"). `AGENTS.md` keeps the rules; this file keeps the incidents and measurements behind them.

A Studio Hub location is a provider: its models are discovered and dispatched
through the same `studio_hub` adapter. **Standalone and controller both work** —
the role only displays. Story Studio must never learn a second address: the
gateway alone knows the locations.

**But a location is not a product, so its models publish under one namespace.**
`fleet.publish_id` collapses every location into `studio-hub`, `catalog.rebuild`
merges the rows, so N locations offering the same model are **one** catalogue
entry with N possible sites — not N entries sharing one `display_name`.
Namespace by **maker**; a cloud vendor keeps its id, because two vendors
offering the same model *are* different products. The old GenStudio agreed, and
one of its migrations is named `0338_kokoro_names_the_maker_not_the_machines`.

Routing follows the split Studio Hub declares in `/api/hub/capabilities`
(`studio-hub-local`):

`authority: {global: "genstudio", site_local_scheduler: "sqlite",
global_job_claiming: false}`:

- **The gateway chooses the site, never the machine.** `fleet.route` ranks
  candidates by pressure (`queue / eligible fitting machines`), then a weighted
  lottery weighted by that count; which machine has the memory for a model
  belongs to the controller.

- **The weight is per model, not per site.** The Hub publishes `model_supply`
  (per model) and `capacity.by_operation` (per operation); `routing_candidates`
  reads the exact model's eligible machine count first, then the operation's, and
  falls back to the site-wide total only for a Hub that publishes neither. A site
  with ten 8 GB image Macs and one 24 GB Mac must not out-weigh a site with two
  24 GB Macs for a 24 GB model. `available` (ready with a free slot) breaks a
  pressure tie. Speed is **not** a routing input (the chip proxy and
  `GATEWAY_PREFER_FASTEST_SITE` were removed in 0.2.121: no gain in simulation);
  measured speed per model × Mac class is on Analytics, for ETAs and planning.
- **Admission is fit-aware (D1).** A group takes a job only while
  `min(operation room, slots(model) − our jobs there whose model fits no more Macs)`
  is above zero (`Worker._room`), so heavy jobs no longer fill a group's whole voice
  budget while its 8 GB Macs idle. Fleet lanes (tts/image/music/transcription) are
  automatic — they follow the fleet's published capacity — unless the owner types a
  ceiling, and fleet-lane work is not counted against `global` (D3).

- **A controller that cannot serve a model is a site property, not a job
  failure.** `can_publish` withholds a model whose `dispatch_state` is present and
  not `eligible` (absence stays unverified), so the picker never offers what the
  Hub will refuse. When the Hub still answers `409 MODEL_NOT_READY` /
  `MODEL_NOT_PACKAGED` at submit, the adapter names it `LOCATION_NOT_READY` and
  the worker re-routes to the next location that offers the same model. A voice is
  never re-routed — its reference clip lives on one controller.

- **Capacity belongs in the weight, never as a rank term.** Ported from the old
  `MultiSiteAssignment`: raw size as a rank made a small site unreachable, the
  same size ignored overloaded the small sites.

- **A voice pins the route.** A voice id names its owner
  (`studio-hub-<controller>::<raw>`), and a reference clip exists on one Hub, so
  voice work stays at that location.

- **The id Story Studio sent is the id it is told back.** The job stores the
  location it was routed to, but `catalog_encode` applies `publish_id` before
  answering, or a saved preset stops matching its jobs. Asserted end to end in
  `test_fleet_standalone.py`.

Secrets live in the credential store (`0600`); `fleet.json` holds the roster.
Lookups of "the providers" must go through `registry.definitions()`, not the
static `PROVIDERS_BY_ID` map: four bugs came from iterating it.

**A location's address comes from its *definition*, and one strategy forgot
that.** Every strategy resolves its base as `values.get("base_url") or
definition.base_url`. `_studio_hub` went from the credential values straight to
`hub_base_url()`, whose fallback is **this Mac's Hub**. A controller
declares no `base_url`, so every location discovered the *local* models; only
when pointed at the mock did the test surface "Studio Hub is not running (no
hub-ready.json found)" and zero models, which `tests/test_e2e.py`'s section 20
asserts against.

**Adding a location must also discover its models, and say what it found.**
Nothing refreshes them on a timer (`fleet.start_refresher` refreshes
*reachability*, not models), so a location added without a refresh shows none.
Models arrive **disabled**, as everything remote must, so the Fleet page says
how many were found and that none are available until enabled.

**A credential is checked, because both credentials work everywhere.**
`/api/hub/settings` answers 200 to the operator token, 403 to a fleet
credential, **401 to a wrong or absent one** (measured against Hub 0.5.1 — a
rejected token must never be reported as the fleet credential).
`PUT /api/fleet/controllers/{id}/credential` replaces a token without touching
the name, address or models.

**An absent credential stays absent — the operator token belongs to *this Mac's*
Hub and to no other address.** `hub_headers(token)` was `token or hub_token()`,
so a controller with no stored credential had **this Mac's operator token** put
in `X-Hub-Token` and sent to whatever address the owner typed. That token is
full operator scope: it can change this Hub's settings and identity, and reach
its storage. It is the mistake `credential_scope` exists to stop the owner, and
silent because a remote Hub that accepts it works. `token` is a **required**
argument and the fallback is reachable through `local=True`, derived by callers
from the **provider id** (`definition.id == provider_id_for(LOCAL_ID)`), never
the address, the one part an owner supplies. Local-only paths call
`local_hub_headers()`, so a future call site cannot inherit a credential by
omission: it fails with the wrong or absent token rather than with this Mac's.
`tests/mock_upstream.py` records every `X-Hub-Token` it receives, so the
assertion is about the **wire**, not a helper's return value.

**A custom provider's kind is checked against `IMPLEMENTED_KINDS` itself, not a
copy.** `_VALID_KINDS` was a second set, behind (`KIND_XAI` was missing), so
`"kind": "xai"` in `providers.local.json` was dropped without a word. Being
skipped is recorded with a reason, logged, and published as
`skipped_custom_providers` on `/admin/api/providers`, **which the console
renders** — a field nothing draws is the same defect as a control that does
nothing.

**A location's name is a label, never an identity, and renaming must stay safe
at any time.** `fleet.rename_controller` changes only the label — id, address,
token and enabled state untouched — so renaming can never cost a re-entry of a
credential. The provider id derives from the **controller id**
(`studio-hub-<controller-id>`) and the model id from the provider, so never
derive an id, key or path from a label. `fleet.json` also carries `local_label`,
which a write that adds or removes a controller must preserve.

Row actions are worth testing in the UI, not just the API: the Fleet page had
`testController` and `removeController` **wired to nothing** — no button ever
called them — so Test and Remove were unreachable from the app while the
endpoints worked. `tests/test_ui.py` now reads the buttons themselves
(`table_button_titles`); `table_texts` sees static text only.
