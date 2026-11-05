"""Scoped late-label discovery and verified-asset admission, with no Hub I/O."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import struct
import unittest
import zlib
from dataclasses import replace
from unittest.mock import patch

from tests import test_render_service as fixtures
from gateway.render_contract import LATE_LABEL_CONTRACT, LATE_LABEL_MOTIONS
from gateway.render_hash_v1 import render_hash
from gateway.render_png import validate_cue_png
from gateway.render_service import FinalRenderService, PrivateRenderUnavailable


def chunk(kind, payload):
    return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))


def png(width=240, height=80, *, color=6, depth=8, interlace=0, raw=None, extra=b''):
    header = struct.pack('>IIBBBBB', width, height, depth, color, 0, 0, interlace)
    rows = raw if raw is not None else (b'\0' + b'\0\0\0\0' * width) * height
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + extra
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


class CuePngTests(unittest.TestCase):
    def check(self, data):
        return validate_cue_png(io.BytesIO(data), max_width=1920, max_height=1080)

    def test_rgba_png_validates_in_bounded_blocks(self):
        self.assertEqual(self.check(png()), (240, 80))
        self.assertEqual(self.check(png(1920, 1080)), (1920, 1080))

    def test_invalid_encoding_dimensions_crc_animation_and_compressed_rows_refuse(self):
        payload = png()
        cases = [b'not PNG', payload[:-1], payload + b'x', png(color=2), png(depth=16),
                 png(interlace=1), png(width=1921), png(width=0),
                 png(extra=chunk(b'acTL', b'\0' * 8)), png(extra=chunk(b'PLTE', b'\0' * 3)),
                 png(raw=b'\0'), png(raw=(b'\5' + b'\0' * 960) * 80),
                 payload[:20] + bytes([payload[20] ^ 1]) + payload[21:],
                 png(raw=b'\0' * (961 * 81))]
        for index, data in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.check(data)


class LateLabelServiceTests(fixtures.RenderServiceCase):
    def setUp(self):
        super().setUp()
        profile = json.loads(fixtures.PROFILE_RAW)
        profile.update(format='studio.render.profile.v3', implementation='story-classic-late-label-v3',
                       combination_set='retained-classic-late-label-candidate-v3', production_eligible=False)
        self.late_raw = fixtures.canonical(profile)
        self.late_digest = hashlib.sha256(self.late_raw).hexdigest()
        self.service.late_label_profile_descriptor = self.late_raw
        self.service.late_label_profile = profile
        self.service.late_label_profile_digest = self.late_digest
        self.service.late_label_profile_facts = copy.deepcopy(self.service.profile_facts)
        capability = {**self.destination.capability_snapshot, 'profile_digest': self.late_digest}
        self.destination = replace(self.destination, capability_snapshot=capability,
                                   capability_snapshot_sha256=hashlib.sha256(fixtures.canonical(capability)).hexdigest())

    def cue_request(self, payload=None, *, placement=None, profile_digest=None):
        value = json.loads(self.request())
        entry, handle = self.publish('cue_0000_label', png() if payload is None else payload, 'png')
        entry['role'] = 'cue_label'
        value['packet']['manifest'].append(entry)
        value['packet']['manifest'].sort(key=lambda item: item['logical_id'])
        value['assets'][entry['logical_id']] = handle
        recipe = value['packet']['recipe']
        recipe['profile_digest'] = profile_digest or self.late_digest
        recipe['operations'] = [{
            'kind': 'late_label_stage', 'contract': LATE_LABEL_CONTRACT, 'cue_id': 'cue_01',
            'enable': {'start_sec': 0.5, 'end_sec': 2.5},
            'placement': placement or {'x_px': 120, 'y_px': 160},
            'motion': {'kind': 'pop', 'entrance_sec': 0.3},
            'label': {'asset': {'type': 'asset_ref', 'logical_id': 'cue_0000_label'}},
            'reveal': None, 'line': None, 'line_reveal': None,
        }]
        return self.seal(value)

    def seal(self, value):
        packet = value['packet']
        packet['recipe_digest'] = render_hash('recipe', packet['recipe'])
        packet['manifest_digest'] = render_hash('manifest', packet['manifest'])
        packet['packet_digest'] = render_hash('packet', {
            'recipe_digest': packet['recipe_digest'], 'manifest_digest': packet['manifest_digest']})
        return fixtures.canonical(value)

    def test_discovery_keeps_legacy_shape_and_selects_new_descriptor_only_when_scoped(self):
        with patch('gateway.render_service.fleet.render_candidates', return_value=[]) as candidates:
            legacy = self.service.capabilities(fixtures.principal())
            self.assertNotIn('late_labels', legacy)
            self.assertEqual(legacy['profile_digest'], hashlib.sha256(fixtures.PROFILE_RAW).hexdigest())
            scoped = self.service.capabilities(fixtures.principal(), requested_contract=LATE_LABEL_CONTRACT)
            self.assertEqual(scoped['profile_digest'], self.late_digest)
            self.assertEqual(scoped['late_labels'], {'format': LATE_LABEL_CONTRACT, 'motions': list(LATE_LABEL_MOTIONS)})
            self.assertEqual(set(scoped), set(legacy) | {'late_labels'})
            self.assertEqual(candidates.call_args.args[0], self.late_digest)
            self.assertEqual(scoped['eligible'], 0)

    def test_scoped_discovery_failure_is_not_reported_as_measured_zero_support(self):
        with patch('gateway.render_service.fleet.render_candidates', side_effect=ValueError('unreadable fleet')):
            with self.assertRaises(PrivateRenderUnavailable):
                self.service.capabilities(fixtures.principal(), requested_contract=LATE_LABEL_CONTRACT)

    def test_missing_late_descriptor_fails_closed_without_disabling_legacy(self):
        self.service.late_label_profile_descriptor = None
        with self.assertRaises(PrivateRenderUnavailable):
            self.service.capabilities(fixtures.principal(), requested_contract=LATE_LABEL_CONTRACT)
        with patch('gateway.render_service.fleet.render_candidates', return_value=[]):
            self.assertNotIn('late_labels', self.service.capabilities(fixtures.principal()))

    def test_cue_png_is_admitted_and_exact_replay_stays_on_original_lineage(self):
        raw = self.cue_request()
        accepted = self.service.accept(fixtures.principal(), 'late-cue-attempt', raw)
        self.destination = replace(self.destination, node_id='different-node')
        with patch.object(self.service, 'destination_selector', side_effect=AssertionError('must not reroute')):
            replay = self.service.accept(fixtures.principal(), 'late-cue-attempt', raw)
        self.assertEqual(replay['id'], accepted['id'])
        self.assertTrue(replay['idempotent_replay'])
        self.assertEqual(self.hub.calls, [])

    def test_legacy_profile_cannot_admit_new_operation(self):
        raw = self.cue_request(profile_digest=hashlib.sha256(fixtures.PROFILE_RAW).hexdigest())
        self.assert_code('RENDER_REQUEST_INVALID', self.service.accept, fixtures.principal(), 'legacy-cue', raw)

    def test_invalid_png_and_outside_canvas_refuse_before_destination_or_intent(self):
        for index, raw in enumerate((self.cue_request(b'fake PNG'),
                                     self.cue_request(placement={'x_px': 1900, 'y_px': 100}))):
            with patch.object(self.service, 'destination_selector', side_effect=AssertionError('must not route')):
                self.assert_code('RENDER_CUE_ASSET_INVALID', self.service.accept,
                                 fixtures.principal(), f'invalid-cue-{index}', raw)
            self.assertIsNone(self.store.get_render_staging_by_attempt_key(
                owner_scope=fixtures.OWNER, gateway_attempt_key=f'invalid-cue-{index}'))

    def test_wrong_destination_profile_or_compiler_cannot_admit_cues(self):
        raw = self.cue_request()
        for key in ('profile_digest', 'compiler_digest', 'tools_digest'):
            capability = {**self.destination.capability_snapshot, key: 'f' * 64}
            destination = replace(self.destination, capability_snapshot=capability,
                                  capability_snapshot_sha256=hashlib.sha256(fixtures.canonical(capability)).hexdigest())
            with patch.object(self.service, 'destination_selector', return_value=destination):
                self.assert_code('RENDER_DESTINATION_UNAVAILABLE', self.service.accept,
                                 fixtures.principal(), 'wrong-' + key, raw)


if __name__ == '__main__':
    unittest.main()
