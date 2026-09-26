#!/usr/bin/env python3
"""Prove a *standalone* Studio Hub works as a fleet location.

The owner's new machine will be set up standalone first and only later join as a
controller. So the gateway must not care which role a location reports.

This runs a real gateway from the checkout against an isolated data directory,
adds the **local standalone Hub** as a location ("Site A"), discovers its models
through the fleet provider path, enables one, and transcribes a real clip end to
end. Nothing is stubbed: the Hub does the work.

This is NOT an offline smoke test. It may run real generation on the owner's Hub.
Run only with explicit generation authorization:
GATEWAY_ALLOW_LIVE_GENERATION=1 .venv/bin/python tests/test_fleet_standalone.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
GW_KEY = "fleet-standalone-test"
READY = Path.home() / "Library/Application Support/StudioHub Standalone/hub-ready.json"

#: Every audio type the gateway or the Hub actually writes. **The old search accepted
#: only `.mp3` under the artifacts dir, and this gateway writes `.wav` for every TTS
#: artifact** — measured 2026-09-26: 4 `.wav`, 0 `.mp3`. That half of the search could
#: never match, so this gate only ever ran when somebody had hand-placed a fixture in
#: `repository/data/`, and on a machine without one it stopped at section 3 with
#: `a real clip is available to transcribe — None`. A release gate that silently
#: cannot find its own input is a gate that stops being run.
_CLIP_SUFFIXES = (".wav", ".mp3", ".m4a", ".flac", ".aiff", ".aif")


def find_transcription_clip() -> Path | None:
    """The **smallest** real clip available, or `None`.

    Smallest rather than first, and that is the second half of the same defect:
    `next(glob(...))` returned whatever the filesystem happened to order first, and
    this artifacts dir holds a 298-second render beside a 3-second one. The gate's
    runtime was therefore a lottery decided by directory order, and a gate whose
    duration varies by two orders of magnitude is one people stop running. Size is
    also the honest metric here — it is the bytes that get uploaded and transcribed.
    """
    candidates: list[Path] = []
    for folder in (ROOT / "data",
                   Path.home() / "Library/Application Support/gateway-kh/data/artifacts"):
        if folder.is_dir():
            candidates.extend(p for p in folder.iterdir()
                              if p.is_file() and p.suffix.lower() in _CLIP_SUFFIXES)
    return min(candidates, key=lambda p: p.stat().st_size) if candidates else None


failures: list[str] = []
checks = 0

#: Every status this suite ever sees. The 402 rule is the strongest contract in
#: the repo — Story Studio holds a credit lock keyed on the API key, sees one 402,
#: and the key is dead permanently — and this suite used to assert it with
#: `check("no 402 anywhere", True)`, a literal. It claimed to uphold the rule
#: while measuring nothing. Recording happens in one place, an httpx response
#: hook on the shared client, so a route added later is covered by default
#: rather than by whoever remembers.
statuses: set[int] = set()


def _record(response: "httpx.Response") -> None:
    statuses.add(response.status_code)


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    tag = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    print(f"  {tag} {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


skipped: list[str] = []


def skip(label: str, reason: str) -> None:
    """A check whose *environment* is missing, with the reason stated.

    This Hub is the owner's live install and is sometimes mid-rebuild — measured
    while `packaging/stage_voice.py --runtime` was staging the runtimes, the Hub
    answered `fleet/overview`, `capabilities`, `stats` and `settings` with 200 and
    `/api/hub/model-packages` with `{"models": []}`, so nothing could be discovered
    from it. That is not a gateway defect and reporting it as one teaches the
    reader to ignore failures. Reachability, credentials and the role checks still
    run; only the model-dependent ones step aside.
    """
    skipped.append(label)
    print(f"  \033[33mSKIP\033[0m {label} — {reason}")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def main() -> int:
    if os.environ.get("GATEWAY_ALLOW_LIVE_GENERATION") != "1":
        print("SKIP: live Hub generation requires explicit GATEWAY_ALLOW_LIVE_GENERATION=1 opt-in.")
        return 0
    if not READY.exists():
        print("Studio Hub is not running; this test needs it.")
        return 1
    hub = json.loads(READY.read_text())
    hub_url, hub_token = hub["url"].rstrip("/"), hub["token"]

    # Does this Hub actually have any model packages installed? It is the owner's
    # live install, so it is sometimes mid-rebuild, and a Hub that answers but
    # offers nothing cannot prove anything about discovery or dispatch. Asked once,
    # up front, so the reason is stated rather than inferred from nine failures.
    packaged = 0
    try:
        probe = httpx.get(f"{hub_url}/api/hub/model-packages",
                          headers={"X-Hub-Token": hub_token}, timeout=10)
        if probe.status_code == 200:
            packaged = len((probe.json() or {}).get("models") or [])
    except (httpx.HTTPError, ValueError):
        packaged = 0
    no_models = packaged == 0

    tmp = Path(tempfile.mkdtemp(prefix="gateway-fleet-"))
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    auth = {"Authorization": f"Bearer {GW_KEY}"}

    env = {
        **os.environ,
        "GATEWAY_DATA": str(tmp / "data"),
        "GATEWAY_APP_SUPPORT": str(tmp),
        "GATEWAY_API_KEY": GW_KEY,
        "GATEWAY_NO_MIGRATE": "1",
        "PATH": os.environ["PATH"],
    }
    server = subprocess.Popen(
        [PY, "-m", "uvicorn", "gateway.api:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                probe = httpx.get(f"{base}/health", timeout=2)
                statuses.add(probe.status_code)
                if probe.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.4)
        else:
            print("gateway did not start")
            return 1

        client = httpx.Client(timeout=40, headers=auth,
                              event_hooks={"response": [_record]})

        print("\n\033[1m1. A standalone Hub is accepted as a location\033[0m")
        added = client.post(f"{base}/admin/api/fleet/controllers", json={
            "label": "Site A", "url": hub_url, "token": hub_token,
        })
        check("a location can be added", added.status_code == 200, added.text[:200])
        controller_id = added.json()["controller"]["id"]
        provider_id = f"studio-hub-{controller_id}"

        snap = client.get(f"{base}/admin/api/fleet").json()
        site = next(l for l in snap["locations"] if l["id"] == controller_id)
        check("the location is reachable", site["reachable"], str(site.get("error"))[:160])
        # The Hub's vocabulary is standalone | controller | agent — there is no
        # `worker`. Identity comes from `capabilities.controller`, because
        # `fleet/nodes.role` used to return a constant and reading it showed the
        # wrong role for a standalone Mac.
        check("its role is accepted as reported, whatever it is",
              site["role"] in {"standalone", "controller", "agent"},
              f"role={site['role']}")
        check("the location publishes stable identity, not just a label",
              bool(site.get("site_id")) and bool(site.get("controller_id")),
              f"site_id={site.get('site_id')} controller_id={site.get('controller_id')}")
        print(f"       reported role: {site['role']}")
        check("it reports machines", len(site["machines"]) >= 1)
        # A Hub lists its own connectivity probes as machines — `probe-fleet` and
        # `probe-hub`, both labelled "probe" and permanently unreachable. Counting
        # them made this Mac read as "3 machines, 2 offline", which is the exact
        # figure the owner wants the Fleet page for; at six sites it would read as
        # ~12 broken machines when nothing is wrong.
        probes = [m for m in site["machines"] if str(m.get("machine") or "").startswith("probe-")]
        check("the Hub's own probe entries are not reported as the owner's machines",
              not probes, f"{[m.get('machine') for m in probes]}")
        if site.get("probe_machines_hidden"):
            check("and the page is told how many were hidden, so nothing vanishes silently",
                  site["probe_machines_hidden"] == 2,
                  f"hidden={site['probe_machines_hidden']}")
            check("the machine total agrees with the machines that remain",
                  (site.get("machine_summary") or {}).get("total") == len(site["machines"]),
                  f"summary={site.get('machine_summary')} listed={len(site['machines'])}")
            check("and the offline count is not inflated by them",
                  site["machine_summary"].get("unreachable", 0)
                  == sum(1 for m in site["machines"] if not m.get("reachable")),
                  f"{site['machine_summary']}")
        check("the location also became a provider",
              provider_id in [p["id"] for p in
                              client.get(f"{base}/admin/api/providers").json()["providers"]])

        print("\n\033[1m2. Its models are discovered through that provider\033[0m")
        # Adding discovered them: nothing refreshes models on a timer, so an added
        # location that showed none would read as "it has nothing" rather than
        # "nobody asked it yet".
        #
        # Both names are bound before the branch, not inside it: `models` and
        # `stt_models` are used again by later sections, and defining them only in
        # the `else` left them unbound whenever the Hub had nothing installed —
        # an `UnboundLocalError` that replaced the honest skip with a crash.
        models: list[dict] = []
        stt_models: list[str] = []
        if no_models:
            skip("adding a location discovers its models",
                 "this Hub reports 0 model packages installed, so there is nothing to discover")
            skip("models discovered from the location", "the Hub offers no model packages")
            skip("transcription models are among them", "the Hub offers no model packages")
        else:
            check("adding a location discovers its models",
                  isinstance(added.json().get("discovered"), int)
                  and added.json().get("discovered") > 0,
                  f"discovered={added.json().get('discovered')} err={added.json().get('discovery_error')}")
            described = client.get(f"{base}/admin/api/providers/{provider_id}/models").json()
            models = described.get("models") or []
            check("models discovered from the location", len(models) > 0, f"{len(models)}")
            stt_models = [m["model_id"] for m in models if m.get("modality") == "transcription"]
            check("transcription models are among them", bool(stt_models), f"{len(models)} models")

        print("\n\033[1m2c. The credential is checked, and can be replaced\033[0m")
        # Both of a controller's credentials reach every route this gateway calls,
        # so pasting the over-privileged one is silent. This Hub's own token IS the
        # operator token, so the check has something true to report.
        baseline_ids = [m["model_id"] for m in models]
        added_scope = added.json().get("credential") or {}
        check("adding a location reports what its credential is",
              added_scope.get("scope") in {"operator", "fleet"},
              f"{added_scope}")
        check("the Hub's own token is reported as the operator token",
              added_scope.get("scope") == "operator",
              f"scope={added_scope.get('scope')} detail={str(added_scope.get('detail'))[:90]}")

        still_same = client.get(f"{base}/admin/api/providers/{provider_id}/models").json()
        replaced = client.put(
            f"{base}/admin/api/fleet/controllers/{controller_id}/credential",
            json={"token": hub_token},
        )
        check("a credential can be replaced", replaced.status_code == 200, replaced.text[:200])
        check("replacing it reports the new credential's scope",
              (replaced.json().get("credential") or {}).get("scope") == "operator",
              str(replaced.json().get("credential"))[:120])
        check("replacing it leaves the discovered models alone",
              [m["model_id"] for m in still_same.get("models") or []] == baseline_ids)
        # A refresh must still work on the replaced credential, or "replaced"
        # would only mean the store changed.
        check("the replaced credential is the one in use",
              client.post(f"{base}/admin/api/providers/{provider_id}/refresh").status_code == 200)

        bad = client.put(f"{base}/admin/api/fleet/controllers/{controller_id}/credential",
                         json={"token": "definitely-not-a-valid-token"})
        bad_scope = (bad.json().get("credential") or {}).get("scope")
        check("a rejected credential is not mistaken for a usable one",
              bad.status_code == 200 and bad_scope == "rejected", f"scope={bad_scope}")
        # Put the working credential back before anything else runs.
        client.put(f"{base}/admin/api/fleet/controllers/{controller_id}/credential",
                   json={"token": hub_token})
        check("an empty credential is refused",
              client.put(f"{base}/admin/api/fleet/controllers/{controller_id}/credential",
                         json={"token": "   "}).status_code == 400)
        check("this Mac's credential is not replaceable from here",
              client.put(f"{base}/admin/api/fleet/controllers/local/credential",
                         json={"token": "x"}).status_code == 400)

        print("\n\033[1m2b. Renaming a location changes nothing but its name\033[0m")
        # The owner's requirement: name any controller, at any time. A name is a
        # display label, so this asserts the *identity* is untouched -- the same
        # provider id, the same discovered models, the same working credential.
        before_ids = [m["model_id"] for m in models]
        renamed = client.patch(f"{base}/admin/api/fleet/controllers/{controller_id}",
                               json={"label": "Angkor Hub"})
        check("a location can be renamed", renamed.status_code == 200, renamed.text[:200])
        check("the roster carries the new name",
              any(c["id"] == controller_id and c["label"] == "Angkor Hub"
                  for c in renamed.json()["controllers"]),
              str(renamed.json().get("controllers"))[:200])
        check("its provider id is not derived from its name",
              provider_id in [p["id"] for p in
                              client.get(f"{base}/admin/api/providers").json()["providers"]])
        after = client.get(f"{base}/admin/api/providers/{provider_id}/models").json()
        check("its discovered models are unchanged by the rename",
              [m["model_id"] for m in after.get("models") or []] == before_ids,
              f"{(after.get('models') or [])[:2]}")
        check("it is still reachable, so the rename did not disturb its credential",
              next(l for l in client.get(f"{base}/admin/api/fleet").json()["locations"]
                   if l["id"] == controller_id)["reachable"])
        check("an empty name is refused",
              client.patch(f"{base}/admin/api/fleet/controllers/{controller_id}",
                           json={"label": "   "}).status_code == 400)
        check("an unknown location answers 404 rather than crashing",
              client.patch(f"{base}/admin/api/fleet/controllers/does-not-exist",
                           json={"label": "x"}).status_code == 404)
        check("this Mac can be renamed too",
              client.patch(f"{base}/admin/api/fleet/controllers/local",
                           json={"label": "HQ Mac mini"}).status_code == 200
              and next(l for l in client.get(f"{base}/admin/api/fleet").json()["locations"]
                       if l["id"] == "local")["label"] == "HQ Mac mini")

        print("\n\033[1m3. Real work runs on that location\033[0m")
        # Try the Hub's transcription models in turn. A model the Hub itself
        # cannot run (a missing Subtitle Studio snapshot config, say) is the
        # Hub's problem, not the routing's — so the test looks for one that
        # works rather than blaming the first failure on the fleet path.
        clip = find_transcription_clip()
        check("a real clip is available to transcribe", clip is not None, str(clip))
        if not clip:
            return 1
        print(f"       using {clip.name} ({clip.stat().st_size:,} bytes)")

        client.post(f"{base}/admin/api/providers/{provider_id}/models/enabled",
                    json={"model_ids": stt_models, "enabled": True})

        # A *second* location offering the same models, so the merge path is
        # genuinely exercised. It is the same Hub reached by another address —
        # which the API is now **right to refuse as a duplicate**, because a
        # controller is identified by the Hub's own stable `site_id`, not by where
        # it happens to answer. A hand-written roster row is a supported input
        # (the file is documented), so the second location is written directly:
        # two rows the roster cannot tell apart by identity, which is exactly the
        # merge case. The alias POST is checked separately, below.
        alias_url = hub_url.replace("127.0.0.1", "localhost")
        second_id = "siteb00000001"
        roster_path = tmp / "data" / "fleet.json"
        roster = json.loads(roster_path.read_text())
        roster.setdefault("controllers", []).append({
            "id": second_id, "label": "Site B", "url": alias_url,
            "added_at": time.time(),
        })
        roster_path.write_text(json.dumps(roster, indent=2))
        os.chmod(roster_path, 0o600)
        # The roster carries no secrets, so its credential goes in through the API
        # — which also proves the by-id route works for a row it did not create.
        # `/credential` is the *dispatch* credential (`hub_token`); `/hub-token` is
        # the operator token used only for shared voices, and reaching for it here
        # is what made this location answer 401 "Hub token required for remote
        # access" while the refresh still reported HTTP 200.
        credited = client.put(
            f"{base}/admin/api/fleet/controllers/{second_id}/credential",
            json={"token": hub_token})
        check("a location written straight into the roster can be given a credential",
              credited.status_code == 200, f"{credited.status_code} {credited.text[:160]}")
        second_provider = f"studio-hub-{second_id}"
        check("the second location is a distinct provider, not the same one",
              second_provider != provider_id)
        # Every location row publishes the provider its models are managed under,
        # derived from the controller id — so a client never has to build it from
        # the label, and the Fleet page's Refresh can act on the right thing.
        _rows = client.get(f"{base}/admin/api/fleet").json()["locations"]
        check("each location publishes the provider id derived from its controller id",
              bool(_rows) and all(r.get("provider_id") == f"studio-hub-{r.get('id')}"
                                  for r in _rows),
              f"{[(r.get('id'), r.get('provider_id')) for r in _rows]}")
        check("and that is never the location id itself",
              all(r.get("provider_id") != r.get("id") for r in _rows),
              f"{[(r.get('id'), r.get('provider_id')) for r in _rows]}")
        refreshed = client.post(f"{base}/admin/api/providers/{second_provider}/refresh")
        enabled_second = client.post(
            f"{base}/admin/api/providers/{second_provider}/models/enabled",
            json={"model_ids": stt_models, "enabled": True})
        second_models = client.get(
            f"{base}/admin/api/providers/{second_provider}/models").json()
        # **A second location that contributes nothing makes every merge check
        # below vacuous.** This is asserted rather than assumed: the roster row was
        # written outside the API, so the gateway has to have picked it up, its
        # credential has to resolve, and its models have to have been discovered —
        # `set_many` reports the count it was *given*, not the count it wrote.
        check("the second location's models really are discovered and enabled",
              refreshed.status_code == 200
              and enabled_second.status_code == 200
              and bool(second_models.get("models")),
              f"refresh {refreshed.status_code} {refreshed.text[:120]} "
              f"models={len(second_models.get('models') or [])} "
              f"error={second_models.get('last_error') or second_models.get('error')}")
        catalogue = client.get(f"{base}/v1/models").json()["models"]

        print("\n\033[1m3. Locations publish one row per model, under the maker\033[0m")
        # A location is a provider internally but not a separate product. Two
        # locations offering Whisper must be ONE row; before this the picker held
        # the same display_name twice and a saved preset broke when its site went
        # offline. Namespace by maker, never by site.
        check("no published id names either location",
              not any(provider_id in m["id"] or second_provider in m["id"] for m in catalogue),
              f"{[m['id'] for m in catalogue if provider_id in m['id'] or second_provider in m['id']][:3]}")
        # **Derived from what the Hub actually offers, never named.** This asked for
        # `mlx-community/whisper-base-mlx` by name, and on 2026-09-26 that model was
        # `not_installed` on the running Hub — so the gateway **correctly** withheld it
        # as unservable, `dupes` came back empty, and one absent model cascaded into
        # five failures: a transcription that never ran, and three checks that passed
        # **vacuously** because `all(...)` over an empty set is `True`. The test was
        # quoting a moving part; the Hub was telling the truth. This is the same defect
        # the tracker already records for a hardcoded Hub version — *"a test is a row,
        # and it lies the same way"* — and the remedy is the same: read the fixture.
        dupes = [m["id"] for m in catalogue
                 if m["id"].startswith("studio-hub::") and "whisper" in m["id"].lower()]
        if no_models or not dupes:
            # The merge itself is proven above by "no published id names either
            # location" — these three name the specific Whisper row, which cannot
            # exist on a Hub that offers no dispatchable one.
            for label in ("a model offered by two locations is published once",
                          "the row is attributed to the maker, not the site",
                          "a superseded site-scoped id survives as an alias, from every location"):
                skip(label, "this Hub reports 0 model packages installed" if no_models
                     else "this Hub offers no dispatchable speech-to-text model")
            check("no model reaches Story Studio from an empty Hub", not dupes, f"{dupes}")
        else:
            # The property is *one row per model*, so asserting it means no id appears
            # twice — not that exactly one Whisper exists. The old `len(dupes) == 1`
            # would go red the moment a second Whisper was installed, which is a
            # change on the Hub and not a defect here.
            check("a model offered by two locations is published once",
                  bool(dupes) and len(dupes) == len(set(dupes)), f"{dupes}")
            check("the row is attributed to the maker, not the site",
                  bool(dupes) and all(m.get("provider_name") == "Studio Hub"
                                      for m in catalogue if m["id"] in dupes),
                  f"{[(m['id'], m.get('provider_name')) for m in catalogue if m['id'] in dupes]}")
            # The id genuinely changed (`studio-hub-local::X` -> `studio-hub::X`), and
            # Story Studio matches a stored selection against every legacy id, so
            # carrying the superseded ones is what stops a saved preset reading as
            # `vanished` across this change. BOTH locations' old ids must survive —
            # **on every merged row**, not on whichever one happened to be looked at.
            missing = {
                m["id"]: [str(a) for a in (m.get("legacy_ids") or [])]
                for m in catalogue if m["id"] in dupes
                and not (any(a.startswith(f"{provider_id}::") for a in (m.get("legacy_ids") or []))
                         and any(a.startswith(f"{second_provider}::") for a in (m.get("legacy_ids") or [])))
            }
            check("a superseded site-scoped id survives as an alias, from every location",
                  not missing, f"{missing}")

        story_ids = [m["id"] for m in catalogue
                     if m["id"].startswith("studio-hub::") and m["id"] in dupes]
        if no_models:
            skip("they reach the Story Studio catalogue", "no model packages on this Hub")
        else:
            check("they reach the Story Studio catalogue", bool(story_ids), f"{story_ids}")

        succeeded = None
        hub_errors: list[str] = []
        echoed: list[tuple[str, str]] = []
        for story_id in story_ids:
            with open(clip, "rb") as handle:
                quote = client.post(
                    f"{base}/v1/audio/transcriptions/quotes",
                    files={"file": (clip.name, handle.read(), "audio/wav")},
                    data={"model": story_id},
                )
            if quote.status_code != 200:
                hub_errors.append(f"{story_id}: quote HTTP {quote.status_code}")
                continue
            confirm = client.post(
                f"{base}/v1/audio/transcriptions/quotes/{quote.json()['id']}/confirm",
                headers={"Idempotency-Key": f"fleet-stt-{int(time.time())}"},
                data={"model": story_id},
            )
            check("the location accepts the job", confirm.status_code == 202, confirm.text[:200])
            if confirm.status_code != 202:
                continue
            # The job is routed to a concrete location, but the id Story Studio is
            # told back has to be the id it sent or its saved preset stops
            # matching its own jobs. This is the assertion that catches a routed
            # provider id leaking into the response.
            echoed.append((story_id, str(confirm.json().get("model_id") or "")))
            job_id = confirm.json().get("id")
            state, done = "unknown", {}
            deadline = time.time() + 240
            while time.time() < deadline:
                done = client.get(f"{base}/v1/transcriptions/{job_id}").json()
                state = done.get("state", "")
                if state in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(2)
            if state == "succeeded":
                succeeded = (story_id, job_id)
                break
            hub_errors.append(f"{story_id}: {(done.get('failure_reason') or state)[:120]}")

        if no_models:
            skip("a transcription ran on the location end to end",
                 "this Hub has no transcription model installed to run one")
            skip("the id Story Studio sent is the id it is told back",
                 "no job was submitted, because the Hub has no model to run it on")
            skip("the artifact comes back through the gateway",
                 "no job ran, so there is no artifact to fetch")
        else:
            check("a transcription ran on the location end to end", succeeded is not None,
                  " | ".join(hub_errors)[:300])
            check("the id Story Studio sent is the id it is told back",
                  bool(echoed) and all(sent == back for sent, back in echoed), f"{echoed}")
            if succeeded:
                story_id, job_id = succeeded
                print(f"       ran {story_id}")
                srt = client.get(f"{base}/v1/transcriptions/{job_id}/srt")
                check("the artifact comes back through the gateway",
                      srt.status_code == 200 and b"-->" in srt.content,
                      f"HTTP {srt.status_code}")
            else:
                # Not a pass, and not silently skipped: say whose fault it is.
                # **"the Hub refused every model" is only true if there were models to
                # refuse.** With `story_ids` empty this printed a colon and nothing
                # after it, which read as a Hub refusal and was actually "nothing was
                # ever submitted" — the two must not share a sentence.
                print(("       the Hub refused every transcription model it offers: "
                       if story_ids else
                       "       no transcription model reached the catalogue, so nothing was submitted: ")
                      + (" | ".join(hub_errors)[:300] or f"(no attempts; story_ids={story_ids})"))
                check("the artifact comes back through the gateway", False, "no job succeeded")

        print("\n\033[1m5. Nothing was gated on the role\033[0m")
        # **A controller is identified by the controller, not by its address.** The
        # Hub states a stable `site_id`, so re-adding Site A at its `localhost`
        # alias must land on the existing row. Matching the address alone minted a
        # second location — orphaning the first one's credential and every
        # per-model enable decision with it — which is exactly what the roster row
        # above had to be written by hand to simulate.
        alias = client.post(f"{base}/admin/api/fleet/controllers", json={
            "label": "Angkor Hub (alias)", "url": alias_url, "token": hub_token,
        })
        check("re-adding the same controller at another address is the same location",
              alias.status_code == 200
              and alias.json()["controller"]["id"] == controller_id
              and alias.json()["controller"].get("site_id"),
              f"{alias.status_code} {alias.text[:200]}")
        check("and no third location was minted for the alias",
              len(client.get(f"{base}/admin/api/fleet/controllers").json()["controllers"]) == 2,
              f"{[c['id'] for c in client.get(f'{base}/admin/api/fleet/controllers').json()['controllers']]}")        # Both locations are local-scope providers because they are machines, not
        # cloud APIs. Asserting the property rather than a count: the count moves
        # whenever a location is added, and a hardcoded one fails for the wrong
        # reason.
        local_ids = {
            p["id"] for p in
            client.get(f"{base}/admin/api/providers?scope=local").json()["providers"]
        }
        cloud_ids = {
            p["id"] for p in
            client.get(f"{base}/admin/api/providers?scope=cloud").json()["providers"]
        }
        check("both locations are filed as local, and neither as cloud",
              {provider_id, second_provider} <= local_ids
              and not ({provider_id, second_provider} & cloud_ids),
              f"local has {len(local_ids)}, cloud has {len(cloud_ids)}")
        # Not a formality. A 402 from *any* route in this run would kill the key
        # Story Studio calls with, and the fleet routes are the ones that talk to
        # a real Hub, so this is where an upstream refusing work is most likely to
        # surface as the wrong status.
        check("no response in the whole run was 402", 402 not in statuses,
              f"statuses seen {sorted(statuses)}")
        check("and the run actually recorded responses", len(statuses) > 0,
              f"statuses seen {sorted(statuses)}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    if skipped:
        print(f"\033[33m{len(skipped)} skipped\033[0m (environment, not the gateway):")
        for name in skipped:
            print(f"  - {name}")
    if failures:
        print(f"\033[31m{len(failures)} of {checks} fleet checks FAILED\033[0m")
        for name in failures:
            print(f"  - {name}")
        return 1
    print(f"\033[32mAll {checks} fleet checks passed.\033[0m")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
