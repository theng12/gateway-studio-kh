"""The mode this gateway publishes is one Story Studio can actually read.

WHAT WAS WRONG. `_chat_row` distinguishes the two structured-output modes on purpose —
`response_format` is JSON-object mode, `structured_outputs` is JSON-schema mode — and it
published the stronger one as `"json_schema"`. Story Studio's union is

    structured_output_mode: 'none' | 'json_object' | 'request_json_schema'
    (story-studio-kh/electron/server/lib/genstudio-client.ts:1708)

and its parser maps anything that is neither of those two onto `'json_object'`
(`:4938-4944`). So the stronger mode arrived as the weaker one and the gateway's own
correct distinction was erased on the wire.

WHY THAT IS BEHAVIOUR AND NOT A LABEL. `productResponseFormat` in Story Studio sends a
provider-native `json_schema` with `strict: true` **only** when the product publishes
`request_json_schema` (`genstudio-client.ts:6234`). Read as `json_object`, the identical
request falls through to a wrapper instruction that describes the schema in prose and asks
the model to comply. That is the shape of the incident this project already paid for: a
model that complied for two packs of forty scenes and wrote prose for the third.

WHY THE SPELLING IS OURS TO CHANGE AND NOT THEIRS. This gateway is Story Studio's only
language surface, and its contract is that Story needs **zero code changes** to talk to it
(`AGENTS.md`). The published vocabulary is therefore GenStudio's. `json_schema` remains
correct on the **request** side, where it is the provider's own `response_format.type`
(`adapters/studio_hub.py`, `adapters/openai_compat.py`) — the two spellings are not
interchangeable in either place.

WHAT THIS FILE CANNOT CATCH: it pins the vocabulary against a union written out below, not
against Story Studio's source — the two repositories are checked out separately and neither
test suite reads the other. If Story ever widens or renames its union, this file has to be
changed by hand, and the comment above is the pointer that makes that findable.
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

#: Story Studio's declared union, quoted from `genstudio-client.ts:1708`.
#: `None` (the field absent or null) is also readable: Story treats absent as
#: `json_object` deliberately, so that an older catalogue is not refused.
STORY_STUDIO_MODES = {"none", "json_object", "request_json_schema"}


def _mode(model_id: str, **raw) -> str | None:
    provider = SimpleNamespace(
        id="openai-direct",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://example.test/v1",
        display_name="openai-direct",
    )
    row = catalog._chat_row(model_id, provider, {"raw": {"id": model_id, **raw}})
    return row["structured_output_mode"]


class ThePublishedModeIsSpelledTheWayStoryReadsItTest(unittest.TestCase):
    def test_a_provider_native_schema_publishes_request_json_schema(self) -> None:
        # This is the regression. It published `"json_schema"`, which Story Studio maps
        # onto `json_object`, so the strict schema was never requested.
        self.assertEqual(_mode("some-model", supported_parameters=["structured_outputs"]),
                         "request_json_schema")

    def test_json_object_mode_still_publishes_json_object(self) -> None:
        self.assertEqual(_mode("some-model", supported_parameters=["response_format"]),
                         "json_object")

    def test_both_parameters_still_publish_the_weaker_one(self) -> None:
        # Unchanged and deliberate: when both are stated, `json_object` is true and is
        # the value this field has always carried.
        self.assertEqual(
            _mode("some-model", supported_parameters=["response_format", "structured_outputs"]),
            "json_object")

    def test_a_silent_provider_publishes_no_mode(self) -> None:
        # Absent is its own claim, and Story Studio reads it as `json_object` on purpose.
        self.assertIsNone(_mode("some-model"))

    def test_a_provider_without_either_parameter_publishes_no_mode(self) -> None:
        self.assertIsNone(_mode("some-model", supported_parameters=["temperature"]))


class EveryValueThisGatewayCanPublishIsReadableTest(unittest.TestCase):
    """The guard that survives the next spelling: nothing may leave outside the union."""

    def test_no_route_through_this_function_publishes_an_unreadable_mode(self) -> None:
        shapes = [
            {},
            {"supported_parameters": []},
            {"supported_parameters": ["temperature", "max_tokens"]},
            {"supported_parameters": ["response_format"]},
            {"supported_parameters": ["structured_outputs"]},
            {"supported_parameters": ["response_format", "structured_outputs"]},
        ]
        for raw in shapes:
            published = _mode("some-model", **raw)
            self.assertTrue(
                published is None or published in STORY_STUDIO_MODES,
                f"{published!r} is not a mode Story Studio can read (from {raw})")

    def test_the_request_side_spelling_is_not_published(self) -> None:
        # `json_schema` is the provider's own `response_format.type` and belongs on the
        # request. Publishing it was the defect; this makes its return loud.
        published = {
            _mode("m", supported_parameters=["response_format"]),
            _mode("m", supported_parameters=["structured_outputs"]),
            _mode("m"),
        }
        self.assertEqual(
            published, {"json_object", "request_json_schema", None},
            "the set of modes this function can publish changed — check it against "
            "Story Studio's union before accepting the new value")
        self.assertNotIn("json_schema", published)


if __name__ == "__main__":
    unittest.main()
