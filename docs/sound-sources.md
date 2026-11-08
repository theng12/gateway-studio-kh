# Sound sources are managed like providers, and are not providers

Moved from `AGENTS.md` on 2026-11-08 (owner: "Trim everything"). `AGENTS.md` keeps the rules; this file keeps the incidents and measurements behind them.

The owner, 2026-10-25: *"make it a 'provider' but have it in a new tab called SFX or something. This
way we can manage them easier. And also make sure all the providers are organised properly too."*
Both halves are display: neither changes what can run.

- **A sound source contributes no catalogue row, so it has no entry in
  `providers/definitions.py`.** Adding one there failed `test_e2e`'s "every provider has a working
  adapter" — the gate working, not a reason to relax it. Sources are declared once in
  `sound_sources.SOUND_SOURCE_DEFINITIONS` (id, adapter `kind`, credential fields, where a key comes
  from, which field is actually sent) with one class per kind in `_ADAPTERS`; the two are checked
  against each other at import, so a declaration with no adapter cannot be listed and an adapter
  nothing declares cannot be reached. A second source is one entry plus one class.
- **The SFX tab is a pane of the Providers page**, which is the same page in a browser and in the
  app's own window (`ConsoleSection` loads `#providers`), so it needs no native work. `#sfx` opens it.
- **Credentials go in the same `CredentialStore` as every provider key**, under the source's own
  `credential_id` — `openverse-sound`, the key this lane has always read; renaming it would hide a
  stored token, which is the one thing a store migration must never do. `set_values_for` is the only
  write that does not resolve a `ProviderDefinition`; it takes the field list from the caller's own
  declaration and **refuses** an unknown key rather than dropping it, because "saved" and "saved
  nothing" must not be the same answer.
- **A secret is never echoed, logged, or in error text.** `sound_sources.describe` masks with the
  same `mask()` the provider console uses; `tests/test_sfx_providers.py` asserts the raw value is
  absent from every response and from every log line captured across save, list, search and clear.
- **Anonymous is not unconfigured.** Openverse answers with no key, so the card publishes
  `configured` and `registered` as two separate facts; one badge would call a working lane dead. The
  quota line says **not read yet** until a search has read the upstream's own headers — never `0`.
- **Provider cards are grouped by what they serve, derived from `modalities`.**
  `definitions.service_group()` files a provider under its first lane in `MODALITIES` order **that
  its adapter can actually serve** (`serves()`, not the declared tuple: Kie declares chat and cannot
  run it, so it is filed under Image); `group_by_service()` orders the headings off the same rows.
  There is no hand-kept vendor list, every provider lands in exactly one group, and dispatch,
  enablement and discovery read none of it. The console's filter matches lanes too, because a
  multi-lane provider is filed under only one of them.
