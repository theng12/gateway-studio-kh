"""Gate entry point for the audited MUSIC-OUTPUT-FORMAT regression.

The audited artifact is `tests/audit_regression_music_output_format.py` — byte for byte the file
handed off in the bundle (`sha256 d924705e…`). It is deliberately named so the gate's
`tests/test_*.py` discovery does not run it directly, because it requires an explicit
`GATEWAY_REPO` and has **no fallback** — that guard is the point: it must never be pointed at a
live or main checkout by accident.

The offline gate runs every child with a stripped environment (`PATH`, `HOME`, `TMPDIR`, … and
nothing else), so `GATEWAY_REPO` cannot reach it. This file supplies the one fact the gate
withholds: the tree this test lives in, which *is* the checkout under test. An explicit
`GATEWAY_REPO` still wins, and the artifact's own guard still refuses anything that is not a
gateway tree.

Standalone, exactly as the bundle says (the artifact runs on its own):

    GATEWAY_REPO=<tree> .venv/bin/python tests/audit_regression_music_output_format.py
"""
from __future__ import annotations

import os
import runpy
from pathlib import Path

os.environ.setdefault("GATEWAY_REPO", str(Path(__file__).resolve().parents[1]))

runpy.run_path(
    str(Path(__file__).with_name("audit_regression_music_output_format.py")),
    run_name="__main__",
)
