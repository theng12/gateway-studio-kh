"""Closed Hub inventory survives the existing Fleet join without invented facts."""
import copy
import unittest

from tests._isolation import sandbox_env
PROFILE = sandbox_env('gate-inventory-')

from gateway import fleet
from tests.gate_fixtures import make_inventory


class DeveloperGateInventoryProjectionTests(unittest.TestCase):
    def test_valid_inventory_is_preserved_exactly(self):
        sample = make_inventory()
        self.assertEqual(fleet.project_developer_gate_inventory(sample), sample)

    def test_unknown_values_stay_null_and_stated_zero_survives(self):
        sample = make_inventory(free_bytes=None)
        drive = sample['internal_physical_drive']
        for key in ('device_id', 'internal', 'physical', 'size_bytes', 'workspace_backing_device_id'):
            drive[key] = None
        drive.update(source='unknown', backing_source='unknown')
        sample['developer_gate_space'].update(workspace_bytes=0, result_spool_bytes=None,
            shared_cache_bytes=None, workspace_limit_bytes=None, result_spool_limit_bytes=None,
            shared_cache_limit_bytes=None)
        self.assertEqual(fleet.project_developer_gate_inventory(sample), sample)

    def test_malformed_or_extra_facts_refuse_instead_of_becoming_readback(self):
        sample = make_inventory()
        for field, value in [('free_bytes', -1), ('free_bytes', True), ('node_id', '../other'),
                             ('observed_at', 'not-a-timeZ'), ('extra_grant', True)]:
            with self.subTest(field=field, value=value):
                bad = copy.deepcopy(sample)
                bad[field] = value
                self.assertIsNone(fleet.project_developer_gate_inventory(bad))
        for provenance in (None, 'requested', ''):
            bad = copy.deepcopy(sample)
            bad['toolchains'][0]['provenance'] = provenance
            self.assertIsNone(fleet.project_developer_gate_inventory(bad))

    def test_missing_or_wrong_format_is_unknown(self):
        for value in (None, {}, 'not-a-dict', {'format': 'wrong'}):
            self.assertIsNone(fleet.project_developer_gate_inventory(value))


class MachinesFlatGateInventoryIntegrationTests(unittest.TestCase):
    def row(self):
        return {'id': 'c1', 'label': 'Fixture site', 'local': False, 'reachable': True,
                'machines': [{'machine': 'n1', 'reachable': True, 'memory_gb': 16}],
                'nodes': [], 'activity': {'machines': [{'machine': 'n1', 'state': 'ready'}], 'pulse': {}}}

    def test_inventory_joins_from_machine(self):
        row = self.row()
        inv = make_inventory()
        row['machines'][0]['developer_gate_inventory'] = inv
        self.assertEqual(fleet.machines_flat([row])[0]['developer_gate_inventory'], inv)

    def test_inventory_joins_from_node(self):
        row = self.row()
        inv = make_inventory()
        row['nodes'] = [{'id': 'n1', 'state': 'reachable', 'developer_gate_inventory': inv}]
        self.assertEqual(fleet.machines_flat([row])[0]['developer_gate_inventory'], inv)

    def test_absence_is_unknown(self):
        self.assertIsNone(fleet.machines_flat([self.row()])[0]['developer_gate_inventory'])


if __name__ == '__main__':
    unittest.main()
