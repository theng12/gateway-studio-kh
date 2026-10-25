"""The number a Hub relays arrives with the Hub's own token, not this gateway's guess.

WHAT WAS WRONG. `_chat_row` has published `*_source` beside every limit since 2026-09-24, precisely
so Story Studio can tell a model's own figure from somebody's fallback. For a Studio Hub location the
value is relayed from the Hub, and `_limit_with_source` answers `provider` for **any** stated value —
so the live row published

    max_output_tokens: 16384
    max_output_tokens_source: "provider"
    max_output_tokens_source_detail: "worker default: the checkpoint ships no generation_config.json"

The token said the model stated it; the sentence directly beside it said the opposite. Story Studio
reads the token, and its own row says what that costs: *"16,384 currently renders as if the model
stated it"* — while the Hub's `FALLBACK_MAX_OUTPUT_TOKENS` is where the number actually comes from.
Two published fields disagreeing about one number, with the machine-readable one wrong.

WHY THE FIX IS A RELAY AND NOT A PARSE. The gateway could have matched a word in the Hub's sentence
("worker default: …") and that is exactly what the Hub asked it not to do — three defects on their
side came from matching a name where a fact could have been published. So the Hub now states
`source_kind` and this gateway carries it through, which is HUB-20's whole shape.

WHAT THIS FILE CANNOT CATCH. It drives `_chat_row` directly, so it proves the row this gateway builds
from a given `raw` — not that a real Hub sends that `raw`. The wire half is pinned on the producer's
side by their own suite; the two are joined only by this vocabulary, which is why
`test_the_relayed_vocabulary_is_the_one_the_consumer_reads` exists.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

# Importing gateway.* here freezes DATA_DIR; without this it binds the owner's real
# data folder at ~/Library/Application Support/gateway-kh/data.
_SANDBOX = sandbox_env()

from gateway import catalog  # noqa: E402
from gateway.providers.definitions import KIND_OPENAI_COMPATIBLE, KIND_STUDIO_HUB  # noqa: E402

#: The tokens a Hub location may state, quoted from the consumer's ask (Story Studio's HUB-20 row:
#: ``model`` / ``worker_default`` / ``operator_default`` / ``curated`` / ``gateway`` / ``unknown``).
HUB_KINDS = {"model", "worker_default", "operator_default", "curated", "gateway", "unknown"}


def _definition(kind: str, provider_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=provider_id, kind=kind, base_url="https://example.test/v1", display_name=provider_id,
    )


def _row(kind: str, provider_id: str, limits: dict | None, **raw) -> dict:
    body = {"id": "some-model", **raw}
    if limits is not None:
        body["model_limits"] = limits
    return catalog._chat_row(
        "some-model", _definition(kind, provider_id), {"raw": body},
    )


def _hub_limits(kind: str | None) -> dict:
    entry = {"value": 16384, "source": "worker default: the checkpoint ships no generation_config.json"}
    if kind is not None:
        entry["source_kind"] = kind
    return {"max_output_tokens": entry}


class AHubsTokenIsRelayedTest(unittest.TestCase):
    def test_a_worker_default_is_not_published_as_the_models_own_number(self) -> None:
        # The regression. This published "provider" — the model's own figure — for a number the
        # Hub's own sentence beside it called a worker default.
        row = _row(KIND_STUDIO_HUB, "studio-hub-local", _hub_limits("worker_default"))
        self.assertEqual(row["max_output_tokens"], 16384)
        self.assertEqual(row["max_output_tokens_source"], "worker_default")

    def test_a_checkpoint_stated_limit_reads_as_the_models(self) -> None:
        row = _row(KIND_STUDIO_HUB, "studio-hub-local", _hub_limits("model"))
        self.assertEqual(row["max_output_tokens_source"], "model")

    def test_the_sentence_is_still_published_beside_the_token(self) -> None:
        # A token is what a program branches on; the sentence is what a person reads. Losing
        # either one is a different defect from the one above.
        row = _row(KIND_STUDIO_HUB, "studio-hub-local", _hub_limits("worker_default"))
        self.assertIn("worker default", row["max_output_tokens_source_detail"])

    def test_a_hub_that_states_no_token_behaves_exactly_as_before(self) -> None:
        # Older Hubs publish the sentence and no token. The change is additive: an absent kind
        # must not become a new guess, and must not start refusing the value.
        row = _row(KIND_STUDIO_HUB, "studio-hub-local", _hub_limits(None))
        self.assertEqual(row["max_output_tokens"], 16384)
        self.assertEqual(row["max_output_tokens_source"], "provider")

    def test_a_cloud_row_keeps_the_three_classes_this_gateway_chooses(self) -> None:
        # The Hub path must not leak onto providers it does not govern.
        row = _row(KIND_OPENAI_COMPATIBLE, "openai-direct", None, max_completion_tokens=4096)
        self.assertIn(row["max_output_tokens_source"], ("provider", "curated", "gateway"))

    def test_a_null_from_the_hub_is_not_relayed_as_unknown(self) -> None:
        # The Hub states `value: null` with `kind: unknown` — nothing tells it the limit. The
        # gateway then publishes one of ITS OWN, because nulling the field is a breaking change
        # for the reader (Story Studio substitutes 8,192 and prints it as the model's). So the
        # honest token for the number now on the wire is `gateway`, **not** `unknown`: a kind
        # describes the value beside it, and `unknown` would say "nobody stated this" while the
        # row carries a number this gateway chose. Two claims sharing one value is the defect
        # every rule in this project is built around.
        row = _row(KIND_STUDIO_HUB, "studio-hub-local",
                   {"max_output_tokens": {"value": None, "source": "nothing states it",
                                          "source_kind": "unknown"}})
        self.assertEqual(row["max_output_tokens_source"], "gateway")

    def test_the_kind_is_relayed_only_alongside_a_value_the_hub_supplied(self) -> None:
        # The rule stated positively, so it is not rediscovered: a kind travels with the Hub's
        # own number, and never labels a number the Hub did not give.
        supplied = _row(KIND_STUDIO_HUB, "studio-hub-local", _hub_limits("worker_default"))
        withheld = _row(KIND_STUDIO_HUB, "studio-hub-local",
                        {"max_output_tokens": {"value": None, "source": "nothing states it",
                                               "source_kind": "worker_default"}})
        self.assertEqual(supplied["max_output_tokens_source"], "worker_default")
        self.assertNotEqual(withheld["max_output_tokens_source"], "worker_default")


class TheRelayedVocabularyIsTheOneTheConsumerReadsTest(unittest.TestCase):
    """The guard that survives the next token: nothing may leave outside the agreed set."""

    def test_every_kind_the_gateway_will_relay_is_one_the_consumer_knows(self) -> None:
        for kind in sorted(HUB_KINDS):
            with self.subTest(kind=kind):
                row = _row(KIND_STUDIO_HUB, "studio-hub-local", _hub_limits(kind))
                self.assertEqual(row["max_output_tokens_source"], kind)

    def test_a_gateway_chosen_source_is_still_one_of_its_own_three(self) -> None:
        # `LIMIT_SOURCES` is documentation, not a gate — this is what makes it load-bearing.
        row = _row(KIND_OPENAI_COMPATIBLE, "openai-direct", None)
        self.assertIn(row["max_output_tokens_source"], catalog.LIMIT_SOURCES)


if __name__ == "__main__":
    unittest.main()
