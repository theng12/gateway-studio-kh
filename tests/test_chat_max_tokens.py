"""Every chat lane forwards the caller's answer length, under every spelling.

WHY THIS EXISTS (`answer-length audit`, 2026-10-19). Three lanes build a chat body from
the caller's `params`, and they read different sets of names: the Studio Hub lane read
`max_completion_tokens`/`max_output_tokens`/`max_tokens`, the Anthropic lane read two of
the three, and the OpenAI-compatible lane read only `max_output_tokens`/`max_tokens`. So
the same request was answered differently depending on which lane served it: a caller
that used `max_completion_tokens` — OpenAI's current name for the field, and the spelling
the Hub's own lane contract publishes — had its allowance silently dropped on the other
two. `max_tokens` is the legacy name the same API still accepts.

**A class test, not a one-lane test.** The assertion is about *every* lane that builds a
chat body, so a lane added later that reads one spelling goes red here rather than
dropping a customer's number in production. What it cannot catch: a lane that reads the
caller's value and then overrides it with a constant of its own — that is what
`test_anthropic_structured.PublishedOutputLimitTests` and
`tests/test_hub_lane_contract.py` assert, and it is the failure the catalogue's
`published_output_cap` chooser exists to prevent.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env()  # never bind to the owner's real Application Support

import httpx  # noqa: E402

from gateway.adapters import anthropic, openai_compat, studio_hub  # noqa: E402
from gateway.adapters.base import BackendRequest  # noqa: E402
from gateway.providers.definitions import KIND_STUDIO_HUB, ProviderDefinition  # noqa: E402


#: The names a caller may legitimately use for "how long may the answer be".
#: `max_output_tokens` is this gateway's own published control, `max_completion_tokens`
#: is OpenAI's current name, and `max_tokens` is the legacy one its API still accepts.
SPELLINGS = ("max_output_tokens", "max_completion_tokens", "max_tokens")

ASKED = 1234


def _request(**params: object) -> BackendRequest:
    return BackendRequest(
        provider_id="provider",
        model_id="model",
        modality="chat",
        params={"messages": [{"role": "user", "content": "write"}], **params},
        values={"base_url": "http://mock", "api_key": "key"},
        base_url="http://mock",
    )


def _openai_body(**params: object) -> dict:
    """The body the OpenAI-compatible lane puts on the wire."""
    answer = httpx.Response(200, json={
        "model": "model",
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"completion_tokens": 1},
    })
    bodies: list[dict] = []
    with patch.object(openai_compat.httpx, "post",
                      lambda url, *, headers, json, timeout: bodies.append(json) or answer):
        openai_compat.OpenAICompatibleBackend().generate(_request(**params))
    return bodies[0]


def _anthropic_body(**params: object) -> dict:
    """The body the Anthropic lane puts on the wire (`max_tokens` is mandatory there)."""
    answer = httpx.Response(200, json={
        "model": "claude-haiku-5-5", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })
    bodies: list[dict] = []
    with patch.object(anthropic.httpx, "post",
                      lambda url, *, headers, json, timeout: bodies.append(json) or answer):
        anthropic.AnthropicBackend().generate(_request(**params))
    return bodies[0]


def _hub_body(**params: object) -> dict:
    """The body the Studio Hub lane puts on the wire.

    `_chat_body` is the lane's whole body builder and a pure function, so it is called
    directly rather than through a mock Hub: the question here is what this adapter
    *sends*, and a mocked Hub would only re-assert the mock.
    """
    definition = ProviderDefinition(
        id="studio-hub-mock", display_name="Mock Hub", category="multi",
        kind=KIND_STUDIO_HUB, base_url="http://mock", modalities=("chat",),
        discovery="static",
    )
    request = BackendRequest(
        provider_id=definition.id, model_id="model", modality="chat",
        params={"messages": [{"role": "user", "content": "write"}], **params},
        values={}, base_url="http://mock",
    )
    return studio_hub._chat_body(request)


#: One entry per lane that builds a chat body. A new lane belongs in this table.
LANES = {
    "openai_compat": _openai_body,
    "anthropic": _anthropic_body,
    "studio_hub": _hub_body,
}


class EveryChatLaneHonoursTheCallersAnswerLengthTests(unittest.TestCase):
    def test_every_lane_forwards_each_spelling_a_caller_may_use(self) -> None:
        for lane, body_of in LANES.items():
            for spelling in SPELLINGS:
                with self.subTest(lane=lane, spelling=spelling):
                    self.assertEqual(
                        ASKED, body_of(**{spelling: ASKED}).get("max_tokens"),
                        f"{lane} dropped the caller's {spelling}",
                    )

    def test_a_caller_that_asks_for_nothing_is_never_given_a_number_it_did_not_choose(self) -> None:
        """The optional lanes send no `max_tokens` at all when the caller states none.

        This is the half of the rule that keeps the fix honest: the answer to "the caller
        asked for nothing" is *the published cap, or nothing* — never a number this
        adapter invented. `published_output_cap` fills `request.max_output_tokens` for the
        lanes that must state one; these two need not, so they leave the field off the
        wire and the provider's own allowance applies.
        """
        self.assertNotIn("max_tokens", _openai_body())
        self.assertNotIn("max_tokens", _hub_body())


if __name__ == "__main__":
    unittest.main()
