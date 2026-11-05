from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway.render_contract import LATE_LABEL_CONTRACT, validate_render_packet_syntax
from gateway.render_hash_v1 import render_hash
from test_render_contract import FULL_PACKET_JSON, packet as legacy_packet, raw


CONTRACT = LATE_LABEL_CONTRACT


def ref(logical_id):
    return {"type": "asset_ref", "logical_id": logical_id}


def label_op(cue_id="cue-a", motion="pop", index=0):
    label = {"asset": ref(f"cue_{index:04d}_label")}
    return {
        "kind": "late_label_stage", "contract": CONTRACT, "cue_id": cue_id,
        "enable": {"start_sec": 0.25, "end_sec": 1.5},
        "placement": {"x_px": 20, "y_px": 30},
        "motion": {"kind": motion, "entrance_sec": 0.3},
        "label": None if motion == "wipe" else label,
        "reveal": None, "line": None, "line_reveal": None,
    }


def asset_row(logical_id):
    return {"logical_id": logical_id, "safe_name": logical_id + ".png", "role": "cue_label",
            "sha256": "a" * 64, "size_bytes": 1}


def seal(value):
    value["manifest"].sort(key=lambda row: row["logical_id"])
    value["recipe_digest"] = render_hash("recipe", value["recipe"])
    value["manifest_digest"] = render_hash("manifest", value["manifest"])
    value["packet_digest"] = render_hash("packet", {
        "recipe_digest": value["recipe_digest"], "manifest_digest": value["manifest_digest"],
    })
    return value


def packet_with(*operations, assets=()):
    value = legacy_packet()
    value["recipe"]["operations"] = copy.deepcopy(list(operations))
    value["manifest"].extend(asset_row(name) for name in assets)
    return seal(value)


def reveal(purpose, operation_index=0, fps=24):
    count = max(2, (fps * 3 + 9) // 10 + 1)
    return {"duration_sec": 0.3, "frames": [
        {"asset": ref(f"cue_{operation_index:04d}_{purpose}{index:04d}"),
         "progress": index / (count - 1)}
        for index in range(count)
    ]}


class RenderLateLabelTests(unittest.TestCase):
    def assert_rejected(self, value, contract=CONTRACT):
        with self.assertRaisesRegex(ValueError, "Invalid render packet"):
            validate_render_packet_syntax(raw(value), contract=contract)

    def test_contract_gate_keeps_legacy_default_and_allows_empty_v3(self):
        old = legacy_packet()
        self.assertEqual(validate_render_packet_syntax(raw(old)), old)
        self.assertEqual(validate_render_packet_syntax(raw(old), contract=CONTRACT), old)
        with self.assertRaisesRegex(ValueError, "Invalid render packet"):
            validate_render_packet_syntax(raw(old).replace(b'"operations":[]', b'"operations":[{"kind":"late_label_stage"}]'))
        self.assert_rejected(old, contract="studio.render.profile.v3")

    def test_accepts_repeatable_late_labels_between_frame_and_logo(self):
        first = label_op("cue-a", index=0)
        second = label_op("cue-b", index=1)
        second["enable"]["start_sec"] = 0.5
        value = packet_with(first, second,
                            assets=("cue_0000_label", "cue_0001_label"))
        self.assertEqual(validate_render_packet_syntax(raw(value), contract=CONTRACT), value)

    def test_stage_is_after_frame_before_logo(self):
        value = json.loads(FULL_PACKET_JSON)
        frame_index = next(i for i, op in enumerate(value["recipe"]["operations"])
                           if op["kind"] == "presentation_frame")
        cue_index = frame_index + 1
        value["recipe"]["operations"].insert(cue_index, label_op(index=cue_index))
        value["manifest"].append(asset_row(f"cue_{cue_index:04d}_label"))
        self.assertEqual(validate_render_packet_syntax(raw(seal(value)), contract=CONTRACT)[
            "recipe"]["operations"][cue_index]["kind"], "late_label_stage")

        moved = value["recipe"]["operations"].pop(cue_index)
        value["recipe"]["operations"].append(moved)
        old_id, new_id = f"cue_{cue_index:04d}_label", f"cue_{len(value['recipe']['operations']) - 1:04d}_label"
        moved["label"]["asset"]["logical_id"] = new_id
        entry = next(row for row in value["manifest"] if row["logical_id"] == old_id)
        entry["logical_id"] = new_id
        entry["safe_name"] = new_id + ".png"
        self.assert_rejected(seal(value))

    def test_accepts_wipe_and_line_draw_reveals_with_exact_progress(self):
        wipe = label_op("wipe", "wipe")
        wipe["reveal"] = reveal("r")
        wipe_assets = tuple(f"cue_0000_r{i:04d}" for i in range(9))
        self.assertEqual(validate_render_packet_syntax(
            raw(packet_with(wipe, assets=wipe_assets)), contract=CONTRACT)["recipe"]["operations"][0], wipe)

        line = label_op("line", "line-draw")
        line["line_reveal"] = reveal("lr")
        line_assets = ("cue_0000_label",) + tuple(f"cue_0000_lr{i:04d}" for i in range(9))
        self.assertEqual(validate_render_packet_syntax(
            raw(packet_with(line, assets=line_assets)), contract=CONTRACT)["recipe"]["operations"][0], line)

    def test_rejects_unknown_fields_bad_windows_duplicate_cues_and_non_png_assets(self):
        op = label_op()
        op["raw_expression"] = "drawtext=..."
        self.assert_rejected(packet_with(op, assets=("cue_0000_label",)))

        for mutate in (
            lambda op: op["enable"].update(start_sec=-0.1),
            lambda op: op["enable"].update(end_sec=3.1),
            lambda op: op["placement"].update(x_px=1.5),
            lambda op: op["motion"].update(entrance_sec=0.31),
            lambda op: op.update(cue_id="Bad ID"),
        ):
            bad = label_op()
            mutate(bad)
            self.assert_rejected(packet_with(bad, assets=("cue_0000_label",)))

        duplicate = label_op("same")
        second = label_op("same", index=1)
        self.assert_rejected(packet_with(duplicate, second,
                                         assets=("cue_0000_label", "cue_0001_label")))

        bad = packet_with(label_op(), assets=("cue_0000_label",))
        bad["manifest"][-1]["safe_name"] = "cue_0000_label.jpg"
        self.assert_rejected(seal(bad))

    def test_rejects_inexact_reveal_count_progress_and_indexed_ids(self):
        op = label_op("wipe", "wipe")
        op["reveal"] = reveal("r")
        assets = tuple(f"cue_0000_r{i:04d}" for i in range(9))
        bad = packet_with(op, assets=assets)
        bad["recipe"]["operations"][0]["reveal"]["frames"].pop()
        self.assert_rejected(seal(bad))

        bad = packet_with(op, assets=assets)
        bad["recipe"]["operations"][0]["reveal"]["frames"][1]["progress"] = 0.2
        self.assert_rejected(seal(bad))

        bad_op = label_op()
        bad_op["label"]["asset"] = ref("some-user-path.png")
        self.assert_rejected(packet_with(bad_op, assets=("cue_0000_label", "some-user-path.png")))


if __name__ == "__main__":
    unittest.main()
