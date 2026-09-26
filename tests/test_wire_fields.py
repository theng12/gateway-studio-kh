"""The fields Story Studio reads that the gateway never published — and why.

WHAT WAS WRONG (`WIRE-3`, measured 2026-09-27). Story Studio's `GenStudioChatModel` carries
twenty-five fields the catalogue never publishes. Twenty-two of them belong to the old GenStudio's
curated database and have no source here — the honest answer for those is "nothing states it", which
is a decision, recorded in `docs/tasks/HANDOFF.md`. Three are different, because the fact existed
and was being thrown away:

  * **`activation_blockers`** — the reason a row cannot be used. It sat in every row as
    `lock_reason`, unread by the only caller that asked for it, so Story Studio's operator-facing
    sentence was its own generic fallback ("This model is not currently available in GenStudio")
    while the real reason was one field away.

  * **the retirement family** (`retires_at`, `deprecation_warning*`, `scheduled_retirement_at`) —
    OpenRouter publishes `expiration_date` on a model it is retiring, and **discovery's key
    whitelist discarded it at fetch time**, so it never reached `raw` and the catalogue read `None`
    for every model. Story Studio has a whole retirement surface (model-selector badge,
    `genstudio-model-retirement.ts`) that could never fire.

  * the whitelist itself, which had already been patched twice by adding one more key
    (`top_provider` for the output allowance, `maximum_text_length_per_request` for the TTS limit).
    Each patch left the next provider fact requiring a discovery change before anyone could read it,
    which is the defect rather than its symptom.

WHAT THIS FILE CANNOT CATCH. It drives `discovery._openai_models`'s raw assembly and `_chat_row`
directly, so it proves the shape this gateway builds from a given provider entry — not that a live
OpenRouter response carries `expiration_date` today. When one does, the row publishes it; when none
does, nothing is invented, and that is the assertion below.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import catalog  # noqa: E402
from gateway.providers.definitions import DISCOVERY_OPENAI_MODELS, ProviderDefinition  # noqa: E402


DEFINITION = ProviderDefinition(
    id="openrouter-main", display_name="OpenRouter", category="cloud",
    kind="openai_compat", base_url="https://openrouter.ai/api/v1",
    modalities=("chat",), discovery=DISCOVERY_OPENAI_MODELS,
)


def row(raw: dict) -> dict:
    return catalog._chat_row("some/model", DEFINITION, {"model_id": "some/model", "raw": raw})


class ActivationBlockerTests(unittest.TestCase):
    def test_a_locked_row_publishes_the_reason_it_is_locked(self) -> None:
        from gateway.catalog import _publish_lock

        built = {"id": "x"}
        _publish_lock(built, True, "No Studio Hub location offering this model is answering.")
        self.assertEqual(built["lock_reason"],
                         "No Studio Hub location offering this model is answering.")
        # `_story_row` is where the blocker list is assembled — the same sentence,
        # in the field the consumer reads.
        source = Path(catalog.__file__).read_text()
        self.assertIn('row["activation_blockers"] = blockers', source)
        self.assertIn("if lock_reason:", source)

    def test_a_location_row_carries_the_hubs_own_reason_too(self) -> None:
        """A package the Hub cannot place states why, and that sentence travels."""
        source = Path(catalog.__file__).read_text()
        self.assertIn('hub_reason = str((model.get("raw") or {}).get("reason") or "").strip()', source)

    def test_a_usable_row_publishes_an_empty_list_not_a_sentence(self) -> None:
        """Empty means "usable"; it must never read as a reason nobody wrote."""
        source = Path(catalog.__file__).read_text()
        self.assertIn("blockers: list[str] = []", source)


class RetirementTests(unittest.TestCase):
    def test_a_stated_expiry_becomes_the_retirement_family(self) -> None:
        built = row({"expiration_date": "2026-12-01", "context_length": 128000})
        self.assertEqual(built["retires_at"], "2026-12-01")
        self.assertIs(built["deprecation_warning"], True)
        self.assertEqual(built["deprecation_warning_at"], "2026-12-01")
        self.assertEqual(built["scheduled_retirement_at"], "2026-12-01")
        self.assertEqual(built["retirement_source"], "provider")

    def test_every_spelling_a_provider_uses_is_read(self) -> None:
        for spelling in ("expiration_date", "deprecation_date", "retirement_date", "retires_at"):
            with self.subTest(spelling=spelling):
                built = row({spelling: "2027-01-02T00:00:00Z"})
                self.assertEqual(built["retires_at"], "2027-01-02T00:00:00Z")
                self.assertEqual(built["retirement_source"], "provider")

    def test_nothing_stated_publishes_nothing_retired(self) -> None:
        """The whole point: absence is unstated, never "it does not retire"."""
        built = row({"context_length": 128000})
        self.assertIsNone(built["retires_at"])
        self.assertIs(built["deprecation_warning"], False)
        self.assertIsNone(built["upstream_shutdown_at"])
        self.assertIsNone(built["replacement_model_id"])
        self.assertIsNone(built["retirement_source"])

    def test_the_family_stays_in_the_consumers_vocabulary(self) -> None:
        """Story Studio parses these six names; a rename here is a silence there."""
        built = row({"expiration_date": "2026-12-01"})
        for field in ("deprecation_warning", "deprecation_warning_at", "scheduled_retirement_at",
                      "upstream_shutdown_at", "retires_at", "replacement_model_id"):
            self.assertIn(field, built)


class DiscoveryKeepsWhatItFetchedTests(unittest.TestCase):
    def test_the_whole_provider_entry_survives(self) -> None:
        """The whitelist is gone: a `/models` entry reaches `raw` intact."""
        source = Path(catalog.__file__).resolve().parents[0] / "providers" / "discovery.py"
        text = source.read_text()
        start = text.index("def _openai_models(")
        end = text.index("def ", start + 10)
        body = text[start:end]
        self.assertIn("raw=dict(entry)", body)
        self.assertNotIn('if k in {"id", "name"', body,
                         "the key whitelist is back — the next provider fact will need a code change")


if __name__ == "__main__":
    unittest.main()
