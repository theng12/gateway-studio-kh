# Handoff verification record — 2026-09-20

[MEASURED] Product-source baseline `fcfde43`; source fixes in `91b45db`. This handoff changes docs, diagnostic files and Git hygiene, not production code.

| Local command/check | Observed result |
|---|---|
| `.venv/bin/python tests/test_e2e.py` | 521 checks passed; isolated mock upstream. |
| `.venv/bin/python tests/test_audit_regressions.py` | 6 tests passed. |
| `.venv/bin/python tests/test_job_claim.py` | 2 tests passed. |
| Swift compilation/run command in the main handoff | Native offscreen Create state check passed, including the delayed-refresh/submission-error regression. |
| `uv pip check --python .venv/bin/python` | 23 installed packages compatible. This is not a vulnerability audit. |
| `audit_probes.py` vs recorded JSON | Outputs matched five reproduced backend defects. A diagnostic match is not a passing fix test. |
| `console_action_probe.js` vs recorded JSON | Two successful mocked requests produced the recorded Generate/Keep type errors. |
| Handoff link/quote validation | 64 local links resolve; 57 exact source quotations match their lines; 30 distinct issue IDs. |
| `git diff --check` | Passed before the handoff commit. |

[MEASURED] Source-machine tools: Python 3.11.15; Apple Swift 6.4, Swift 5 language mode for the app checks; arm64-apple-macosx26.0 compiler target; Node v24.15.0 for the optional offline console diagnostic. These are observed versions, not a claim that they are the minimum supported toolchain.

[MEASURED] Previous artifact: `91b45db-202609201216`, built with `./menubar/build.sh --no-install`; bundled source comparison, isolated API import (55 routes), and `codesign --verify --deep --strict` passed. It was not installed/activated. No rebuild is claimed for documentation-only changes in this handoff.

[MEASURED] Before initial GitHub upload, a local scan checked 602 historical blobs (20,060,913 bytes) for selected secret patterns and exact matches to current gateway secrets, without printing secret values. It found no matches. This was not an exhaustive secret or vulnerability scan. Runtime/private data and build caches are not part of the source upload; the existing tracked voice metadata remains source and is called out in portability issue P01.

[MEASURED] GitHub repository was created private under `theng12/gateway-kh`; Actions were disabled and read back as `enabled: false` before source pushes. The final PR/merge and clone comparison are reported in the task result; the exact final commit cannot be embedded into its own file.

Not exercised: full installed-app UI suite, real-Hub fleet suite, paid/provider generation breadth, load/stress, actual public bind exposure, owner-data migration or Content Pack installation. See the handoff/ledger for the next checks rather than treating passing existing tests as bug-freedom.
