"""A publish/update cannot silently choose the helper's default Gateway."""
from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tests._isolation import sandbox_env

_SANDBOX = sandbox_env("gateway-release-origin-")

from fastapi.testclient import TestClient  # noqa: E402
from gateway import api, release_publisher, release_rollout, story_releases  # noqa: E402

ORIGIN = "http://gateway.test:8787"
MUTATIONS = (
    ("/admin/api/story/release/publish", {"artifact_path": "/unused/story.dmg"}),
    ("/admin/api/release/publish", {"artifact_path": "/unused/hub.dmg"}),
    ("/admin/api/story/instances/mini/update", {}),
)


class ReleaseOriginTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"GATEWAY_API_KEY": "release-origin-test"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(api.app, base_url=ORIGIN,
                                 headers={"Authorization": "Bearer release-origin-test"})
        self.effects = []
        for target, name in ((story_releases, "publish"), (story_releases, "request_update"),
                             (release_rollout, "start"), (release_publisher, "publish")):
            mock = patch.object(target, name, return_value={})
            self.effects.append(mock.start())
            self.addCleanup(mock.stop)

    def test_missing_origin_refuses_every_publish_and_update_before_side_effects(self):
        for path, body in MUTATIONS:
            with self.subTest(path=path):
                result = self.client.post(path, json=body)
                self.assertEqual(result.status_code, 422, result.text)
                self.assertEqual(result.json()["code"], "GATEWAY_ORIGIN_REQUIRED")
        for effect in self.effects:
            effect.assert_not_called()

    def test_wrong_origin_refuses_even_with_valid_owner_key(self):
        for path, body in MUTATIONS:
            with self.subTest(path=path):
                result = self.client.post(path, json=body,
                                          headers={"X-Gateway-Origin": "http://other.test:8787"})
                self.assertEqual(result.status_code, 409, result.text)
                self.assertEqual(result.json()["code"], "GATEWAY_ORIGIN_MISMATCH")
        for effect in self.effects:
            effect.assert_not_called()

    def test_matching_helper_and_browser_origins_allow_existing_actions(self):
        for headers in ({"X-Gateway-Origin": ORIGIN}, {"Origin": ORIGIN}):
            for path, body in MUTATIONS:
                with self.subTest(headers=headers, path=path):
                    result = self.client.post(path, json=body, headers=headers)
                    self.assertEqual(result.status_code, 200, result.text)
        for effect in self.effects[:3]:
            self.assertEqual(effect.call_count, 2)

    def test_malformed_duplicate_and_cross_scheme_origins_do_not_publish(self):
        bad = ["null", ORIGIN + "/admin", ORIGIN + "?", ORIGIN + "#fragment",
               "http://secret:do-not-echo@gateway.test:8787", "http://gateway.test:bad",
               "http://gate\tway.test:8787"]
        for value in bad:
            with self.subTest(value=value):
                result = self.client.post(MUTATIONS[0][0], json=MUTATIONS[0][1],
                                          headers={"X-Gateway-Origin": value})
                self.assertEqual(result.status_code, 422, result.text)
                self.assertEqual(result.json()["code"], "GATEWAY_ORIGIN_INVALID")
                self.assertNotIn("secret", result.text)
        duplicate = self.client.post(MUTATIONS[0][0], json=MUTATIONS[0][1],
                                     headers=[("X-Gateway-Origin", ORIGIN),
                                              ("X-Gateway-Origin", ORIGIN)])
        self.assertEqual(duplicate.status_code, 422)
        cross_scheme = self.client.post(MUTATIONS[0][0], json=MUTATIONS[0][1],
                                        headers={"X-Gateway-Origin": "https://gateway.test:8787"})
        self.assertEqual(cross_scheme.status_code, 409)
        for effect in self.effects:
            effect.assert_not_called()

    def test_default_port_is_equivalent_but_referer_alone_is_not_a_binding(self):
        client = TestClient(api.app, base_url="http://gateway.test", headers=self.client.headers)
        result = client.post(MUTATIONS[0][0], json=MUTATIONS[0][1],
                             headers={"X-Gateway-Origin": "http://gateway.test:80/"})
        self.assertEqual(result.status_code, 200)
        result = client.post(MUTATIONS[0][0], json=MUTATIONS[0][1],
                             headers={"Referer": "http://gateway.test/admin"})
        self.assertEqual(result.status_code, 422)


class GatewayHelperTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / "scripts" / "gw.py"
        spec = importlib.util.spec_from_file_location("release_gw_helper", source)
        self.gw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gw)

    def test_unbound_or_invalid_origin_reads_no_credentials_and_sends_nothing(self):
        for origin in (None, "", " ", "http://example.test/path", "http://secret:bad@example.test",
                       "http://example.test:bad", "http://example.test?x=1", "file:///tmp"):
            with self.subTest(origin=origin), patch.dict(os.environ, {}, clear=True):
                if origin is not None:
                    os.environ["KH_GATEWAY_URL"] = origin
                with patch.object(self.gw, "key") as key, \
                        patch.object(self.gw.urllib.request, "urlopen") as send:
                    with self.assertRaisesRegex(ValueError, "KH_GATEWAY_URL"):
                        self.gw.call("POST", MUTATIONS[0][0], MUTATIONS[0][1])
                    key.assert_not_called()
                    send.assert_not_called()

    def test_bound_origin_is_sent_with_the_request_and_does_not_fall_back(self):
        with patch.dict(os.environ, {"KH_GATEWAY_URL": ORIGIN + "/"}), \
                patch.object(self.gw, "key", return_value="test-key"), \
                patch.object(self.gw.urllib.request, "urlopen") as send:
            send.return_value.__enter__.return_value.status = 200
            send.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
            self.assertEqual(self.gw.call("POST", MUTATIONS[0][0], {}), (200, {"ok": True}))
            request = send.call_args.args[0]
            self.assertEqual(request.full_url, ORIGIN + MUTATIONS[0][0])
            self.assertEqual(request.get_header("X-gateway-origin"), ORIGIN)

    def test_missing_key_and_absolute_paths_are_loud_without_network(self):
        with patch.dict(os.environ, {"KH_GATEWAY_URL": ORIGIN}), \
                patch.object(self.gw.urllib.request, "urlopen") as send:
            with patch("builtins.open", return_value=io.StringIO("OTHER=value\n")):
                with self.assertRaisesRegex(RuntimeError, "API key"):
                    self.gw.call("POST", MUTATIONS[0][0], {})
            with patch.object(self.gw, "key") as key:
                for path in ("https://other.test/", "//other.test/", ""):
                    with self.assertRaisesRegex(ValueError, "path"):
                        self.gw.call("POST", path, {})
                key.assert_not_called()
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
