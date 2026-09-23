"""A model that accepts only the default temperature is not offered the control.

OpenAI's own API publishes no `supported_parameters`, so the gateway's over-offer
in `_chat_row` handed Story Studio a `temperature` control for `gpt-6-luna`, which
answered `400 "temperature does not support 0.7"` — Story Studio sends `0.7`
whenever the control is offered. The catalogue now withholds it for the known
default-only families, **only** on the provider that refuses them: a proxy that
accepts `0.7` keeps the control, and a provider that publishes its own
`supported_parameters` is always believed.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway import catalog  # noqa: E402
from gateway.providers.definitions import KIND_OPENAI_COMPATIBLE  # noqa: E402


def _provider(provider_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=provider_id,
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://example.test/v1",
        display_name=provider_id,
    )


def _controls(model_id: str, provider_id: str, **raw) -> list[str]:
    # `_chat_row` reads the provider's fields from `model["raw"]`.
    row = catalog._chat_row(model_id, _provider(provider_id), {"raw": {"id": model_id, **raw}})
    return row["controls"]


class ChatControlTests(unittest.TestCase):
    def test_openai_reasoning_models_do_not_offer_temperature(self) -> None:
        for model in ("gpt-6-luna", "gpt-6-luna-pro", "gpt-5.6-luna", "o1", "o3-mini", "o4-mini"):
            controls = _controls(model, "openai-direct")
            self.assertNotIn("temperature", controls, model)
            self.assertIn("max_output_tokens", controls, model)

    def test_openai_non_reasoning_models_keep_it(self) -> None:
        for model in ("gpt-4o", "gpt-4.1", "gpt-4o-mini"):
            self.assertIn("temperature", _controls(model, "openai-direct"), model)

    def test_a_proxy_that_accepts_0_7_keeps_the_control(self) -> None:
        # Experiential Labs served `gpt-5.6-luna` with 0.7 successfully; the rule is
        # scoped to the provider whose API refuses it, not to the model name.
        self.assertIn("temperature", _controls("gpt-5.6-luna", "experiential-labs"))

    def test_a_published_parameter_list_is_always_believed(self) -> None:
        # A provider's own answer wins over the curated table.
        self.assertIn(
            "temperature",
            _controls(
                "gpt-6-luna", "openai-direct",
                supported_parameters=["temperature", "max_tokens"],
            ),
        )
        # And a published list that omits it withholds it, as before.
        self.assertNotIn(
            "temperature",
            _controls("some-model", "openai-direct", supported_parameters=["max_tokens"]),
        )


if __name__ == "__main__":
    unittest.main()
