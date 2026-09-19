"""Which build of the gateway is actually running.

A stale gateway serving old code through a rebuild is invisible without this:
measured, one ran for two hours across four rebuilds because the app *adopts*
whatever already answers `/health` instead of spawning a new one. The app compares
this value with its own stamp and can say so out loud.

The stamp is written by `menubar/build.sh` into `BUILD`, beside the `gateway`
package inside the app bundle. Two sources, deliberately:

  * `GATEWAY_BUILD`, which the app passes when it spawns — authoritative, because
    it names the app that started this process;
  * the `BUILD` file next to the package, for a gateway started any other way
    (a test, or by hand).

Neither is invented: if neither is present the answer is `unknown`, not a guess.
"""

from __future__ import annotations

import os
from pathlib import Path

_CACHE: dict[str, str] = {}


def build_id() -> str:
    """The running gateway's build stamp, or `"unknown"` when nothing says."""
    if "value" in _CACHE:
        return _CACHE["value"]

    value = (os.environ.get("GATEWAY_BUILD") or "").strip()
    if not value:
        # `BUILD` sits beside the package: <bundle>/gateway/BUILD.
        candidate = Path(__file__).resolve().parent.parent / "BUILD"
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
    _CACHE["value"] = value or "unknown"
    return _CACHE["value"]
