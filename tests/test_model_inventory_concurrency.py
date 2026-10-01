"""Toggling models while another thread saves must never raise "dictionary changed size"."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.TemporaryDirectory(prefix="gateway-inventory-concurrency-")
os.environ.setdefault("GATEWAY_APP_SUPPORT", _TMP.name)
os.environ.setdefault("GATEWAY_NO_MIGRATE", "1")

from gateway.providers.credentials import CredentialStore  # noqa: E402
from gateway.providers.models import ModelInventory  # noqa: E402


class _Trap(dict):
    """A dict that lets another thread grow it **while** it is being serialised.

    `json.dumps(indent=2)` iterates `.items()`; after the first item this hands control to a
    thread that adds a key (a model toggle), the exact interleaving that raised "dictionary
    changed size during iteration" when `_save` ran unlocked.
    """

    armed = False
    on_first = None

    def items(self):
        iterator = iter(super().items())
        first = next(iterator, None)
        if first is not None:
            yield first
            if self.armed:
                self.armed = False
                self.on_first()
        yield from iterator


class ModelInventoryConcurrencyTest(unittest.TestCase):
    def test_a_toggle_during_a_save_neither_raises_nor_is_lost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models_cache.json"
            inventory = ModelInventory(CredentialStore(), path=path)
            inventory.set_many("openai-direct", ["a", "b", "c"], True)
            trap = _Trap(inventory._data["enabled"]["openai-direct"])
            inventory._data["enabled"]["openai-direct"] = trap
            errors: list[BaseException] = []

            def toggle() -> None:
                try:
                    inventory.set_model_enabled("openai-direct", "late", True)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            def interleave() -> None:
                worker = threading.Thread(target=toggle)
                worker.start()
                worker.join(0.5)  # with the lock the toggle waits for the save; without, it lands mid-dump

            trap.on_first = interleave
            trap.armed = True
            inventory._save()  # RuntimeError here on the unlocked code
            for t in threading.enumerate():
                if t.name != "MainThread" and t.is_alive() and t is not threading.current_thread():
                    t.join(5)
            self.assertEqual(errors, [])
            saved = ModelInventory(CredentialStore(), path=path)
            self.assertIs(saved._data["enabled"]["openai-direct"].get("late"), True)


if __name__ == "__main__":
    unittest.main()
