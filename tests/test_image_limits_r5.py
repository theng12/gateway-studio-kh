"""r5 producer-snapshot consumption: one effective row per node/model/mode.

No network and no real profile: the r5 ``hub_memory_admission`` annotation is supplied as
fixture bytes exactly as the Hub publishes it.
"""
from __future__ import annotations

import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from _isolation import sandbox_env
_SANDBOX = sandbox_env('gateway-image-limits-r5-')
from gateway import catalog, image_limits

MODEL = 'maker/fixture-4bit'
REV = 'a' * 64
COVERS = ['node_id', 'model', 'mode', 'physical_tier', 'physical_tier_gb',
          'total_memory_bytes', 'ram effective values', 'reference rows']
_ABSENT = object()
DEFINITION = SimpleNamespace(id='studio-hub-site', display_name='Fixture site', kind='studio_hub')


def row(count, *, allowed=True, maximum=4, swap=0.0):
    return {
        'reference_count': count,
        'effective': {'allowed': allowed, 'max_reference_images': maximum,
                      'swap_allowance_gb': swap},
        'sources': {'allowed': 'published', 'max_reference_images': 'published',
                    'swap_allowance_gb': 'published'},
        'unknown_reasons': {'allowed': None, 'max_reference_images': None,
                            'swap_allowance_gb': None},
        'published': None, 'override': None, 'max_intrinsic': None,
        'clamped_reason': None, 'overridden': False, 'stale_tier_ignored': False,
    }


def view(mode, rows, *, revision=REV, available=True, reason=None, tier='24'):
    return {'available': available, 'reason': reason, 'physical_tier': tier,
            'physical_tier_gb': int(tier) if tier != '<8' else None,
            # 24 GiB is tier '24' under the producer's own reference_tier arithmetic.
            'total_memory_bytes': 24 * 1024 ** 3, 'mode': mode,
            'intrinsic_max': None, 'intrinsic_unknown_reason': 'fixture',
            'rows': rows, 'revision': revision, 'revision_covers': list(COVERS)}


def snapshot(node='node-a', model=MODEL, *, edit_rows=None, txt_rows=None,
             revision=REV, ram_revision=REV, txt_revision=REV,
             available=True, reason=None, tier='24'):
    edit = view('edit', edit_rows if edit_rows is not None else [row(None, maximum=4)],
                revision=revision, available=available, reason=reason, tier=tier)
    txt = view('txt2img', txt_rows if txt_rows is not None else [row(None, maximum=0)],
               revision=txt_revision, available=available, reason=reason, tier=tier)
    return {
        'revision': 'b' * 64,
        'revision_covers': list(COVERS),
        'node_id': node,
        'model': model,
        'ram': {
            'model_wide': {'model': model, 'mode': 'txt2img',
                           'revision': 'b' * 64, 'revision_covers': list(COVERS)},
            'modes': {
                'edit': {'model': model, 'mode': 'edit', 'revision': ram_revision,
                         'revision_covers': list(COVERS)},
                'txt2img': {'model': model, 'mode': 'txt2img', 'revision': txt_revision,
                            'revision_covers': list(COVERS)},
            },
        },
        'reference': {'edit': edit, 'txt2img': txt},
    }


def worker(machine='node-a', *, snapshot_value=_ABSENT, legacy_max=4):
    tiers = {'24': {'allowed': True, 'max_reference_images': legacy_max,
                    'swap_allowance_gb': 0, 'source': 'fixture'}}
    table = {'schema': image_limits.SCHEMA, 'revision': 'legacy',
             'swap_allowance_units': 'fixture',
             'rows': [{'model_id': MODEL, 'mode': mode, 'reference_count': None,
                       'tiers': copy.deepcopy(tiers)} for mode in ('txt2img', 'edit')]}
    value = {'hub_machine': machine, 'hub_cached': True, 'runtime_ready': True,
             'memory_eligible': True, 'available': True,
             'lane_contract': {'reference_limits_table': table,
                               'references': {'tier': '24'}},
             'image_modes': [{'id': 'txt2img', 'available': True},
                             {'id': 'edit', 'available': True,
                              'references': {'min': 1, 'max': legacy_max, 'tier': '24'}}]}
    if snapshot_value is not _ABSENT:
        value['hub_memory_admission'] = snapshot_value
    return value


def request(count):
    return {'_reference_digests': list(range(count))}


class SnapshotTests(unittest.TestCase):
    def test_present_snapshot_replaces_legacy_grant_and_preserves_unknown(self):
        # The legacy table grants 4; the producer's generic row states no cap.
        raw = {'image_workers': [worker(snapshot_value=snapshot(
            edit_rows=[row(None, maximum=None)]), legacy_max=4)]}
        decision = image_limits.assess(raw, MODEL, request(3))
        self.assertIsNotNone(decision)
        self.assertFalse(decision['allowed'])
        self.assertIn('does not state a reference-image cap', decision['reason'])
        self.assertNotIn('at most 4', decision['reason'])

    def test_exact_row_false_zero_and_null_keep_their_meaning(self):
        cases = [
            ([row(None, maximum=2), row(2, allowed=False, maximum=0)], 'Off'),
            ([row(None, maximum=2), row(2, allowed=None, maximum=2)], 'unknown'),
            ([row(None, maximum=0)], 'at most 0'),
            ([row(None, maximum=2, swap=None)], 'swap allowance (unknown)'),
        ]
        for rows, expected in cases:
            with self.subTest(expected=expected):
                raw = {'image_workers': [worker(snapshot_value=snapshot(edit_rows=rows))]}
                decision = image_limits.assess(raw, MODEL, request(2 if len(rows) > 1 else 1))
                self.assertFalse(decision['allowed'])
                self.assertIn(expected, decision['reason'])

    def test_present_invalid_snapshot_refuses_instead_of_falling_back(self):
        cases = {
            'node': (snapshot(node='node-b'), 'not this node'),
            'model': (snapshot(model='other/model'), "names model 'other/model'"),
            'ram token': (snapshot(ram_revision='c' * 64), 'do not match'),
            'revision': (snapshot(revision='short'), 'no valid mode revision'),
            'unavailable': (snapshot(available=False, reason='the published table is unreadable'),
                            'unavailable: the published table is unreadable'),
            'tier band': (snapshot(), 'disagrees with its band'),
        }
        for name, (value, expected) in cases.items():
            if name == 'tier band':
                value['reference']['edit']['physical_tier_gb'] = 16
            with self.subTest(name=name):
                raw = {'image_workers': [worker(snapshot_value=value, legacy_max=4)]}
                decision = image_limits.assess(raw, MODEL, request(2))
                self.assertIsNotNone(decision)
                self.assertFalse(decision['allowed'])
                self.assertIn(expected, decision['reason'])

    def test_malformed_count_identities_fail_closed(self):
        base = row(None, maximum=4)
        cases = {
            'missing selector': ([{k: v for k, v in base.items() if k != 'reference_count'}],
                                 'without a reference_count identity'),
            'bool selector': ([base, row(True)], 'malformed reference_count'),
            'zero selector': ([base, row(0)], 'malformed reference_count'),
            'negative selector': ([base, row(-1)], 'malformed reference_count'),
            'string selector': ([base, row('2')], 'malformed reference_count'),
            'duplicate generic': ([base, row(None)], 'duplicate rows for the generic fallback'),
            'duplicate exact': ([base, row(2), row(2)], 'duplicate rows for 2 reference image(s)'),
        }
        for name, (rows, expected) in cases.items():
            with self.subTest(name=name):
                raw = {'image_workers': [worker(snapshot_value=snapshot(edit_rows=rows),
                                                legacy_max=4)]}
                decision = image_limits.assess(raw, MODEL, request(1))
                self.assertFalse(decision['allowed'])
                self.assertIn(expected, decision['reason'])

    def test_nonstring_identifiers_refuse_instead_of_matching_after_coercion(self):
        cases = {
            'node': (snapshot(node=123), 'non-string node id'),
            'model': (snapshot(model=456), 'non-string model'),
        }
        for name, (value, expected) in cases.items():
            with self.subTest(name=name):
                raw = {'image_workers': [worker(snapshot_value=value, legacy_max=4)]}
                decision = image_limits.assess(raw, MODEL, request(2))
                self.assertFalse(decision['allowed'])
                self.assertIn(expected, decision['reason'])
        # A numeric machine id must not coerce into the snapshot's node id.
        raw = {'image_workers': [worker(123, snapshot_value=snapshot(node='node-a'),
                                        legacy_max=4)]}
        decision = image_limits.assess(raw, MODEL, request(2))
        self.assertFalse(decision['allowed'])
        self.assertIn('machine id is not a string', decision['reason'])
        # The RAM view's model is an identifier too, never coerced.
        value = snapshot()
        value['ram']['modes']['edit']['model'] = 123
        raw = {'image_workers': [worker(snapshot_value=value, legacy_max=4)]}
        decision = image_limits.assess(raw, MODEL, request(2))
        self.assertFalse(decision['allowed'])
        self.assertIn('do not match', decision['reason'])

    def test_snapshot_tier_does_not_depend_on_legacy_table_bands(self):
        value = worker(snapshot_value=snapshot(tier='32'))
        for block in value['hub_memory_admission']['reference'].values():
            block['total_memory_bytes'] = 32 * 1024 ** 3
        # The legacy fixture has only tier 24; a valid r5 answer must not be rejected by it.
        raw = {'image_workers': [value]}
        self.assertTrue(image_limits.assess(raw, MODEL, request(1))['allowed'])
        value['lane_contract']['reference_limits_table']['rows'] = 123
        self.assertTrue(image_limits.assess(raw, MODEL, request(1))['allowed'])

    def test_malformed_physical_tier_refuses(self):
        for tier in ('unknown', '0', '-1', '024'):
            value = snapshot()
            value['reference']['edit'].update(physical_tier=tier, physical_tier_gb=None)
            with self.subTest(tier=tier):
                decision = image_limits.assess(worker(snapshot_value=value), MODEL, request(1))
                self.assertFalse(decision['allowed'])
                self.assertIn('invalid physical tier', decision['reason'])

    def test_host_memory_total_must_be_a_positive_number(self):
        value = worker(snapshot_value=snapshot())
        value['hub_memory_admission']['reference']['edit']['total_memory_bytes'] = True
        raw = {'image_workers': [value]}
        decision = image_limits.assess(raw, MODEL, request(1))
        self.assertFalse(decision['allowed'])
        self.assertIn('invalid host memory total', decision['reason'])

    def test_producer_tier_does_not_require_legacy_table(self):
        # The producer owns tier calculation even when no legacy table is published.
        value = worker(snapshot_value=snapshot(tier='24'))
        del value['lane_contract']
        value['hub_memory_admission']['reference']['edit']['total_memory_bytes'] = 32 * 1024 ** 3
        raw = {'image_workers': [value]}
        self.assertTrue(image_limits.assess(raw, MODEL, request(1))['allowed'])

    def test_remote_row_cannot_borrow_another_nodes_snapshot(self):
        raw = {'image_workers': [worker('node-a', snapshot_value=snapshot(node='node-b'))]}
        self.assertFalse(image_limits.assess(raw, MODEL, request(1))['allowed'])
        # A row that names no machine cannot prove the snapshot is its own.
        missing = {'image_workers': [worker('', snapshot_value=snapshot(node='node-a'))]}
        self.assertFalse(image_limits.assess(missing, MODEL, request(1))['allowed'])
        # The Hub's own row is published under the alias `local`; its producer snapshot
        # names the resolved node and is the row's own answer.
        local = {'image_workers': [worker('local', snapshot_value=snapshot(node='mac-1234'))]}
        self.assertTrue(image_limits.assess(local, MODEL, request(2))['allowed'])

    def test_contiguous_cap_stops_at_the_first_gap_and_exact_row_stays_true(self):
        value = snapshot(edit_rows=[row(None, maximum=4),
                                    row(3, allowed=False, maximum=0),
                                    row(4, allowed=True, maximum=4)])
        raw = {'image_workers': [worker(snapshot_value=value)]}
        published = image_limits.published(raw, MODEL)
        self.assertTrue(published['edit'])
        self.assertEqual(published['max'], 2)
        self.assertFalse(image_limits.assess(raw, MODEL, request(3))['allowed'])
        self.assertTrue(image_limits.assess(raw, MODEL, request(4))['allowed'])

    def test_absent_snapshot_keeps_the_legacy_table(self):
        raw = {'image_workers': [worker(snapshot_value=_ABSENT, legacy_max=2)]}
        self.assertTrue(image_limits.assess(raw, MODEL, request(2))['allowed'])
        self.assertEqual(image_limits.published(raw, MODEL), {'txt2img': True, 'edit': True, 'max': 2})

    def test_legacy_table_also_stops_at_the_first_gap(self):
        value = worker(legacy_max=4)
        table = value['lane_contract']['reference_limits_table']
        for count, allowed in ((3, False), (4, True)):
            exact = copy.deepcopy(table['rows'][1])
            exact['reference_count'] = count
            exact['tiers']['24'].update(allowed=allowed,
                                        max_reference_images=4 if allowed else 0)
            table['rows'].append(exact)
        raw = {'image_workers': [value]}
        self.assertEqual(image_limits.published(raw, MODEL)['max'], 2)
        self.assertTrue(image_limits.assess(raw, MODEL, request(4))['allowed'])

    def test_measurement_target_keeps_the_exact_node(self):
        raw = {'image_workers': [
            worker('node-a', snapshot_value=snapshot(node='node-a',
                                                     edit_rows=[row(None, maximum=1)])),
            worker('node-b', snapshot_value=snapshot(node='node-b',
                                                     edit_rows=[row(None, maximum=4)])),
        ]}
        self.assertTrue(image_limits.assess(raw, MODEL, request(4))['allowed'])
        pinned = {**request(4), 'measurement_target': {'machine': 'node-a'}}
        self.assertFalse(image_limits.assess(raw, MODEL, pinned)['allowed'])

    def test_catalogue_and_admission_share_the_snapshot_cap(self):
        value = snapshot(edit_rows=[row(None, maximum=4),
                                    row(3, allowed=False, maximum=0),
                                    row(4, allowed=True, maximum=4)])
        raw = {'image_workers': [worker(snapshot_value=value)]}
        catalogue = catalog._image_row(MODEL, DEFINITION, {'raw': raw})
        self.assertEqual(catalogue['max_reference_images'], 2)
        self.assertTrue(image_limits.assess(raw, MODEL, request(4))['allowed'])


if __name__ == '__main__':
    unittest.main()
