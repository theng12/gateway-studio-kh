"""Explicit disposable account/quote grants for existing worker protocol regression fixtures."""
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


def iso(moment=None):
    return datetime.fromtimestamp(time.time() if moment is None else moment, timezone.utc).isoformat()


def observed_account(account_id="fixture-account", **changes):
    value = {"schema": 1, "provider": "flow", "accountId": account_id,
             "source": "fixture.provider", "observedAt": iso(), "connection": "connected",
             "signIn": "signed_in", "readiness": "ready", "activity": "idle",
             "attention": False, "enabled": True, "paused": False,
             "nextEligibleAt": iso(time.time() - 1),
             "balance": {"value": 100, "unit": "credits", "source": "fixture.balance", "observedAt": iso()},
             "resetAt": None}
    value.update(changes)
    return value


def isolated_store(case):
    from gateway import api, runtime
    from gateway.store import JobStore

    temp = tempfile.TemporaryDirectory(prefix="gateway-account-fixture-")
    case.addCleanup(temp.cleanup)
    store = JobStore(Path(temp.name) / "jobs.sqlite")
    case.addCleanup(store.close)
    for module in (runtime, api):
        scope = patch.object(module, "store", store)
        scope.start()
        case.addCleanup(scope.stop)
    return store


def fixture_offers(case):
    from gateway import extension_workers as ew, runtime

    isolated_store(case)
    original = ew.broker.offer

    def offer(kind, payload, *, gateway_job_id=""):
        if not gateway_job_id:
            row, _ = runtime.store.submit(modality="tts" if kind == "audio" else kind,
                                          model_id="fixture", params={}, idempotency_key=uuid.uuid4().hex,
                                          provider_id="story-studio-" + kind)
            runtime.store.mark_running(row["id"])
            gateway_job_id = row["id"]
        return original(kind, payload, gateway_job_id=gateway_job_id)

    scope = patch.object(ew.broker, "offer", side_effect=offer)
    scope.start()
    case.addCleanup(scope.stop)


def claim_with_observed_account(client, token, worker_id, kind, extra):
    from gateway import extension_workers as ew, runtime
    from gateway.extension_accounts import payload_sha256

    identity = ew.worker_for(token)
    if identity is None or identity["worker_id"] != worker_id or identity["kind"] != kind:
        return client.post("/api/workers/claim", headers={"Authorization": "Bearer " + token},
                           json={"workerId": worker_id, "kind": kind, **extra})
    # Fixture grants are explicit, never supplied by production claim callers.
    provider = "fish" if kind == "audio" else "flow"
    account = observed_account("fixture-" + worker_id, provider=provider)
    ew.broker.observe_account(identity, account)
    runtime.store.extension_account_set_enabled(provider, account["accountId"], True)
    with ew.admin_session._lock:
        rows = ew._rows()
        rows[identity["_token_id"]]["dispatch_enabled"] = True
        ew.admin_session._write(ew._tokens_path(), {"workers": rows})
    pending = next((j for j in sorted(ew.broker._jobs.values(), key=lambda j: j.created_at)
                    if j.state == "pending" and j.kind == kind), None)
    body = {"workerId": worker_id, "kind": kind, "account": account, **extra}
    if pending:
        body["quote"] = {"provider": provider, "accountId": account["accountId"],
                         "payloadSha256": payload_sha256(pending.payload), "value": 1,
                         "unit": "credits", "source": "fixture.quote", "observedAt": iso()}
    return client.post("/api/workers/claim", headers={"Authorization": "Bearer " + token}, json=body)
