"""gateway-kh — a personal model gateway.

One API in front of many backends: local Apple Silicon runners (Studio Hub) and
remote providers (fal.ai, Kie.ai, OpenRouter, DeepSeek, ...). It exists so that
Story Studio KH points at a single URL and its model pickers fill up, with no
stocking step, no credits and no per-model setup.
"""

from pathlib import Path


def _read_version() -> str:
    """The release number, from the one `VERSION` file the repo (and the app bundle) carries.

    `menubar/build.sh` copies it beside the package, so the running gateway and the checkout answer
    the same way. This was a hard-coded `0.0.1` that `/api/version` reported for every release.
    """
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


__version__ = _read_version()
