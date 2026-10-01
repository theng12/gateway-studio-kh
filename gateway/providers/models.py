"""Model inventory — discovered, curated and user-added models, with toggles.

A model reaches Story Studio only when all three hold:

    1. its provider is enabled,
    2. its provider has a usable credential,
    3. the model itself is enabled.

Rule 3 is what keeps a 300-model aggregator from flooding the picker. Newly
discovered models follow the provider's `auto_enable_discovered` policy, and any
explicit operator decision is remembered and wins from then on.

Cached discovery results live in `data/models_cache.json` so the catalogue
survives a restart even when a provider is briefly unreachable.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from ..config import MODEL_CACHE_PATH, write_secret
from .credentials import CredentialStore
from .definitions import (
    DISCOVERY_STATIC,
    IMAGE_REFERENCE_CAPABLE_KINDS,
    KIND_STUDIO_HUB,
    PROVIDERS_BY_ID,
    ProviderDefinition,
    image_reference_state,
    music_can_serve,
    chat_can_serve,
    transcription_can_serve,
)
from .discovery import DiscoveryResult, DiscoveredModel, discover


#: Models the owner has retired. Matched case-insensitively against a row's model id and the
#: Hub's `package_id`, so a Hub that has not yet dropped one still has it withheld here.
#: LTX-2.3 (local video), retired 2026-10-01. Do not add a model that is only a dependency.
RETIRED_MODEL_IDS: tuple[str, ...] = (
    "ltx-2-3-22b-ram-12gb-mlx",
    "baa-ai/ltx-2.3-22b-ram-12gb-mlx",
)


class ModelInventory:
    def __init__(
        self,
        credentials: CredentialStore,
        path: Path | None = None,
        definition_lookup: "Callable[[str], Any] | None" = None,
        definitions_lookup: "Callable[[], list[Any]] | None" = None,
    ) -> None:
        self.path = Path(path or MODEL_CACHE_PATH)
        self.credentials = credentials
        # Resolves ids the static map does not know about — chiefly locations
        # added on the Fleet page, which are providers like any other.
        self._lookup = definition_lookup
        # The *list* of providers, for the operations that walk every one of
        # them (discovery of all, the summary, and building the catalogue).
        # Iterating the static map instead silently omitted every location the
        # owner added on the Fleet page.
        self._all = definitions_lookup
        self._data: dict[str, Any] = {"providers": {}, "enabled": {}, "user": {}}
        self._load()

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(parsed, dict):
            for key in ("providers", "enabled", "user"):
                if isinstance(parsed.get(key), dict):
                    self._data[key] = parsed[key]

    def _save(self) -> None:
        # Not a secret file, but it does reveal which models are configured, so
        # it stays owner-only like everything else in data/.
        write_secret(self.path, json.dumps(self._data, indent=2) + "\n")

    # -- discovery --------------------------------------------------------
    def _every_definition(self) -> list[tuple[str, Any]]:
        if self._all is not None:
            return [(d.id, d) for d in self._all()]
        return list(PROVIDERS_BY_ID.items())

    def _definition(self, provider_id: str) -> Any:
        """Resolve an id, including locations added on the Fleet page."""
        if self._lookup is not None:
            found = self._lookup(provider_id)
            if found is not None:
                return found
        return PROVIDERS_BY_ID.get(provider_id)

    def refresh(self, provider_id: str, *, force: bool = True,
                hub_version: str | None = None) -> DiscoveryResult:
        """Run live discovery for one provider and cache the result.

        `hub_version` is the Studio Hub build whose answers produced this list,
        recorded **beside** it so a later reader can answer "do these rows still
        describe the Hub that is running?" without discovering again. It is
        written only when discovery succeeded: a failed fetch learned nothing
        about the Hub, and a version recorded beside stale rows would claim they
        are current. `None` leaves any previous record alone, which is what every
        provider that cannot name a version needs.
        """
        definition = self._definition(provider_id)
        if definition is None:
            raise KeyError(provider_id)

        values = self.credentials.resolved(provider_id)
        result = discover(definition, values)

        entry = self._data["providers"].setdefault(provider_id, {})
        entry["fetched_at"] = result.fetched_at
        entry["error"] = result.error
        if result.error is None:
            # A static provider's models are curated in code, so caching them
            # only creates staleness: a model removed from the definition would
            # survive in the cache and keep appearing — and, because the merge
            # unions curated with cached, it would also stay enabled.
            if definition.discovery == DISCOVERY_STATIC:
                entry["models"] = []
            else:
                entry["models"] = [
                    {
                        "model_id": m.model_id,
                        "display_name": m.display_name,
                        "modality": m.modality,
                        "raw": m.raw,
                    }
                    for m in result.models
                ]
            # What the provider listed that this gateway could not classify at
            # all. Kept beside the models rather than folded into `error`: the
            # discovery itself succeeded, and one unrecognised engine must not
            # make a working provider read as broken.
            entry["skipped"] = list(result.skipped)
        if result.error is None and hub_version is not None:
            # The build this list came from. Not on failure: a fetch that did not
            # answer tells us nothing about the Hub, and recording a version
            # beside rows we could not refresh would mark them current.
            entry["hub_version"] = str(hub_version).strip()
        self._save()
        return result

    def refresh_all(self, *, only_enabled: bool = False) -> dict[str, dict[str, Any]]:
        outcomes: dict[str, dict[str, Any]] = {}
        for provider_id, definition in self._every_definition():
            if only_enabled and not self.credentials.is_enabled(provider_id):
                continue
            result = self.refresh(provider_id)
            outcomes[provider_id] = {
                "count": len(result.models),
                "error": result.error,
            }
        return outcomes

    # -- reads ------------------------------------------------------------
    def cached(self, provider_id: str) -> dict[str, Any]:
        return dict(self._data["providers"].get(provider_id) or {})

    def _is_enabled(self, provider_id: str, model_id: str, definition: ProviderDefinition, source: str) -> bool:
        explicit = (self._data["enabled"].get(provider_id) or {}).get(model_id)
        if explicit is not None:
            return bool(explicit)
        if source == "user":
            return True
        return definition.auto_enable_discovered

    def models_for(self, provider_id: str, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        """Merged model list for one provider: discovered + curated + user-added."""
        definition = self._definition(provider_id)
        if definition is None:
            return []

        merged: dict[str, dict[str, Any]] = {}

        for curated in definition.static_models:
            model_id = str(curated["id"])
            merged[model_id] = {
                "model_id": model_id,
                "display_name": curated.get("display_name", model_id),
                "modality": curated.get("modality", definition.modalities[0]),
                "source": "curated",
                "raw": {},
            }

        cached = self.cached(provider_id)
        for item in cached.get("models") or []:
            if not isinstance(item, dict) or not item.get("model_id"):
                continue
            merged[item["model_id"]] = {
                "model_id": item["model_id"],
                "display_name": item.get("display_name") or item["model_id"],
                "modality": item.get("modality") or definition.modalities[0],
                "source": "discovered",
                "raw": item.get("raw") or {},
            }

        for item in self._data["user"].get(provider_id) or []:
            if not isinstance(item, dict) or not item.get("model_id"):
                continue
            merged[item["model_id"]] = {
                "model_id": item["model_id"],
                "display_name": item.get("display_name") or item["model_id"],
                "modality": item.get("modality") or definition.modalities[0],
                "source": "user",
                "raw": {},
            }

        output: list[dict[str, Any]] = []
        for model_id, item in sorted(merged.items(), key=lambda kv: kv[1]["display_name"].lower()):
            item["enabled"] = self._is_enabled(provider_id, model_id, definition, item["source"])
            if include_disabled or item["enabled"]:
                output.append(item)
        return output

    def withhold_reason(self, definition: Any, model: dict[str, Any]) -> str | None:
        """Why this model cannot be offered, or `None` when it can.

        **One function, two answers.** `can_publish` is exactly
        `withhold_reason(...) is None`, and the console prints what this returns.
        They are not two readers of one rule by convention — they are one function,
        because a separate "why?" explainer is free to disagree with the chooser the
        first time either gains a step. That is how the console came to advertise
        rows the catalogue silently dropped in the first place.

        The sentence matters as much as the boolean. Measured 2026-09-25: eleven
        enabled models on the local location were withheld, every one of them
        **correctly** — nine because the Hub said `files_state: not_installed`, two
        because the Hub said `catalogued: false` (a codec and a tokenizer, not
        models) — and the console said *"this model cannot be offered"* eleven times.
        The owner asked why, and the answer was already in the row.

        Four questions, finest last:

          1. does the provider **declare** the modality at all;
          2. does its adapter **serve** that modality (a kind having an adapter is
             not the same claim — Google declares six and serves four);
          3. is there a **per-model** reason it cannot answer (OpenAI's newer
             transcription models cannot return the SRT this gateway must send;
             Lyria's realtime model has no one-shot call);
          4. does the Hub itself say the model is not runnable **there**.
        """
        from ..adapters import serves

        retired_ids = {str(model.get("model_id") or "").lower(),
                       str((model.get("raw") or {}).get("package_id") or "").lower()}
        if retired_ids & set(RETIRED_MODEL_IDS):
            return "this model has been retired and is no longer offered"

        modality = str(model.get("modality") or "")
        if modality not in definition.modalities:
            return f"{definition.display_name} does not offer {modality or 'this modality'}"
        if not serves(definition, modality):
            return (
                f"this gateway has no {modality} lane for {definition.display_name} — "
                "it declares the modality but nothing here serves it"
            )
        model_id = str(model.get("model_id") or "")
        if modality == "chat" and model_id.endswith(":batch"):
            base = self.credentials.resolved(definition.id).get("base_url") or definition.base_url
            # OpenRouter's batch-only routes cannot serve our chat-completions
            # requests. Match the serving host, including custom provider entries.
            try:
                host = urlsplit(base).hostname
            except ValueError:
                return "this model's batch route has no usable host"
            if host == "openrouter.ai":
                return "a batch-only route: half price, and it answers 404 to a normal request"
        if modality == "transcription" and not transcription_can_serve(model_id):
            return "this model cannot return the SRT this gateway must ask for"
        if modality == "music" and not music_can_serve(model_id):
            return "this model has no one-shot call this gateway can make"
        # A local video lane is only usable when the Studio publishes its own contract: the
        # modes it accepts, the canvases and durations, and the artifact it returns. The
        # gateway does not invent those from a model name — it publishes what the worker said,
        # and with nothing said there is no honest row to offer.
        if modality == "video" and definition.kind == KIND_STUDIO_HUB:
            contract = (model.get("raw") or {}).get("lane_contract")
            if not isinstance(contract, dict) or not contract.get("modes"):
                return ("this location's video model publishes no usable lane contract, so the "
                        "gateway cannot describe what it accepts")
        # A chat catalogue is not a list of chat models. NVIDIA NIM answers every
        # one of its 82 rows with only `{id, owned_by}`, so its embeddings, its
        # safety classifiers, its reward model, its OCR parsers and its translators
        # were all offered as chat and failed on use. This is the third instance of
        # the shape; see `chat_can_serve`.
        if modality == "chat" and not chat_can_serve(model_id):
            return "this id is not a chat model — the provider lists it beside its chat rows"
        # An image endpoint whose own classification requires a reference image,
        # on a kind whose image body cannot send one. fal's queue accepts the
        # body and validates at the worker, so this reads as a job that
        # `COMPLETED` and then failed on download — the `:batch` shape again.
        # Withheld rather than offered; the gateway's own image bodies send a
        # prompt and nothing else for every kind outside
        # `IMAGE_REFERENCE_CAPABLE_KINDS`.
        if modality == "image":
            if (image_reference_state(definition.kind, model_id, model.get("raw") or {})
                    is True
                    and definition.kind not in IMAGE_REFERENCE_CAPABLE_KINDS):
                return "this endpoint requires a reference image, and this lane cannot send one"

        # Two things a location's package list cannot tell you, which the Hub's
        # own catalogue can — both **read**, and only when it states them, so an
        # absent key keeps meaning "unverified" rather than "withhold".
        #
        # 1. A location's packages include the codecs and tokenizers its models
        #    load, indistinguishable from a runnable model there. Publishing a
        #    tokenizer as a clone-capable voice model is the "control that does
        #    nothing" this rule exists to prevent, and it happened for real the
        #    moment the migrated packages were enabled: five companions and an
        #    uncatalogued variant reached Story Studio's picker that way.
        # 2. The Hub already decides whether a model fits the machine it runs on.
        #    A model that cannot run here must not be offered; its `memory_eligible`
        #    is that location's verdict, so a bigger site's copy stays publishable.
        # 3. `files_verified` is not "ready": the Hub combines files, runtime, OS
        #    and chip into `dispatch_state`, and it refuses a submit for a model
        #    that is not `eligible` with 409 MODEL_NOT_READY. Withhold it here so
        #    the picker never offers work the chosen controller will refuse.
        #    Absence stays unverified — a Hub that does not publish it withholds
        #    nothing.
        raw = model.get("raw") or {}
        # **The Hub's own `type`, from whichever read answered** (C5/M13). The catalogue says
        # `dependency` for a package that exists only to serve another — a tokenizer, a codec,
        # an encoder — and the package list says the same thing on its own rows. The rule used
        # to rest entirely on the *catalogue* read, so a catalogue that timed out (it has
        # happened; `models.py` records the incident) left a tokenizer publishable while the
        # package list had already answered `dependency` in the first request.
        # **A dependency of one studio can be the model of another.** The Hub types a package
        # `dependency` when any other package lists it as a companion, and Whisper turbo is
        # listed by two Qwen3 voice packages (it transcribes their reference clips) while also
        # being the Subtitle studio's own default model — so the Hub typed it `dependency` and
        # the picker had no transcription model at all. Every genuine companion (tokenizers,
        # codecs, text encoders) is absent from the Hub's catalogue; Whisper turbo is the one
        # that it describes. So a package row's `dependency` yields to a catalogue that
        # lists the package and does not itself say `dependency`. A catalogue that did not
        # answer sets no `catalogued` at all, so this never loosens the old rule then.
        catalogue_type = str(raw.get("catalogue_type") or "").strip().lower()
        package_type = str(raw.get("package_type") or "").strip().lower()
        described_as_model = raw.get("catalogued") is True and catalogue_type != "dependency"
        if catalogue_type == "dependency" or (package_type == "dependency" and not described_as_model):
            return ("the Hub describes this as a dependency of another model, not a model "
                    "to run")
        if raw.get("catalogued") is False:
            return (
                "the Hub does not catalogue this as a model — it is a companion its "
                "models load, such as a codec or a tokenizer"
            )
        if raw.get("memory_eligible") is False:
            return "this Mac does not have the memory the Hub says this model needs"
        dispatch_state = raw.get("dispatch_state")
        if dispatch_state is not None and dispatch_state != "eligible":
            files = str(raw.get("files_state") or "").strip()
            if files == "not_installed":
                return "the model files are not installed on this Mac"
            return f"the Hub reports it not ready to dispatch ({dispatch_state})"
        return None

    def can_publish(self, definition: Any, model: dict[str, Any]) -> bool:
        """Whether this model could ever be offered to Story Studio.

        **One rule, two readers** — and now one function. The catalogue asks it to
        decide what to publish, and the console asks it to decide what to list,
        because a model that can never run should not be shown as something to tick
        either. They were two separate sets of conditions and the console's was the
        weaker one, so it advertised rows the catalogue would silently drop; the
        console now prints `withhold_reason`, so the two cannot come apart again.
        """
        return self.withhold_reason(definition, model) is None

    def enabled_models(self, modality: str) -> list[tuple[ProviderDefinition, dict[str, Any]]]:
        """Every enabled model of a modality across every enabled provider.

        This is the exact set the Story Studio catalogue is built from.

        Providers whose kind has no adapter are skipped entirely. They remain
        discoverable and configurable — the point is that a model which would
        fail the moment Story Studio used it must never appear in the picker.

        So is a modality the adapter does not serve. "There is an adapter for
        this kind" was the only question asked here, and it is not the same one:
        Google's adapter forwards everything to the OpenAI-compatible shim, which
        serves four of the six modalities Google declares, so a discovered video
        row published and then failed on use.
        """
        output: list[tuple[ProviderDefinition, dict[str, Any]]] = []
        for provider_id, definition in self._every_definition():
            if not self.credentials.is_enabled(provider_id):
                continue
            if not self.credentials.is_configured(provider_id):
                continue
            for model in self.models_for(provider_id, include_disabled=False):
                if model["modality"] != modality:
                    continue
                if not self.can_publish(definition, model):
                    continue
                output.append((definition, model))
        return output

    # -- writes -----------------------------------------------------------
    def set_model_enabled(self, provider_id: str, model_id: str, enabled: bool) -> None:
        # Same reason as `add_user_model`: the static map does not hold the
        # locations derived from the fleet roster, and a location's models are
        # the ones most likely to be switched on one at a time.
        if self._definition(provider_id) is None:
            raise KeyError(provider_id)
        bucket = self._data["enabled"].setdefault(provider_id, {})
        bucket[model_id] = bool(enabled)
        self._save()

    def set_many(self, provider_id: str, model_ids: list[str], enabled: bool) -> int:
        # Resolved through `_definition`, exactly like `set_model_enabled` above
        # and `add_user_model` below — and for the *other* direction of the same
        # hazard. That map does not hold derived locations, while the **published**
        # namespace (`studio-hub`) is an id that is not a provider at all: a caller
        # sending it wrote enable state under a key `forget_provider` never sweeps,
        # so those decisions outlived the locations they were made for.
        if self._definition(provider_id) is None:
            raise KeyError(provider_id)
        bucket = self._data["enabled"].setdefault(provider_id, {})
        for model_id in model_ids:
            bucket[model_id] = bool(enabled)
        self._save()
        return len(model_ids)

    def add_user_model(
        self, provider_id: str, model_id: str, display_name: str, modality: str
    ) -> None:
        """Add a model a provider released but discovery cannot see.

        Resolved through `_definition`, **not** the static map. A Studio Hub
        location is a provider like any other, but it is derived at runtime from
        the fleet roster and so is never in `PROVIDERS_BY_ID`. The admin route
        checks the id with `registry.definition()` — which *does* know locations —
        and then this rejected the very id that had just been accepted, raising a
        bare `KeyError` that no `except ValueError` caught. Adding a model by hand
        to one of the owner's locations answered **500**, which is the failure
        mode this codebase has paid for four times already.
        """
        definition = self._definition(provider_id)
        if definition is None:
            raise KeyError(provider_id)
        if modality not in definition.modalities:
            raise ValueError(
                f"'{modality}' is not a modality of provider '{provider_id}'"
            )
        # **A declared modality is not a served one.** Google declares six and its
        # adapter serves four, so this form accepted rows that `can_publish` then
        # withholds for ever: enabled, counted in the header, never published — a
        # control that does nothing, which is the failure the owner asked to stop.
        # Checked here rather than only in the console, because the console is not
        # the only caller and a rule enforced in the UI is not enforced.
        #
        # Local import: `adapters` imports `providers.definitions`, not this module,
        # and a module-level edge between them invites a cycle for one predicate.
        from ..adapters import serves

        if not serves(definition, modality):
            raise ValueError(
                f"nothing on the gateway can serve '{modality}' for provider "
                f"'{provider_id}', so a model added here could never run"
            )
        bucket = self._data["user"].setdefault(provider_id, [])
        bucket = [m for m in bucket if m.get("model_id") != model_id]
        bucket.append(
            {
                "model_id": model_id,
                "display_name": display_name or model_id,
                "modality": modality,
                "added_at": time.time(),
            }
        )
        self._data["user"][provider_id] = bucket
        self.set_model_enabled(provider_id, model_id, True)

    def remove_user_model(self, provider_id: str, model_id: str) -> None:
        bucket = self._data["user"].get(provider_id) or []
        self._data["user"][provider_id] = [
            m for m in bucket if m.get("model_id") != model_id
        ]
        (self._data["enabled"].get(provider_id) or {}).pop(model_id, None)
        self._save()

    def cached_provider_ids(self) -> list[str]:
        """Every provider id the cache holds anything for."""
        return sorted(
            set(self._data.get("providers", {}))
            | set(self._data.get("enabled", {}))
            | set(self._data.get("user", {}))
        )

    def forget_provider(self, provider_id: str) -> int:
        """Drop every trace of a provider that no longer exists.

        Called when a Studio Hub location is removed, so this is not tidiness:
        the cache is what a rediscovered provider is seeded from. A location that
        went away left its whole model list and every enable/disable decision
        behind, and `enabled` is the owner's own answer to "should this be offered
        to Story Studio?" — carrying that answer over to a *different* location
        that happened to reuse the id would publish models nobody approved. The
        credential store already forgets the token for exactly this reason; the
        models and the decisions have to go with it.

        Returns how many cached models were dropped, and never raises: this runs
        on the removal path, and failing to tidy up must not block the removal.
        """
        dropped = 0
        cached = self._data.get("providers", {}).get(provider_id)
        if isinstance(cached, dict):
            models = cached.get("models")
            dropped = len(models) if isinstance(models, list) else 0
        self._data.get("providers", {}).pop(provider_id, None)
        self._data.get("enabled", {}).pop(provider_id, None)
        self._data.get("user", {}).pop(provider_id, None)
        self._save()
        return dropped

    # -- projection -------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """Counts for the admin UI header.

        **Two different questions, two different numbers.** `enabled_models` is what
        the operator has switched on; `publishable_models` is how many of those can
        actually reach Story Studio. They diverge whenever `can_publish` withholds
        something — a package the Hub's catalogue does not describe as a model, or
        one it says does not fit this machine — and reporting only the first said
        "121 live" while nine of them could not be picked. `by_modality` counts the
        **publishable** ones, because it is what the Overview chart labels
        "Offered to Story Studio".

        **`publishable_models` and `by_modality` come from `enabled_models()`**, the
        one function the catalogue is built from, rather than from a second walk with
        its own conditions. That walk was missing two of the catalogue's gates — a
        provider that is switched on with its key cleared has models and can offer
        none of them — so "offered" was inflated by rows nobody could pick. Deriving
        them means they cannot drift from the catalogue again.

        **Three names, three questions, and only one of them is "discovered".**
        `discovered_models` is every model on every provider, whatever its state;
        `total_models` is the models belonging to providers that are switched on; and
        `enabled_models` is the ones actually ticked. The Overview tile used to label
        `total_models` as "discovered", which is why a provider the owner had turned
        off made the discovered count fall.
        """
        total = enabled = discovered = 0
        for provider_id, definition in self._every_definition():
            live = self.credentials.is_enabled(provider_id)
            # The same gate the catalogue applies. A provider switched on with its
            # key cleared can publish nothing, and counting its rows as offered was
            # the defect this docstring's first paragraph is about.
            configured = self.credentials.is_configured(provider_id)
            for model in self.models_for(provider_id, include_disabled=True):
                discovered += 1
                if not live or not configured:
                    continue
                total += 1
                if model["enabled"]:
                    enabled += 1

        # The modalities are **derived from the providers**, not imported: the only
        # constant lives in `catalog.py`, which imports this module, so importing it
        # back would be a cycle — and a second copy of the tuple is the shape this
        # file keeps being fixed for. Deriving also means a provider declaring a
        # modality the catalogue's tuple does not know about still appears here.
        modalities = sorted({
            modality
            for _provider_id, definition in self._every_definition()
            for modality in (definition.modalities or ())
        })
        published = [
            (definition, model)
            for modality in modalities
            for definition, model in self.enabled_models(modality)
        ]
        offered: dict[str, int] = {}
        for _definition, model in published:
            offered[model["modality"]] = offered.get(model["modality"], 0) + 1
        publishable = len(published)
        return {
            "discovered_models": discovered,
            "total_models": total,
            "enabled_models": enabled,
            "publishable_models": publishable,
            "withheld_models": enabled - publishable,
            "by_modality": offered,
        }
