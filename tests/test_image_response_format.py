"""The gpt-image family rejects `response_format`; the dall-e family needs it."""
import sys, types, unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path("/build/Developer/gateway-kh/repository")
sys.path.insert(0, str(ROOT))
from gateway.adapters import openai_compat as oc
from gateway.adapters.base import BackendRequest

def req(model):
    return BackendRequest(provider_id="openai-direct", model_id=model, modality="image",
                          params={"prompt": "x"}, values={"api_key": "k"}, base_url="http://x")

class T(unittest.TestCase):
    def _body(self, model):
        seen = {}
        class R:
            status_code = 200
            text = ""
            def json(self): return {"data": [{"b64_json": "aGk="}]}
            def raise_for_status(self): pass
        def fake_post(url, headers=None, json=None, timeout=None):
            seen.update(json or {}); return R()
        with patch.object(oc.httpx, "post", fake_post):
            oc._image(req(model))
        return seen

    def test_gpt_image_does_not_send_response_format(self):
        for m in ("gpt-image-1", "gpt-image-2.5-sunburst-2026-09-08",
                  "gpt-image-2.5-flare-2026-09-08"):
            with self.subTest(model=m):
                self.assertNotIn("response_format", self._body(m),
                                 "the gpt-image family answers 400 for this parameter")

    def test_dall_e_still_asks_for_b64(self):
        for m in ("dall-e-2", "dall-e-3"):
            with self.subTest(model=m):
                self.assertEqual("b64_json", self._body(m).get("response_format"))

    def test_the_rest_of_the_body_is_unchanged(self):
        b = self._body("gpt-image-2.5-sunburst-2026-09-08")
        self.assertEqual("gpt-image-2.5-sunburst-2026-09-08", b["model"])
        self.assertEqual("x", b["prompt"])

if __name__ == "__main__":
    unittest.main(verbosity=1)
