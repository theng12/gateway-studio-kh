"""Offline check of the committed subtitle triple (e4462ec) through the real private route.

No Hub, no renderer, no network: the actual data/final-render-subtitle bytes must
reach the runtime service, be published by /v1/final-renders/capabilities, and
select the subtitle contract by their own digest.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUN_ROOT = Path(tempfile.mkdtemp(prefix="gateway-subtitle-committed-"))
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(RUN_ROOT),
    "GATEWAY_DATA": str(RUN_ROOT / "data"),
    "GATEWAY_ARTIFACTS": str(RUN_ROOT / "artifacts"),
    "GATEWAY_DB": str(RUN_ROOT / "gateway.sqlite3"),
    "GATEWAY_CREDENTIALS": str(RUN_ROOT / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(RUN_ROOT / "models.json"),
    "GATEWAY_ENV_FILE": str(RUN_ROOT / ".env"),
    "GATEWAY_FLEET": str(RUN_ROOT / "fleet.json"),
    "GATEWAY_NO_MIGRATE": "1",
    "STUDIOHUB_READY_FILE": str(RUN_ROOT / "no-hub.json"),
    "GATEWAY_API_KEY": "ordinary-fixture-key",
})

from fastapi.testclient import TestClient  # noqa: E402
from gateway import api, runtime  # noqa: E402
from gateway.render_contract import SUBTITLE_CONTRACT  # noqa: E402
from gateway.render_service import AuthenticatedPrivateRender, RenderServiceError  # noqa: E402


BUNDLE = ROOT / "data" / "final-render-subtitle"
PROFILE_BYTES = (BUNDLE / "profile.json").read_bytes()
TOOLS_BYTES = (BUNDLE / "tools.json").read_bytes()
COMPILER_BYTES = (BUNDLE / "compiler.json").read_bytes()
PROFILE = json.loads(PROFILE_BYTES)
PROFILE_DIGEST = hashlib.sha256(PROFILE_BYTES).hexdigest()
TOOLS_DIGEST = hashlib.sha256(TOOLS_BYTES).hexdigest()
COMPILER_DIGEST = hashlib.sha256(COMPILER_BYTES).hexdigest()
PRINCIPAL = AuthenticatedPrivateRender(
    owner_scope="22222222-2222-4222-8222-222222222222",
    gateway_instance_id="33333333-3333-4333-8333-333333333333",
    story_instance_id="44444444-4444-4444-8444-444444444444",
)


def candidate(**changes: object) -> dict:
    capability = {
        "profile_execution_bounds": PROFILE["execution_bounds"],
        "profile_digest": PROFILE_DIGEST,
        "tools_digest": TOOLS_DIGEST,
        "compiler_digest": COMPILER_DIGEST,
        "font_support": True,
        "subtitles": {"format": SUBTITLE_CONTRACT},
        "bounds": {"output_limit_bytes": 1},
    }
    capability.update(changes)
    return {"site_id": "site", "roster_controller_id": "controller",
            "node_id": "node", "capability": capability}


class CommittedSubtitleBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        api.app.dependency_overrides[api.require_private_render] = lambda: PRINCIPAL
        self.addCleanup(api.app.dependency_overrides.clear)
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(self.client.close)

    def test_runtime_service_carries_the_committed_triple_unchanged(self) -> None:
        service = runtime.render_service
        self.assertEqual(service.subtitle_profile_descriptor, PROFILE_BYTES)
        self.assertEqual(service.subtitle_profile_digest, PROFILE_DIGEST)
        self.assertEqual(service.subtitle_profile_facts["tools"],
                         {"descriptor_utf8": TOOLS_BYTES.decode(), "digest": TOOLS_DIGEST})
        self.assertEqual(service.subtitle_profile_facts["compiler"],
                         {"descriptor_utf8": COMPILER_BYTES.decode(), "digest": COMPILER_DIGEST})
        self.assertEqual(service.subtitle_profile, PROFILE)
        self.assertEqual(PROFILE["format"], "studio.render.profile.v4")
        self.assertIs(PROFILE["production_eligible"], False)

    def test_private_route_publishes_the_committed_identity(self) -> None:
        with patch("gateway.render_service.fleet.render_candidates",
                   return_value=[candidate()]) as read:
            response = self.client.get("/v1/final-renders/capabilities",
                                       params={"requested_contract": SUBTITLE_CONTRACT})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(read.call_args_list)
        for call in read.call_args_list:
            self.assertEqual(call.args[0], PROFILE_DIGEST)
            self.assertEqual(call.kwargs["requested_contract"], SUBTITLE_CONTRACT)
        self.assertEqual(body["profile_descriptor_utf8"], PROFILE_BYTES.decode())
        self.assertEqual(body["profile_digest"], PROFILE_DIGEST)
        self.assertEqual(body["tools"], {"descriptor_utf8": TOOLS_BYTES.decode(),
                                         "digest": TOOLS_DIGEST})
        self.assertEqual(body["compiler"], {"descriptor_utf8": COMPILER_BYTES.decode(),
                                            "digest": COMPILER_DIGEST})
        self.assertEqual((body["eligible"], body["idle"]), (1, 1))
        self.assertEqual(body["subtitles"], {"format": SUBTITLE_CONTRACT})

    def test_a_candidate_with_another_identity_is_not_advertised(self) -> None:
        for key in ("profile_digest", "tools_digest", "compiler_digest"):
            with self.subTest(key=key), patch(
                    "gateway.render_service.fleet.render_candidates",
                    return_value=[candidate(**{key: "f" * 64})]):
                response = self.client.get("/v1/final-renders/capabilities",
                                           params={"requested_contract": SUBTITLE_CONTRACT})
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual((body["eligible"], body["idle"]), (0, 0))
            self.assertNotIn("subtitles", body)

    def test_packet_selects_subtitle_only_by_the_committed_digest(self) -> None:
        contract, (descriptor, digest, profile, facts) = runtime.render_service._packet_profile(
            {"recipe": {"profile_digest": PROFILE_DIGEST}})
        self.assertEqual(contract, SUBTITLE_CONTRACT)
        self.assertEqual(descriptor, PROFILE_BYTES)
        self.assertEqual(digest, PROFILE_DIGEST)
        self.assertEqual(profile, PROFILE)
        self.assertEqual(facts["tools"]["digest"], TOOLS_DIGEST)
        with self.assertRaises(RenderServiceError) as caught:
            runtime.render_service._packet_profile({"recipe": {"profile_digest": "f" * 64}})
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("RENDER_PROFILE_MISMATCH", 409))


if __name__ == "__main__":
    unittest.main()
