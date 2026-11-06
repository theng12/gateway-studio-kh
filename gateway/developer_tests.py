"""Owner-run finite model tests over registered Hub transport, with no automatic replay."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time
from uuid import uuid4

import httpx

from . import fleet, startup_control
from .developer_contract import (DeveloperTestError, decode, validate, validate_request,
                                 validate_status, validate_receipt)
from .developer_store import DeveloperStore, encoded
from .render_hash_v1 import decode_render_json

SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
# ponytail: 8 MiB is this reader's structural ceiling, not a Hub/model limit; version
# the envelope and expose a larger reader ceiling if packaged fixtures require it.
WIRE_CEILING = 8 * 1024 * 1024
TERMINAL = {"not_run", "passed", "failed", "cancelled", "interrupted"}


def segment(value):
    if not isinstance(value, str) or SEGMENT.fullmatch(value) is None:
        raise DeveloperTestError(400, "INVALID_ID", "Use a registered safe node or test ID.")
    return value


def closed(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise DeveloperTestError(400, "INVALID_REQUEST", "The request has missing or unknown fields.")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class HubDeveloperClient:
    def __init__(self, client=None):
        self.client = client

    def identity(self, controller):
        segment(controller)
        try:
            base, token = startup_control._target(controller)
        except startup_control.StartupError:
            raise DeveloperTestError(503, "LOCATION_UNAVAILABLE", "The original registered location is unavailable.") from None
        if not token:
            raise DeveloperTestError(503, "CREDENTIAL_UNAVAILABLE", "This location has no verified credential.")
        return base, token, sha(encoded([base, token]))

    def send(self, controller, node, operation, *, key=None, raw=None, query=None,
             pin=None, ceiling=WIRE_CEILING, request_ceiling=WIRE_CEILING):
        segment(node)
        suffixes = {"capabilities": ("GET", "/capabilities"), "setting": ("GET", "/setting"),
                    "set_setting": ("POST", "/setting"), "submit": ("POST", ""),
                    "status": ("GET", "/by-key/{key}"),
                    "cancel": ("POST", "/by-key/{key}/cancel"),
                    "receipt": ("GET", "/by-key/{key}/receipt"),
                    "log": ("GET", "/by-key/{key}/log")}
        if operation not in suffixes:
            raise DeveloperTestError(400, "INVALID_OPERATION", "Unknown Developer Mode operation.")
        method, suffix = suffixes[operation]
        if "{key}" in suffix:
            suffix = suffix.format(key=segment(key))
        if operation == "log":
            closed(query, ("offset_bytes", "length_bytes"))
            if any(type(v) is not int or v < 0 for v in query.values()) or query["length_bytes"] == 0:
                raise DeveloperTestError(400, "INVALID_LOG_RANGE", "The log range is invalid.")
        elif query is not None:
            raise DeveloperTestError(400, "INVALID_QUERY", "This operation accepts no query.")
        base, token, current_pin = self.identity(controller)
        if pin is not None and pin != current_pin:
            raise DeveloperTestError(409, "DESTINATION_CHANGED", "The original address or credential changed; keep this attempt in recovery.")
        headers = {"X-Hub-Token": token}
        if raw is not None:
            if len(raw) > min(WIRE_CEILING, request_ceiling):
                raise DeveloperTestError(413, "PAYLOAD_TOO_LARGE", "The request exceeds the published reader limit.")
            headers["Content-Type"] = "application/json"
        if operation == "submit":
            headers["Idempotency-Key"] = segment(key)
        client = self.client or httpx.Client(timeout=httpx.Timeout(20, connect=6), follow_redirects=False)
        body = bytearray()
        try:
            with client.stream(method, base + f"/api/hub/fleet/nodes/{node}/developer-tests" + suffix,
                               headers=headers, content=raw, params=query, follow_redirects=False) as response:
                status = response.status_code
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > min(WIRE_CEILING, ceiling):
                        raise DeveloperTestError(502, "INVALID_HUB_RESPONSE", "The Hub response exceeds its byte limit.")
                    body.extend(chunk)
        except httpx.HTTPError:
            raise DeveloperTestError(502, "TRANSPORT_UNCERTAIN", "The original controller did not answer; acceptance is unresolved.") from None
        finally:
            if self.client is None:
                client.close()
        result = bytes(body)
        if token.encode() in result:
            raise DeveloperTestError(502, "INVALID_HUB_RESPONSE", "The Hub response contained private credential material.")
        if status not in (200, 202):
            # A refusal is never assumed to settle a previously attempted submission.
            raise DeveloperTestError(status if status in (400, 401, 403, 404, 409, 413, 503) else 502,
                                     "HUB_REFUSED", "The Hub refused this Developer Mode operation.")
        return status, result


class DeveloperTests:
    def __init__(self, store=None, transport=None, inventory=None):
        self.store = store or DeveloperStore()
        self.transport = transport or HubDeveloperClient()
        self.inventory_source = inventory or fleet.snapshot

    def inventory(self):
        snapshot = self.inventory_source()
        at = snapshot.get("generated_at")
        fresh = (not isinstance(at, bool) and isinstance(at, (int, float)) and math.isfinite(at)
                 and 0 <= time.time() - at <= fleet._MEMBERS_STALE_AFTER_S)
        locations = {row["id"]: row for row in snapshot.get("locations", [])}
        targets = []
        for machine in snapshot.get("machines", []):
            controller, node = machine.get("location_id"), machine.get("machine")
            if not all(isinstance(v, str) and SEGMENT.fullmatch(v) for v in (controller, node)):
                continue
            location = locations.get(controller) or {}
            memory = machine.get("memory_gb")
            ram = (int(memory * 1024**3) if type(memory) in (int, float)
                   and math.isfinite(memory) and memory > 0 else None)
            paused = bool(location.get("paused_at") or location.get("paused")
                          or machine.get("node_paused") or machine.get("paused")
                          or location.get("enabled") is False)
            targets.append({"controller_id": controller, "node_id": node,
                            "label": machine.get("label") or node,
                            "chip": machine.get("chip"), "ram_bytes": ram,
                            "paused": paused,
                            "online": bool(fresh and location.get("reachable") is True
                                           and machine.get("reachable") is True),
                            "hub_controller_id": location.get("controller_id")})
        return {"inventory_at": at, "targets": targets, "fresh": fresh}

    def target(self, controller, node):
        segment(controller), segment(node)
        found = next((r for r in self.inventory()["targets"]
                      if (r["controller_id"], r["node_id"]) == (controller, node)), None)
        if found is None:
            raise DeveloperTestError(404, "UNKNOWN_TARGET", "This Mac is not in the registered inventory.")
        return found

    def capabilities(self, controller, node):
        target = self.target(controller, node)
        _, raw = self.transport.send(controller, node, "capabilities")
        cap = decode(raw, "capabilities", max_bytes=WIRE_CEILING)
        if node != "local" and cap["node_id"] != node:
            raise DeveloperTestError(502, "IDENTITY_MISMATCH", "The Hub answered for a different node.")
        if target["hub_controller_id"] is not None and cap["controller_id"] != target["hub_controller_id"]:
            raise DeveloperTestError(502, "IDENTITY_MISMATCH", "The Hub answered for a different controller.")
        observed = datetime.fromisoformat(cap["server_time"].replace("Z", "+00:00")).timestamp()
        if abs(time.time() - observed) > fleet._MEMBERS_STALE_AFTER_S:
            raise DeveloperTestError(503, "STALE_CAPABILITIES", "Developer Mode authority is stale.")
        if len(raw) > cap["bounds"]["response_bytes"]:
            raise DeveloperTestError(502, "INVALID_HUB_RESPONSE", "Capabilities exceed their declared byte limit.")
        return cap

    def setting(self, controller, node, raw):
        target = self.target(controller, node)
        request = decode(raw, "settingRequest", max_bytes=WIRE_CEILING)
        action = f"{controller}/{node}/{request['request_id']}"
        _, _, pin = self.transport.identity(controller)
        fresh, original = self.store.setting_action(action, encoded({
            "request_base64": base64.b64encode(raw).decode("ascii"), "transport_pin": pin}))
        if not fresh:
            if original is not None:
                return decode(original, "setting", max_bytes=WIRE_CEILING)
            # Readback of the same request is the only safe reconciliation; no resend.
            _, result = self.transport.send(controller, node, "setting", pin=pin)
        else:
            _, result = self.transport.send(controller, node, "set_setting", raw=raw, pin=pin)
        answer = decode(result, "setting", max_bytes=WIRE_CEILING)
        if (answer["request_id"] != request["request_id"] or not answer["verified"]
                or answer["enabled"] != request["enabled"] or (node != "local" and answer["node_id"] != node)
                or (target["hub_controller_id"] is not None
                    and answer["controller_id"] != target["hub_controller_id"])):
            raise DeveloperTestError(502, "SETTING_UNRESOLVED", "The switch change has no matching verified readback yet.")
        self.store.setting_result(action, result)
        return answer

    def view(self):
        inventory = self.inventory()
        return {**inventory, "runs": [self.public(r) for r in self.store.rows()],
                "store_revision": self.store.evidence()["store_revision"],
                "reader_byte_ceiling": WIRE_CEILING}

    @staticmethod
    def public(row):
        visible = {k: v for k, v in row.items() if k not in {
            "request_bytes", "receipt_bytes", "log_bytes", "capabilities", "transport_pin"}}
        visible["request"] = json.loads(row["request_bytes"])
        visible["receipt"] = None if row["receipt_bytes"] is None else json.loads(row["receipt_bytes"])
        visible["log_complete"] = bool(row.get("log_complete"))
        visible["log_collected_bytes"] = len(row["log_bytes"])
        visible["log_total_bytes"] = (None if visible["receipt"] is None
                                      else visible["receipt"].get("log", {}).get("size_bytes"))
        return visible

    def run(self, raw):
        if len(raw) > WIRE_CEILING:
            raise DeveloperTestError(413, "PAYLOAD_TOO_LARGE", "The run exceeds the reader byte limit.")
        body = decode_render_json(raw)
        fields = ("run_id", "inventory_at", "targets", "kind", "model_id", "settings", "requested_limits")
        closed(body, fields + (("filters",) if "filters" in body else ()))
        filters = body.get("filters", {"chip": None, "ram_bytes": None, "query": ""})
        closed(filters, ("chip", "ram_bytes", "query"))
        if ((filters["chip"] is not None and (type(filters["chip"]) is not str
                or not filters["chip"] or len(filters["chip"]) > 256))
                or (filters["ram_bytes"] is not None and (type(filters["ram_bytes"]) is not int
                    or not 0 < filters["ram_bytes"] <= 2**53 - 1))
                or type(filters["query"]) is not str or len(filters["query"]) > 256):
            raise DeveloperTestError(400, "INVALID_TARGET_FILTER", "Use a stated chip, physical RAM size or Mac name/node filter.")
        campaign = segment(body["run_id"])
        with self.store.connection() as db:
            prior = db.execute("SELECT body FROM campaigns WHERE id=?", (campaign,)).fetchone()
        if prior:
            if prior[0] != raw:
                raise DeveloperTestError(409, "RUN_CONFLICT", "This run ID already names different bytes.")
            return {"runs": [self.public(r) for r in self.store.rows(campaign)]}
        inventory = self.inventory()
        if not inventory["fresh"] or body["inventory_at"] != inventory["inventory_at"]:
            raise DeveloperTestError(409, "STALE_INVENTORY", "Refresh the registered target list before Run.")
        if not isinstance(body["targets"], list) or not 1 <= len(body["targets"]) <= 4096:
            raise DeveloperTestError(400, "INVALID_TARGETS", "Select a finite set of registered Macs.")
        targets = {(t["controller_id"], t["node_id"]): t for t in inventory["targets"]}
        rows, seen = [], set()
        for selection in body["targets"]:
            closed(selection, ("controller_id", "node_id"))
            controller, node = selection["controller_id"], selection["node_id"]
            segment(controller), segment(node)
            target = targets.get((controller, node))
            if target is None:
                raise DeveloperTestError(404, "UNKNOWN_TARGET", "This Mac is not in the selected inventory.")
            if ((filters["chip"] is not None and target["chip"] != filters["chip"])
                    or (filters["ram_bytes"] is not None and target["ram_bytes"] != filters["ram_bytes"])
                    or filters["query"].casefold() not in f"{target['label']} {node}".casefold()):
                raise DeveloperTestError(409, "TARGET_FILTER_MISMATCH", "A selected Mac does not match every chip, RAM and node filter.")
            if (controller, node) in seen:
                raise DeveloperTestError(400, "DUPLICATE_TARGET", "Each Mac may appear once per run.")
            seen.add((controller, node))
            if target["paused"]:
                raise DeveloperTestError(409, "TARGET_PAUSED", "A selected Mac or its location is paused or Off.")
            if not target["online"]:
                raise DeveloperTestError(503, "TARGET_OFFLINE", "A selected Mac has no fresh reachable inventory; nothing was submitted.")
            cap = self.capabilities(controller, node)
            model = next((m for m in cap["models"] if m["model"]["id"] == body["model_id"]), None)
            if model is None:
                raise DeveloperTestError(409, "MODEL_UNAVAILABLE", "The chosen model is not declared by every selected Mac.")
            settings = dict(body["settings"])
            closed(settings, ("width_px", "height_px", "steps", "guidance", "seed", "memory_mode",
                              "reference_count", "prompt_characters", "fixture_set_id"))
            fixture = next((f for f in cap["fixture_sets"] if f["id"] == settings["fixture_set_id"]), None)
            if fixture is None:
                raise DeveloperTestError(409, "FIXTURE_UNAVAILABLE", "The packaged fixture set is unavailable.")
            count, chars = settings["reference_count"], settings["prompt_characters"]
            if (type(count) is not int or count < 0 or count > min(4096, len(fixture["reference_order"]))
                    or type(chars) is not int or chars < 0
                    or chars > min(WIRE_CEILING, model["settings_bounds"]["prompt_characters"]["maximum"])):
                raise DeveloperTestError(400, "INVALID_VECTOR", "The reference or prompt count exceeds live test bounds.")
            pattern = fixture["prompt_pattern"]
            prompt = (pattern * ((chars + len(pattern) - 1) // len(pattern)))[:chars]
            settings["prompt_sha256"] = sha(prompt.encode("utf-8"))
            fixtures = {f["fixture_id"]: f for f in fixture["fixtures"]}
            refs = [{k: fixtures[name][k] for k in ("fixture_id", "sha256", "size_bytes")}
                    for name in fixture["reference_order"][:count]]
            test_id = uuid4().hex
            request = {"format": "studio.developer-test.request.v1", "run_id": campaign,
                       "test_id": test_id, "runner_id": cap["runner_id"], "model": model["model"],
                       "kind": body["kind"], "settings": settings, "input_refs": refs,
                       "requested_limits": body["requested_limits"], "runner_revision": cap["runner_revision"],
                       "runner_sha256": cap["runner_sha256"], "fixture_set_sha256": fixture["sha256"],
                       "node_setting_revision": cap["setting"]["revision"]}
            validate_request(request, cap)
            request_bytes = encoded(request)
            if len(request_bytes) > min(WIRE_CEILING, cap["bounds"]["request_bytes"]):
                raise DeveloperTestError(413, "PAYLOAD_TOO_LARGE", "The vector exceeds the Hub's live request limit.")
            _, _, pin = self.transport.identity(controller)
            rows.append({**target, "id": test_id, "campaign": campaign, "key": uuid4().hex,
                         "inventory_at": inventory["inventory_at"], "selection_filters": filters,
                         "capabilities": cap,
                         "transport_pin": pin, "request_bytes": request_bytes,
                         "request_sha256": sha(request_bytes), "submitted": False,
                         "connectivity": "online", "state": "not_run", "reason": "Not submitted yet.",
                         "hub_status": None, "receipt_sha256": None, "log_complete": False})
        created = self.store.create(campaign, raw, rows)
        if created:
            for row in rows:
                self.submit(row["id"])
        return {"runs": [self.public(r) for r in self.store.rows(campaign)]}

    def submit(self, attempt):
        row = self.store.get(attempt)
        if row["submitted"]:
            return row
        try:
            target = self.target(row["controller_id"], row["node_id"])
        except DeveloperTestError:
            return self.store.update(attempt, state="not_run", connectivity="unknown",
                                     reason="The registered target disappeared before submission.")
        if target["paused"] or not target["online"]:
            return self.store.update(attempt, state="offline" if not target["online"] else "not_run",
                                     connectivity="offline" if not target["online"] else "online",
                                     reason="The Mac became paused or unavailable before submission.")
        # Persist the uncertainty boundary BEFORE sending. No restart or refresh resends it.
        if not self.store.claim_send(attempt):
            return self.store.get(attempt)
        try:
            status_code, raw = self.transport.send(row["controller_id"], row["node_id"], "submit",
                key=row["key"], raw=row["request_bytes"], pin=row["transport_pin"],
                ceiling=row["capabilities"]["bounds"]["response_bytes"],
                request_ceiling=row["capabilities"]["bounds"]["request_bytes"])
            return self.accept_status(row, raw)
        except DeveloperTestError as error:
            return self.store.update(attempt, state="recovery_hold", connectivity="unknown",
                                     reason=f"{error.detail} Reconcile the original controller, node and key; no automatic resubmission.")

    def accept_status(self, row, raw):
        cap, request = row["capabilities"], json.loads(row["request_bytes"])
        status = decode(raw, "status", max_bytes=min(WIRE_CEILING, cap["bounds"]["response_bytes"]))
        validate_status(status, request, cap)
        previous = row.get("hub_status")
        if (status["idempotency_key"] != row["key"] or status["request_sha256"] != row["request_sha256"]
                or (previous is not None and previous["attempt_id"] != status["attempt_id"])):
            raise DeveloperTestError(502, "IDENTITY_MISMATCH", "The status does not bind the original attempt.")
        if row.get("receipt_bytes") is not None:
            if status["receipt_sha256"] != sha(row["receipt_bytes"]):
                raise DeveloperTestError(502, "IDENTITY_MISMATCH", "The settled receipt identity changed.")
            return row
        # A terminal status alone is not immutable validated evidence.
        state = "recovery_hold" if status["state"] in TERMINAL else status["state"]
        reason = ("Collecting the terminal receipt and retained log." if status["state"] in TERMINAL
                  else (status["reason"] or {}).get("detail"))
        return self.store.update(row["id"], hub_status=status, connectivity="online", state=state, reason=reason)

    def refresh(self, attempt):
        row = self.store.get(attempt)
        if not row["submitted"]:
            return self.public(row)
        try:
            cap = row["capabilities"]
            if row["receipt_bytes"] is None:
                _, raw = self.transport.send(row["controller_id"], row["node_id"], "status", key=row["key"],
                    pin=row["transport_pin"], ceiling=cap["bounds"]["response_bytes"])
                row = self.accept_status(row, raw)
                status = row["hub_status"]
                if status["state"] in TERMINAL:
                    if not status["resource_release_confirmed"] or status["receipt_sha256"] is None:
                        raise DeveloperTestError(502, "INCOMPLETE_EVIDENCE", "Terminal status has no confirmed resource release and receipt.")
                    _, raw = self.transport.send(row["controller_id"], row["node_id"], "receipt", key=row["key"],
                        pin=row["transport_pin"], ceiling=cap["bounds"]["response_bytes"])
                    if sha(raw) != status["receipt_sha256"]:
                        raise DeveloperTestError(502, "RECEIPT_HASH_MISMATCH", "The receipt bytes do not match their immutable hash.")
                    receipt = decode(raw, "receipt", max_bytes=min(WIRE_CEILING, cap["bounds"]["response_bytes"]))
                    validate_receipt(receipt, json.loads(row["request_bytes"]), cap)
                    if any(receipt[k] != status[k] for k in ("attempt_id", "idempotency_key", "request_sha256", "node_id", "controller_id")) or receipt["outcome"] != status["state"]:
                        raise DeveloperTestError(502, "IDENTITY_MISMATCH", "The receipt is not the original settled attempt.")
                    row = self.store.update(attempt, receipt=raw, receipt_sha256=sha(raw))
            if row["receipt_bytes"] is not None:
                row = self.collect_log(row)
                if row["log_complete"]:
                    receipt = json.loads(row["receipt_bytes"])
                    row = self.store.update(attempt, state=receipt["outcome"],
                        reason=None if receipt["reason"] is None else receipt["reason"]["detail"])
        except (DeveloperTestError, ValueError) as error:
            detail = error.detail if isinstance(error, DeveloperTestError) else "The collected log encoding is invalid."
            row = self.store.update(attempt, state="recovery_hold", connectivity="unknown",
                reason=f"{detail} The original attempt is retained and will not be resubmitted.")
        return self.public(row)

    def collect_log(self, row):
        if row.get("log_complete"):
            return row
        receipt = json.loads(row["receipt_bytes"])
        descriptor, cap = receipt["log"], row["capabilities"]
        current = row["log_bytes"]
        total = descriptor["size_bytes"]
        if total > min(WIRE_CEILING, cap["bounds"]["log_total_bytes"]):
            raise DeveloperTestError(502, "INVALID_LOG", "The retained log exceeds the published bound.")
        if len(current) < total:
            envelope = {k: receipt[k] for k in ("node_id", "controller_id", "attempt_id", "request_sha256")}
            envelope.update(format="studio.developer-test.log-chunk.v1", log=descriptor,
                            offset_bytes=total, length_bytes=total, data_base64="",
                            chunk_sha256="0" * 64, eof=False)
            # Reserve envelope bytes before base64 expansion; the response bound is independent.
            room = (min(WIRE_CEILING, cap["bounds"]["response_bytes"])
                    - len(json.dumps(envelope, ensure_ascii=True, allow_nan=False).encode("utf-8")))
            length = min(total - len(current), cap["bounds"]["log_chunk_bytes"], max(0, room // 4 * 3))
            if length == 0:
                raise DeveloperTestError(502, "INVALID_LOG", "The response bound cannot carry a log chunk.")
            _, raw = self.transport.send(row["controller_id"], row["node_id"], "log", key=row["key"],
                query={"offset_bytes": len(current), "length_bytes": length}, pin=row["transport_pin"],
                ceiling=cap["bounds"]["response_bytes"])
            chunk = decode(raw, "logChunk", max_bytes=min(WIRE_CEILING, cap["bounds"]["response_bytes"]))
            data = base64.b64decode(chunk["data_base64"], validate=True)
            if (any(chunk[k] != receipt[k] for k in ("node_id", "controller_id", "attempt_id", "request_sha256"))
                    or chunk["log"] != descriptor or chunk["offset_bytes"] != len(current)
                    or chunk["length_bytes"] != length or len(data) != length
                    or sha(data) != chunk["chunk_sha256"] or chunk["eof"] != (len(current) + length == total)):
                raise DeveloperTestError(502, "INVALID_LOG", "The log chunk does not bind the retained evidence.")
            current += data
        complete = len(current) == total and sha(current) == descriptor["sha256"]
        if len(current) == total and not complete:
            raise DeveloperTestError(502, "INVALID_LOG", "The reconstructed log digest does not match.")
        if complete:
            current.decode("utf-8", errors="strict")
        _, token, pin = self.transport.identity(row["controller_id"])
        if pin != row["transport_pin"] or token.encode("utf-8") in current:
            raise DeveloperTestError(502, "INVALID_LOG", "The log redaction or destination identity could not be verified.")
        return self.store.update(row["id"], log=current, log_complete=complete)

    def cancel(self, attempt, raw):
        decode(raw, "cancelRequest", max_bytes=WIRE_CEILING)
        row = self.store.get(attempt)
        if not row["submitted"]:
            return self.public(self.store.update(attempt, state="not_run", reason="Cancelled before submission."))
        if row["receipt_bytes"] is not None:
            return self.public(row)
        self.store.update(attempt, reason="Cancellation requested; resource release is not yet confirmed.")
        try:
            _, result = self.transport.send(row["controller_id"], row["node_id"], "cancel", key=row["key"],
                raw=raw, pin=row["transport_pin"], ceiling=row["capabilities"]["bounds"]["response_bytes"])
            row = self.accept_status(row, result)
        except DeveloperTestError:
            row = self.store.update(attempt, state="recovery_hold", reason="Cancellation is unresolved; resources are not claimed released.")
        return self.public(row)

    def proposal(self, attempt):
        row = self.store.get(attempt)
        if row["receipt_bytes"] is None or not row.get("log_complete") or row["state"] != "passed":
            raise DeveloperTestError(409, "INCOMPLETE_EVIDENCE", "Only complete verified passed evidence can be reviewed for approval.")
        receipt = json.loads(row["receipt_bytes"])
        validate_receipt(receipt, json.loads(row["request_bytes"]), row["capabilities"])
        scope = {"node_ids": [receipt["node_id"]], "chip_model": receipt["hardware"]["chip_model"],
                 "ram_bytes": receipt["hardware"]["ram_bytes"],
                 "model": json.loads(row["request_bytes"])["model"],
                 "runner_sha256": receipt["runner_sha256"], "settings": receipt["settings"]}
        scope_key = sha(encoded(scope))
        metrics = receipt["measurements"]
        values = {"measured_start_min_available_memory_bytes": receipt["measurement_metadata"]["available_memory_before_start_bytes"],
                  "measured_runtime_seconds": metrics["generation_seconds"],
                  "measured_peak_process_tree_rss_bytes": metrics["peak_process_memory_bytes"],
                  "measured_system_swap_growth_bytes": metrics["swap_growth_bytes"]}
        with self.store.connection() as db:
            prior = db.execute("SELECT data FROM measured WHERE scope=?", (scope_key,)).fetchone()
            previous = {} if prior is None else json.loads(prior[0])["values"]
            proposal = {"expected_store_revision": self.store.revision(db), "scope": scope,
                        "receipt_sha256s": [row["receipt_sha256"]],
                        "changes": [{"field": field, "previous_value": previous.get(field), "value": value,
                                     "unit": "seconds" if field == "measured_runtime_seconds" else "bytes"}
                                    for field, value in values.items()]}
            raw = encoded(proposal)
            digest = sha(raw)
            db.execute("INSERT OR IGNORE INTO proposals VALUES(?,?)", (digest, raw))
        return {**proposal, "proposal_sha256": digest}

    def approve(self, attempt, payload, actor):
        closed(payload, ("proposal_sha256", "expected_store_revision", "owner_action_id"))
        segment(payload["owner_action_id"])
        # Re-read immutable evidence at approval time; never accept a caller's measured values.
        current = self.proposal(attempt)
        with self.store.connection() as db:
            existing = db.execute("SELECT body FROM approvals WHERE id=?", (payload["owner_action_id"],)).fetchone()
            if existing:
                answer = json.loads(existing[0])
                if (answer["proposal_sha256"] != payload["proposal_sha256"]
                        or answer["expected_store_revision"] != payload["expected_store_revision"]):
                    raise DeveloperTestError(409, "APPROVAL_CONFLICT", "This owner action ID already records a different approval.")
                return answer
            if (payload["expected_store_revision"] != self.store.revision(db)
                    or current["proposal_sha256"] != payload["proposal_sha256"]):
                raise DeveloperTestError(409, "STALE_APPROVAL", "The evidence proposal or measured store changed; review it again.")
            answer = {"format": "studio.developer-test.approval.v1", **payload,
                      "receipt_sha256s": current["receipt_sha256s"], "actor": actor,
                      "approved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                      "scope": current["scope"], "changes": current["changes"]}
            validate(answer, "approval")
            scope_key = sha(encoded(answer["scope"]))
            record = {"scope": answer["scope"], "values": {c["field"]: c["value"] for c in answer["changes"]},
                      "source": "measured", "receipt_sha256s": answer["receipt_sha256s"],
                      "approval_id": answer["owner_action_id"]}
            previous = db.execute("SELECT data FROM measured WHERE scope=?", (scope_key,)).fetchone()
            db.execute("INSERT INTO approval_previous VALUES(?,?)",
                       (answer["owner_action_id"], None if previous is None else previous[0]))
            db.execute("INSERT INTO approvals VALUES(?,?)", (answer["owner_action_id"], encoded(answer)))
            db.execute("INSERT OR REPLACE INTO measured VALUES(?,?)", (scope_key, encoded(record).decode()))
            db.execute("UPDATE meta SET value=? WHERE key='revision'", (uuid4().hex,))
        return answer

    def rollback(self, action_id, expected_revision, actor):
        segment(action_id)
        with self.store.connection() as db:
            if self.store.revision(db) != expected_revision:
                raise DeveloperTestError(409, "STALE_APPROVAL", "The measured evidence store changed.")
            found = db.execute("SELECT body FROM approvals WHERE id=?", (action_id,)).fetchone()
            if found is None:
                raise DeveloperTestError(404, "UNKNOWN_APPROVAL", "No such measured evidence approval.")
            approval = json.loads(found[0])
            key = sha(encoded(approval["scope"]))
            current = db.execute("SELECT data FROM measured WHERE scope=?", (key,)).fetchone()
            if current is None or json.loads(current[0])["approval_id"] != action_id:
                raise DeveloperTestError(409, "STALE_APPROVAL", "A newer approval already changed this exact scope.")
            values = {c["field"]: c["previous_value"] for c in approval["changes"]}
            previous = db.execute("SELECT data FROM approval_previous WHERE id=?", (action_id,)).fetchone()
            if previous is None:
                raise DeveloperTestError(409, "ROLLBACK_UNAVAILABLE", "The prior evidence snapshot is unavailable.")
            restored = (json.loads(previous[0]) if previous[0] is not None else
                        {"scope": approval["scope"], "values": values, "source": "measured",
                         "receipt_sha256s": [], "approval_id": None})
            db.execute("UPDATE measured SET data=? WHERE scope=?", (encoded(restored).decode(), key))
            revision = uuid4().hex
            db.execute("UPDATE meta SET value=? WHERE key='revision'", (revision,))
            db.execute("INSERT INTO observations(attempt,at,data) VALUES(?,?,?)",
                       (action_id, time.time(), encoded({"action": "approval_rollback", "actor": actor,
                                                        "revision": revision}).decode()))
        return {"restored": True, "store_revision": revision}

    def log(self, attempt):
        row = self.store.get(attempt)
        return {"complete": bool(row.get("log_complete")),
                "text": row["log_bytes"].decode("utf-8") if row.get("log_complete") else None,
                "reason": None if row.get("log_complete") else "The immutable redacted log is not fully verified yet."}
