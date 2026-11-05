"""Wire validation for Remote Developer Mode (contract studio.developer-test.v1).

The frozen design bytes live in ``data/developer-test-v1.schema.json`` (sha256
3ce2175fb7456161cc45bb6ffc7b3930f6c09eb76492b9ef917d1ec240bd3e7a); this module
never re-derives them and refuses to run if those bytes change. ``decode`` is the
strict byte path (UTF-8, duplicate keys, nonfinite/unsafe numbers, ceiling before
parse) into closed named-schema validation. The schema is draft 2020-12 but only a
small keyword subset is used, so validation is an explicit minimal interpreter: an
unrecognised keyword fails closed (500), never permissively skipped.

``validate(value, "capabilities")`` (and therefore ``decode`` of capabilities) also
enforces the normative capability relations the schema cannot express; the
``validate_request`` / ``validate_status`` / ``validate_receipt`` helpers enforce
the remaining normative rules: live bounds, packaged fixture order, prompt digest
construction, identity bindings, and complete passed-receipt measurements. No
numeric default is invented anywhere: an unverifiable fact is an error.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import NoReturn

from .render_hash_v1 import _constant, _integer, _pairs

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "data" / "developer-test-v1.schema.json"
_SCHEMA_SHA256 = "3ce2175fb7456161cc45bb6ffc7b3930f6c09eb76492b9ef917d1ec240bd3e7a"
# ponytail: 32 MiB is a reader ceiling above the schema's 16 MiB logChunk field,
# not a Hub or model limit; raise it with the envelope version if packaged logs grow.
_MAX_BYTES = 32 * 1024 * 1024

_SUPPORTED = frozenset({
    "$ref", "anyOf", "additionalProperties", "const", "enum", "format", "items",
    "maxItems", "maxLength", "maximum", "minItems", "minLength", "minimum",
    "pattern", "properties", "required", "type", "uniqueItems",
    # annotations: present in the frozen bytes, carrying no validation
    "$schema", "$id", "$comment", "title", "description", "default", "examples",
})
_STRING_KEYWORDS = frozenset({"minLength", "maxLength", "pattern", "format"})
_ARRAY_KEYWORDS = frozenset({"items", "minItems", "maxItems", "uniqueItems"})
_OBJECT_KEYWORDS = frozenset({"properties", "required", "additionalProperties"})
_PATTERNS: dict = {}
_SCHEMA = None

# receipt metric_unavailable_reasons field enum, as frozen: six measurements,
# the preflight observation, and five nullable hardware observations.
_METRIC_FIELDS = (
    ("measurements", "elapsed_seconds"),
    ("measurements", "generation_seconds"),
    ("measurements", "peak_process_memory_bytes"),
    ("measurements", "system_swap_baseline_bytes"),
    ("measurements", "system_swap_peak_bytes"),
    ("measurements", "swap_growth_bytes"),
    ("measurement_metadata", "available_memory_before_start_bytes"),
    ("hardware", "chip_family"),
    ("hardware", "chip_model"),
    ("hardware", "ram_bytes"),
    ("hardware", "os_version"),
    ("hardware", "os_build"),
)


class DeveloperTestError(Exception):
    """A wire refusal: HTTP status, stable machine code, human detail."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(f"{status} {code}: {detail}")
        self.status = int(status)
        self.code = str(code)
        self.detail = str(detail)


def _bad(detail) -> NoReturn:
    """An answer from the Hub that violates the contract it claims to speak."""
    raise DeveloperTestError(502, "INVALID_HUB_RESPONSE", detail)


def _identity(detail):
    raise DeveloperTestError(502, "IDENTITY_MISMATCH", detail)


def _incomplete(detail):
    raise DeveloperTestError(502, "INCOMPLETE_EVIDENCE", detail)


def _violation(path, message):
    raise DeveloperTestError(400, "SCHEMA_VIOLATION", f"{path}: {message}")


def _schema():
    global _SCHEMA
    if _SCHEMA is None:
        try:
            raw = _SCHEMA_PATH.read_bytes()
        except OSError as exc:
            raise DeveloperTestError(500, "SCHEMA_UNAVAILABLE", str(exc)) from exc
        if hashlib.sha256(raw).hexdigest() != _SCHEMA_SHA256:
            raise DeveloperTestError(500, "SCHEMA_INTEGRITY",
                                     "The frozen Developer Mode schema bytes no longer match their hash.")
        try:
            loaded = json.loads(raw.decode("utf-8", errors="strict"))
        except (ValueError, UnicodeError) as exc:
            raise DeveloperTestError(500, "SCHEMA_INTEGRITY", str(exc)) from exc
        _SCHEMA = loaded
    return _SCHEMA


def _number(token):
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("nonfinite JSON number")
    return value


def _same(value, other):
    return type(value) is type(other) and value == other


def _type_names(node):
    expected = node.get("type")
    if isinstance(expected, list):
        return expected
    return [expected] if expected is not None else []


def _is_type(value, name):
    if name == "null":
        return value is None
    if name == "boolean":
        return type(value) is bool
    if name == "integer":
        return type(value) is int
    if name == "number":
        return type(value) in (int, float)
    if name == "string":
        return type(value) is str
    if name == "array":
        return type(value) is list
    if name == "object":
        return type(value) is dict
    raise DeveloperTestError(500, "SCHEMA_ERROR", f"The frozen schema names unknown type {name!r}.")


def _match(pattern, text):
    compiled = _PATTERNS.get(pattern)
    if compiled is None:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise DeveloperTestError(500, "SCHEMA_ERROR", f"bad frozen pattern {pattern!r}: {exc}") from exc
        _PATTERNS[pattern] = compiled
    return compiled.search(text) is not None


def _timestamp(text):
    """RFC 3339 UTC 'Z' instant; anything else is not a contract timestamp."""
    if type(text) is not str or "T" not in text[:11] or not text.endswith("Z"):
        raise ValueError("not a UTC Z timestamp")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("not a UTC Z timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("not a UTC Z timestamp")
    return parsed


def _check(value, node, path, root):
    if type(node) is not dict:
        raise DeveloperTestError(500, "SCHEMA_ERROR", f"{path}: schema node is not an object.")
    for keyword in node:
        if keyword not in _SUPPORTED:
            raise DeveloperTestError(500, "UNSUPPORTED_SCHEMA_KEYWORD",
                                     f"{path}: schema keyword {keyword!r} has no validator.")
    if "$ref" in node:
        target = node["$ref"]
        prefix = "#/$defs/"
        if type(target) is not str or not target.startswith(prefix):
            raise DeveloperTestError(500, "SCHEMA_ERROR", f"{path}: unsupported $ref {target!r}.")
        name = target[len(prefix):]
        defs = root.get("$defs") if type(root) is dict else None
        if type(defs) is not dict or name not in defs:
            raise DeveloperTestError(500, "SCHEMA_ERROR", f"{path}: unknown $ref {target!r}.")
        _check(value, defs[name], path, root)
    names = _type_names(node)
    if names and not any(_is_type(value, name) for name in names):
        _violation(path, "must be " + " or ".join(str(name) for name in names))
    if "const" in node and not _same(value, node["const"]):
        _violation(path, f"must equal {node['const']!r}")
    if "enum" in node and not any(_same(value, option) for option in node["enum"]):
        _violation(path, "is not one of the allowed values")
    if "minimum" in node or "maximum" in node:
        if type(value) not in (int, float):
            _violation(path, "must be a number")
        if not math.isfinite(value):
            _violation(path, "must be finite")
        if "minimum" in node and value < node["minimum"]:
            _violation(path, f"must be at least {node['minimum']}")
        if "maximum" in node and value > node["maximum"]:
            _violation(path, f"must be at most {node['maximum']}")
    if _STRING_KEYWORDS & set(node):
        if type(value) is not str:
            _violation(path, "must be a string")
        if "minLength" in node and len(value) < node["minLength"]:
            _violation(path, f"must be at least {node['minLength']} characters")
        if "maxLength" in node and len(value) > node["maxLength"]:
            _violation(path, f"must be at most {node['maxLength']} characters")
        if "pattern" in node and not _match(node["pattern"], value):
            _violation(path, "does not match the allowed pattern")
        if "format" in node:
            if node["format"] != "date-time":
                raise DeveloperTestError(500, "SCHEMA_ERROR",
                                         f"{path}: unsupported format {node['format']!r}.")
            try:
                _timestamp(value)
            except ValueError:
                _violation(path, "must be a UTC Z timestamp")
    if type(value) is list and (_ARRAY_KEYWORDS & set(node) or "array" in names):
        if "minItems" in node and len(value) < node["minItems"]:
            _violation(path, f"must hold at least {node['minItems']} items")
        if "maxItems" in node and len(value) > node["maxItems"]:
            _violation(path, f"must hold at most {node['maxItems']} items")
        if "items" not in node:
            raise DeveloperTestError(500, "SCHEMA_ERROR", f"{path}: array schema has no item validator.")
        for index, item in enumerate(value):
            _check(item, node["items"], f"{path}[{index}]", root)
        if node.get("uniqueItems"):
            if not all(type(item) is str for item in value):
                raise DeveloperTestError(500, "SCHEMA_ERROR",
                                         f"{path}: uniqueItems is only implemented for strings.")
            if len(set(value)) != len(value):
                _violation(path, "items must be unique")
    if type(value) is dict and (_OBJECT_KEYWORDS & set(node) or "object" in names):
        properties = node.get("properties")
        if type(properties) is not dict:
            properties = {}
        for key in node.get("required", ()):  # every frozen object is closed and typed
            if key not in value:
                _violation(path, f"missing required field {key!r}")
        if node.get("additionalProperties") is not False:
            raise DeveloperTestError(500, "SCHEMA_ERROR", f"{path}: object schema is not closed.")
        unknown = sorted(set(value) - set(properties))
        if unknown:
            _violation(path, "unknown field(s): " + ", ".join(unknown))
        for key, child in properties.items():
            if key in value:
                _check(value[key], child, f"{path}.{key}", root)
    if "anyOf" in node:
        for branch in node["anyOf"]:
            try:
                _check(value, branch, path, root)
                return
            except DeveloperTestError as exc:
                if exc.status != 400:
                    raise
        _violation(path, "matches none of the allowed forms")


def validate(value, kind):
    """Validate against the frozen named schema; capability/approval semantics included."""
    schema = _schema()
    defs = schema.get("$defs")
    if type(kind) is not str or type(defs) is not dict or kind not in defs:
        raise DeveloperTestError(500, "UNKNOWN_KIND", f"No frozen schema defines {kind!r}.")
    _check(value, {"$ref": "#/$defs/" + kind}, kind, schema)
    if kind == "capabilities":
        _capability_semantics(value)
    elif kind == "approval":
        _approval_semantics(value)
    return value


def decode(raw, kind, max_bytes):
    """Strict UTF-8 JSON bytes -> closed named-schema dict, or DeveloperTestError."""
    if type(raw) is not bytes:
        raise DeveloperTestError(400, "INVALID_JSON", "Wire input must be bytes.")
    if type(max_bytes) is not int:
        raise DeveloperTestError(500, "INVALID_CEILING", "The caller byte ceiling must be an integer.")
    ceiling = min(max_bytes, _MAX_BYTES)
    if len(raw) > ceiling:  # checked before parsing, per the contract
        raise DeveloperTestError(413, "PAYLOAD_TOO_LARGE",
                                 f"The body exceeds the {ceiling} byte ceiling.")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise DeveloperTestError(400, "INVALID_JSON", "A UTF-8 BOM is forbidden.")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DeveloperTestError(400, "INVALID_JSON", "The body is not strict UTF-8.") from exc
    try:
        value = json.loads(text, object_pairs_hook=_pairs, parse_int=_integer,
                           parse_float=_number, parse_constant=_constant)
    except ValueError as exc:
        reason = str(exc).removeprefix("Invalid render hash value: ")
        raise DeveloperTestError(400, "INVALID_JSON", reason or "The body is not valid JSON.") from exc
    except RecursionError as exc:
        raise DeveloperTestError(400, "INVALID_JSON", "The body nests too deeply.") from exc
    return validate(value, kind)


def _capability_semantics(caps):
    bounds = caps["bounds"]
    if bounds["log_chunk_bytes"] > bounds["log_total_bytes"]:
        _bad("log_chunk_bytes exceeds log_total_bytes.")
    if len(caps["models"]) > bounds["models"]:
        _bad("The model list exceeds the published wire bound.")
    if len(caps["fixture_sets"]) > bounds["fixture_sets"]:
        _bad("The fixture set list exceeds the published wire bound.")
    if caps["supported"]:
        proof = caps["stop_proof"]
        if proof is None:
            _bad("supported capabilities carry no stop proof.")
        if proof["stop_latency_seconds"] <= 0:
            _bad("The stop proof asserts no positive stop latency.")
        if not caps["test_kinds"] or not caps["checks"]:
            _bad("supported capabilities advertise no test kinds or checks.")
        if not any(model["installable"] for model in caps["models"]):
            _bad("supported capabilities offer no installable model.")
        if not any(fset["fixtures"] for fset in caps["fixture_sets"]):
            _bad("supported capabilities offer no usable fixture set.")
    elif caps["reason"] is None:
        _bad("Unsupported capabilities carry no reason.")
    seen_models = set()
    for model in caps["models"]:
        identity = model["model"]["id"]
        if identity in seen_models:
            _bad(f"Model {identity} is declared twice.")
        seen_models.add(identity)
        if not model["installable"] and model["reason"] is None:
            _bad(f"Model {identity} is not installable and carries no reason.")
        range_bounds = model["settings_bounds"]
        for name in ("width_px", "height_px", "steps", "seed", "reference_count", "prompt_characters"):
            bound = range_bounds[name]
            if bound["minimum"] > bound["maximum"]:
                _bad(f"The advertised {name} range is inverted.")
        if range_bounds["guidance"] is not None:
            if range_bounds["guidance"]["minimum"] > range_bounds["guidance"]["maximum"]:
                _bad("The advertised guidance range is inverted.")
    seen_sets = set()
    for fset in caps["fixture_sets"]:
        if fset["id"] in seen_sets:
            _bad(f"Fixture set {fset['id']} is declared twice.")
        seen_sets.add(fset["id"])
        if len(fset["fixtures"]) > bounds["fixtures"]:
            _bad(f"Fixture set {fset['id']} exceeds the fixture wire bound.")
        if len(fset["prompt_pattern"]) > bounds["prompt_pattern_code_points"]:
            _bad(f"Fixture set {fset['id']} prompt pattern exceeds the wire bound.")
        fixture_ids = [fixture["fixture_id"] for fixture in fset["fixtures"]]
        if len(set(fixture_ids)) != len(fixture_ids):
            _bad(f"Fixture set {fset['id']} repeats a fixture id.")
        packaged = set(fixture_ids)
        for wanted in fset["reference_order"]:
            if wanted not in packaged:
                _bad(f"Fixture set {fset['id']} orders {wanted} but does not package it.")
    policy = caps["safety_policy"]["requested_limits"]
    units = {"max_runtime_seconds": "seconds",
             "max_process_tree_rss_bytes": "bytes",
             "max_system_swap_growth_bytes": "bytes",
             "test_start_min_free_bytes": "bytes"}
    for field, unit in units.items():
        bound = policy[field]
        if bound["minimum"] > bound["maximum"]:
            _bad(f"The packaged safety range {field} is inverted.")
        if bound["unit"] != unit:
            _bad(f"The packaged safety range {field} is declared in {bound['unit']}, not {unit}.")


def _approval_semantics(approval):
    fields = [change["field"] for change in approval["changes"]]
    if len(set(fields)) != len(fields):
        _violation("changes", "each measured field may appear once")
    for change in approval["changes"]:
        wanted = "seconds" if change["field"] == "measured_runtime_seconds" else "bytes"
        if change["unit"] != wanted:
            _violation(f"changes.{change['field']}", f"unit must be {wanted!r}")


def _build_prompt(pattern, characters):
    """repeat_pattern_then_codepoint_prefix_v1: repeat, then take exactly N code points."""
    if characters <= 0:
        return ""
    parts, filled = [], 0
    while filled < characters:
        parts.append(pattern)
        filled += len(pattern)
    return "".join(parts)[:characters]


def _model_for(capabilities, model):
    return next((entry for entry in capabilities["models"] if entry["model"] == model), None)


def validate_request(request, capabilities):
    """The submit gate: supported/verified authority, exact pins, live bounds, fixture and prompt identity."""
    validate(request, "request")
    validate(capabilities, "capabilities")
    if not capabilities["supported"]:
        reason = capabilities["reason"] or {}
        raise DeveloperTestError(503, "UNSUPPORTED",
                                 reason.get("detail") or "This Mac does not support Developer Mode tests.")
    setting = capabilities["setting"]
    if not setting["enabled"]:
        raise DeveloperTestError(503, "UNVERIFIED", "Developer Mode is Off on this Mac.")
    if not setting["verified"]:
        raise DeveloperTestError(503, "UNVERIFIED", "This Mac's Developer Mode setting is unverified.")
    if request["node_setting_revision"] != setting["revision"]:
        raise DeveloperTestError(409, "STALE_SETTING",
                                 "The request pins a different Developer Mode setting revision.")
    if (request["runner_id"] != capabilities["runner_id"]
            or request["runner_revision"] != capabilities["runner_revision"]
            or request["runner_sha256"] != capabilities["runner_sha256"]):
        raise DeveloperTestError(409, "PIN_CONFLICT",
                                 "The runner identity changed since these capabilities were read.")
    if request["kind"] not in capabilities["test_kinds"]:
        raise DeveloperTestError(409, "KIND_NOT_ADVERTISED", "This Mac does not offer that test kind.")
    model = _model_for(capabilities, request["model"])
    if model is None:
        raise DeveloperTestError(409, "MODEL_UNAVAILABLE",
                                 "This Mac does not declare that model revision.")
    if not model["installable"]:
        reason = model["reason"] or {}
        raise DeveloperTestError(503, "UNSUPPORTED",
                                 reason.get("detail") or "That model cannot be installed here.")
    settings = request["settings"]
    fset = next((fset for fset in capabilities["fixture_sets"]
                 if fset["id"] == settings["fixture_set_id"]), None)
    if fset is None:
        raise DeveloperTestError(409, "FIXTURE_UNAVAILABLE", "This Mac does not package that fixture set.")
    if request["fixture_set_sha256"] != fset["sha256"]:
        raise DeveloperTestError(409, "PIN_CONFLICT",
                                 "The fixture set bytes changed since these capabilities were read.")
    range_bounds = model["settings_bounds"]
    for name in ("width_px", "height_px", "steps", "seed", "reference_count", "prompt_characters"):
        bound = range_bounds[name]
        if not bound["minimum"] <= settings[name] <= bound["maximum"]:
            raise DeveloperTestError(400, "OUT_OF_RANGE",
                                     f"settings.{name}={settings[name]} is outside "
                                     f"{bound['minimum']}..{bound['maximum']}.")
    multiple = range_bounds["dimension_multiple"]
    if settings["width_px"] % multiple or settings["height_px"] % multiple:
        raise DeveloperTestError(400, "INVALID_SETTINGS",
                                 "Image dimensions are not a multiple of the model's dimension_multiple.")
    guidance_bounds = range_bounds["guidance"]
    if guidance_bounds is None:
        if settings["guidance"] is not None:
            raise DeveloperTestError(400, "INVALID_SETTINGS",
                                     "This model advertises no guidance range; guidance must be null.")
    else:
        if settings["guidance"] is None:
            raise DeveloperTestError(400, "INVALID_SETTINGS",
                                     "This model advertises a guidance range; guidance is required.")
        if not guidance_bounds["minimum"] <= settings["guidance"] <= guidance_bounds["maximum"]:
            raise DeveloperTestError(400, "OUT_OF_RANGE",
                                     f"settings.guidance={settings['guidance']} is outside "
                                     f"{guidance_bounds['minimum']}..{guidance_bounds['maximum']}.")
    if settings["memory_mode"] not in model["memory_modes"]:
        raise DeveloperTestError(400, "INVALID_SETTINGS",
                                 "That memory mode is not offered by this engine.")
    count = settings["reference_count"]
    order = fset["reference_order"]
    if count > len(order):
        raise DeveloperTestError(400, "OUT_OF_RANGE",
                                 "reference_count exceeds the packaged reference order.")
    refs = request["input_refs"]
    if len(refs) != count:
        raise DeveloperTestError(400, "FIXTURE_ORDER_MISMATCH",
                                 "input_refs must hold exactly reference_count entries.")
    if len(refs) > capabilities["bounds"]["input_refs"]:
        raise DeveloperTestError(400, "OUT_OF_RANGE", "input_refs exceeds the published wire bound.")
    if [ref["fixture_id"] for ref in refs] != order[:count]:
        raise DeveloperTestError(400, "FIXTURE_ORDER_MISMATCH",
                                 "input_refs must follow the packaged reference order exactly.")
    packaged = {fixture["fixture_id"]: fixture for fixture in fset["fixtures"]}
    for ref in refs:
        source = packaged.get(ref["fixture_id"])
        if source is None:
            _bad(f"Fixture set {fset['id']} references unpackaged fixture {ref['fixture_id']}.")
        if ref["sha256"] != source["sha256"] or ref["size_bytes"] != source["size_bytes"]:
            raise DeveloperTestError(400, "FIXTURE_MISMATCH",
                                     f"{ref['fixture_id']} does not match its packaged digest and size.")
    prompt = _build_prompt(fset["prompt_pattern"], settings["prompt_characters"])
    if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != settings["prompt_sha256"]:
        raise DeveloperTestError(400, "PROMPT_DIGEST_MISMATCH",
                                 "prompt_sha256 does not match repeat_pattern_then_codepoint_prefix_v1.")
    policy = capabilities["safety_policy"]["requested_limits"]
    for field, wanted in request["requested_limits"].items():
        bound = policy[field]
        if not bound["minimum"] <= wanted <= bound["maximum"]:
            raise DeveloperTestError(400, "OUT_OF_RANGE",
                                     f"requested_limits.{field}={wanted} is outside the packaged range "
                                     f"{bound['minimum']}..{bound['maximum']}.")
    return request


def validate_status(status, request, capabilities):
    """Status identity bindings against the stable node/controller and the pinned request.

    The idempotency key, raw request hash and attempt id are checked by the ledger
    that owns them; this helper owns the parts derivable from these three values.
    """
    validate(status, "status")
    validate(capabilities, "capabilities")
    if status["node_id"] != capabilities["node_id"] or status["controller_id"] != capabilities["controller_id"]:
        _identity("The status answers for a different node or controller.")
    if status["run_id"] != request["run_id"] or status["test_id"] != request["test_id"]:
        _identity("The status does not bind this run and test.")
    if status["state"] == "passed":
        if status["reason"] is not None:
            _bad("A passed status carries a reason.")
        if status["receipt_sha256"] is None:
            _bad("A passed status carries no receipt identity.")
    elif status["reason"] is None:
        _bad(f"A {status['state']} status carries no reason.")
    if not status["accepted"] and status["state"] != "not_run":
        _bad("An unaccepted attempt cannot be in a working state.")
    return status


def validate_receipt(receipt, request, capabilities):
    """Receipt identity bindings plus the normative measurement/evidence rules.

    The receipt's own byte hash and its attempt/key cross-check against the status
    belong to the collector that holds both; this helper owns everything derivable
    from the receipt, the request and the capability snapshot.
    """
    validate(receipt, "receipt")
    validate(capabilities, "capabilities")
    for field in ("run_id", "test_id", "node_setting_revision", "runner_id", "runner_sha256",
                  "runner_revision", "fixture_set_sha256", "kind"):
        if receipt[field] != request[field]:
            _identity(f"receipt {field} does not bind the request.")
    if (receipt["model"]["id"] != request["model"]["id"]
            or receipt["model"]["revision"] != request["model"]["revision"]):
        _identity("receipt model does not bind the request.")
    if receipt["settings"] != request["settings"]:
        _identity("receipt settings do not bind the request.")
    if receipt["node_id"] != capabilities["node_id"] or receipt["controller_id"] != capabilities["controller_id"]:
        _identity("receipt answers for a different node or controller.")
    model = _model_for(capabilities, request["model"])
    if model is None:
        _identity("The capability snapshot no longer declares the receipt's model.")
    for field in ("package_id", "manifest_sha256", "engine"):
        if receipt["model"][field] != model[field]:
            _identity(f"receipt model {field} does not match its packaged capability.")
    metadata = receipt["measurement_metadata"]
    if metadata["requested_limits"] != request["requested_limits"]:
        _identity("The receipt's accepted limits do not bind the request.")
    policy = capabilities["safety_policy"]
    if (metadata["safety_policy"]["revision"] != policy["revision"]
            or metadata["safety_policy"]["sha256"] != policy["sha256"]):
        _identity("The receipt's safety policy does not match the packaged capability policy.")
    outcome = receipt["outcome"]
    if outcome == "passed":
        if receipt["reason"] is not None:
            _bad("A passed receipt carries a reason.")
    elif receipt["reason"] is None:
        _bad(f"A {outcome} receipt carries no reason.")
    release = receipt["resource_release"]
    if not release["confirmed"] or release["confirmed_at"] is None:
        _bad("A terminal receipt claims an unconfirmed resource release.")
    installation = receipt["installation"]
    if not model["installed"] and not installation["required"]:
        _bad("The receipt runs an uninstalled model without recording installation as required.")
    if installation["completed"] and installation["verified_manifest_sha256"] != receipt["model"]["manifest_sha256"]:
        _bad("A completed installation verifies a different manifest.")
    measurements = receipt["measurements"]
    hardware = receipt["hardware"]
    if outcome == "passed":
        for field in ("elapsed_seconds", "generation_seconds", "peak_process_memory_bytes",
                      "system_swap_baseline_bytes", "system_swap_peak_bytes", "swap_growth_bytes"):
            if measurements[field] is None:
                _incomplete(f"passed receipt lacks the required measurement {field}.")
        if metadata["available_memory_before_start_bytes"] is None or metadata["preflight_observed_at"] is None:
            _incomplete("passed receipt lacks the required preflight memory observation.")
        for field in ("chip_family", "chip_model", "ram_bytes", "os_version", "os_build"):
            if hardware[field] is None:
                _incomplete(f"passed receipt lacks the required hardware field {field}.")
        if installation["required"] and not (installation["completed"]
                                             and installation["verified_manifest_sha256"] == receipt["model"]["manifest_sha256"]):
            _incomplete("passed receipt lacks verified installation.")
        output = receipt["output"]
        if output is None:
            _incomplete("passed receipt carries no validated output.")
    _check_metric_reasons(receipt)
    if metadata["available_memory_before_start_bytes"] is not None and metadata["preflight_observed_at"] is None:
        _bad("A preflight memory observation carries no observation time.")
    elapsed, generation = measurements["elapsed_seconds"], measurements["generation_seconds"]
    if elapsed is not None and generation is not None and generation > elapsed:
        _bad("generation_seconds exceeds elapsed_seconds.")
    base = measurements["system_swap_baseline_bytes"]
    peak = measurements["system_swap_peak_bytes"]
    growth = measurements["swap_growth_bytes"]
    if base is not None and peak is not None:
        if peak < base:
            _bad("The sampled swap peak is below its baseline.")
        if growth is not None and growth != max(0, peak - base):
            _bad("swap_growth_bytes is not max(0, peak-baseline).")
    elif growth is not None:
        _bad("swap_growth_bytes is reported without a complete baseline and peak.")
    try:
        started = _timestamp(receipt["started_at"])
        finished = _timestamp(receipt["finished_at"])
    except ValueError as exc:
        _bad(f"The receipt timestamps are not UTC Z instants: {exc}.")
    if finished < started:
        _bad("finished_at precedes started_at.")
    preflight_at = metadata["preflight_observed_at"]
    if preflight_at is not None:
        try:
            observed = _timestamp(preflight_at)
        except ValueError as exc:
            _bad(f"The preflight timestamp is not a UTC Z instant: {exc}.")
        if observed < started or observed > finished:
            _bad("The preflight observation cannot be ordered between attempt start and finish.")
    checks = receipt["checks"]
    names = [check["name"] for check in checks]
    if len(set(names)) != len(names):
        _bad("Receipt checks are not unique.")
    advertised = set(capabilities["checks"])
    unknown = set(names) - advertised
    if unknown:
        _bad("Receipt reports checks this Mac does not advertise: " + ", ".join(sorted(unknown)) + ".")
    for check in checks:
        if check["passed"] and check["reason"] is not None:
            _bad(f"Passing check {check['name']} carries a reason.")
        if not check["passed"] and check["reason"] is None:
            _bad(f"Failing check {check['name']} carries no reason.")
    if outcome == "passed":
        if not names:
            _incomplete("passed receipt reports no checks.")
        missing = advertised - set(names)
        if missing:
            _incomplete("passed receipt lacks advertised checks: " + ", ".join(sorted(missing)) + ".")
        if any(not check["passed"] for check in checks):
            _bad("A passed receipt contains a failing check.")
    output = receipt["output"]
    if output is not None and (output["width_px"], output["height_px"]) != (
            receipt["settings"]["width_px"], receipt["settings"]["height_px"]):
        _bad("Output dimensions do not equal the requested settings.")
    if outcome == "not_run":
        if measurements["generation_seconds"] is not None:
            _bad("A not_run receipt claims generation time.")
        if output is not None:
            _bad("A not_run receipt claims an output artifact.")
    return receipt


def _check_metric_reasons(receipt):
    """Every null observation has exactly one reason naming it; observed fields carry none."""
    entries = [entry["field"] for entry in receipt["metric_unavailable_reasons"]]
    if len(set(entries)) != len(entries):
        _bad("metric_unavailable_reasons holds duplicate fields.")
    nulls = {(f"hardware.{field}" if container == "hardware" else field)
             for container, field in _METRIC_FIELDS
             if receipt[container][field] is None}
    missing = nulls - set(entries)
    extra = set(entries) - nulls
    if missing:
        _bad("Null observations without a metric_unavailable_reasons entry: " + ", ".join(sorted(missing)) + ".")
    if extra:
        _bad("metric_unavailable_reasons entries for observed values: " + ", ".join(sorted(extra)) + ".")
