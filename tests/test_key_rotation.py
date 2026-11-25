"""The API-key rotation: a manual press, a measured 24-hour overlap, then the old value stops.

The owner's words (2026-11-25): "the old and new values both work for 24 hours, so already-paired Macs
and extensions pick up the change; after that the old one stops." Two things this file exists to keep
true: the window is counted from the press rather than from first use (a Mac asleep through the
rotation still works when it wakes inside it), and an empty value is never a pass.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-key-rotation-"))
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT), "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
    "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"), "GATEWAY_NO_MIGRATE": "1",
})
os.environ.pop("GATEWAY_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import config  # noqa: E402

T0 = 1_800_000_000.0
HOUR = 3600.0


class RotationTest(unittest.TestCase):
    def setUp(self) -> None:
        config.ENV_PATH.write_text("GATEWAY_API_KEY=first-key\n# a comment that must survive\n"
                                   "GATEWAY_HOST=127.0.0.1\n", encoding="utf-8")
        config.secret_file_mode(config.ENV_PATH)
        self.addCleanup(lambda: config.ENV_PATH.unlink(missing_ok=True))
        os.environ.pop("GATEWAY_API_KEY", None)

    def rotate(self):
        return config.rotate_gateway_api_key(now=T0)

    def test_the_new_value_works_immediately_and_the_old_one_still_does(self) -> None:
        result = self.rotate()
        self.assertTrue(config.accepts_gateway_api_key(str(result["key"]), now=T0))
        self.assertTrue(config.accepts_gateway_api_key("first-key", now=T0 + 60))

    def test_the_old_value_works_all_the_way_to_the_edge_and_stops_after_it(self) -> None:
        self.rotate()
        self.assertTrue(config.accepts_gateway_api_key("first-key", now=T0 + 24 * HOUR - 60),
                        "a minute inside the window is still inside it")
        self.assertFalse(config.accepts_gateway_api_key("first-key", now=T0 + 24 * HOUR + 60),
                         "a minute after it, the old value is gone")
        self.assertTrue(config.accepts_gateway_api_key(
            config.gateway_api_key(), now=T0 + 24 * HOUR + 60), "the new value is unaffected")

    def test_a_mac_asleep_through_the_rotation_works_when_it_wakes_inside_the_window(self) -> None:
        # The window counts from the press, not from the first time anyone presents the old value.
        self.rotate()
        self.assertTrue(config.accepts_gateway_api_key("first-key", now=T0 + 23 * HOUR))
        self.assertFalse(config.accepts_gateway_api_key("first-key", now=T0 + 25 * HOUR))

    def test_an_empty_value_is_never_a_pass(self) -> None:
        self.rotate()
        for candidate in ("", "   ", None):
            self.assertFalse(config.accepts_gateway_api_key(candidate or "", now=T0))

    def test_the_rotation_leaves_every_other_line_alone(self) -> None:
        self.rotate()
        text = config.ENV_PATH.read_text(encoding="utf-8")
        self.assertIn("# a comment that must survive", text)
        self.assertIn("GATEWAY_HOST=127.0.0.1", text)
        self.assertNotIn("first-key", text.split("GATEWAY_API_KEY_PREVIOUS=")[0].split("\n")[0],
                         "the current key is the new one")

    def test_the_file_stays_owner_only(self) -> None:
        self.rotate()
        mode = stat.S_IMODE(config.ENV_PATH.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_a_key_from_the_process_environment_is_refused_rather_than_faked(self) -> None:
        os.environ["GATEWAY_API_KEY"] = "from-the-app"
        self.addCleanup(os.environ.pop, "GATEWAY_API_KEY", None)
        with self.assertRaises(RuntimeError) as refused:
            config.rotate_gateway_api_key(now=T0)
        self.assertIn("rotate from the app", str(refused.exception).lower())
        self.assertEqual(config.ENV_PATH.read_text(encoding="utf-8").count("GATEWAY_API_KEY="), 1,
                         "a refused rotation writes nothing")

    def test_v1_and_content_guards_honour_the_overlap_window(self) -> None:
        import time
        from types import SimpleNamespace
        from gateway import api

        result = config.rotate_gateway_api_key(now=time.time())
        new_key = str(result["key"])
        req = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        # Both new and previous keys pass /v1 and content auth while the window is open.
        for candidate in (new_key, "first-key"):
            api.require_auth(req, f"Bearer {candidate}")
            self.assertEqual(api.require_content_auth(f"Bearer {candidate}"), new_key)
        # Once the window closes, the old key is refused by both guards while the new one still works.
        config.rotate_gateway_api_key(now=T0)
        # Re-seed with first-key as expired previous and new_key as current.
        config.ENV_PATH.write_text(
            f"GATEWAY_API_KEY={new_key}\n"
            f"GATEWAY_API_KEY_PREVIOUS=first-key\n"
            f"GATEWAY_API_KEY_PREVIOUS_UNTIL={int(time.time()) - 60}\n"
            "GATEWAY_HOST=127.0.0.1\n",
            encoding="utf-8",
        )
        api.require_auth(req, f"Bearer {new_key}")
        self.assertEqual(api.require_content_auth(f"Bearer {new_key}"), new_key)
        with self.assertRaises(api.GatewayError) as exc1:
            api.require_auth(req, "Bearer first-key")
        self.assertEqual(exc1.exception.status, 403)
        with self.assertRaises(api.GatewayError) as exc2:
            api.require_content_auth("Bearer first-key")
        self.assertEqual(exc2.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
