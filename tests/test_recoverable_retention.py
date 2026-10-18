"""Recoverable runtime retention; native recycling always stays in this sandbox."""
from __future__ import annotations
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._isolation import sandbox_env
_SANDBOX = sandbox_env('gateway-recoverable-retention-')
from gateway import retention, diskguard
_NATIVE_RECYCLE = retention._recycle

class Store:
    def protected_input_paths(self):
        return set()

class RecoverableRetentionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(_SANDBOX.name) / self._testMethodName
        self.artifacts = self.root / 'artifacts'
        self.trash = self.root / '_trash'
        self.legacy = self.root / 'trash'
        self.recycled = self.root / 'macos-trash'
        self.artifacts.mkdir(parents=True)
        self.recycled.mkdir()
        self.now = 1791342000.0
        for name, value in [('ARTIFACT_DIR', self.artifacts), ('TRASH_DIR', self.trash),
                            ('LEGACY_TRASH_DIR', self.legacy), ('SETTINGS_PATH', self.root/'retention.json'),
                            ('KEPT_PATH', self.root/'kept.json')]:
            p = patch.object(retention, name, value, create=True)
            p.start(); self.addCleanup(p.stop)
        self.calls = []
        def recycle(path):
            self.calls.append(path)
            path.rename(self.recycled / f'{len(self.calls)}-{path.name}')
            return True
        p = patch.object(retention, '_recycle', side_effect=recycle, create=True)
        self.native = p.start(); self.addCleanup(p.stop)

    def artifact(self, name='old.bin', age=30*86400, size=4096):
        p=self.artifacts/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(b'x'*size)
        os.utime(p,(self.now-age,self.now-age)); return p

    def retire(self, name, moment):
        path=self.artifact(name)
        self.assertTrue(retention._move_to_trash(path,moment))
        return next(self.trash.rglob(name))

    def test_retirement_age_and_exact_seven_day_boundary(self):
        moved=self.retire('ancient.bin',self.now)
        retention.prune(now=self.now+7*86400,store=Store())
        self.assertTrue(moved.exists()); self.assertEqual(self.calls,[])
        result=retention.prune(now=self.now+7*86400+1,store=Store())
        self.assertFalse(moved.exists()); self.assertEqual(result['recycled_files'],1)
        self.assertEqual(result['freed_bytes'],0)
        self.assertEqual(next(self.recycled.iterdir()).read_bytes(),b'x'*4096)

    def test_combined_cap_oldest_retirement_first_and_new_overflow_same_pass(self):
        legacy=self.legacy/retention._day(self.now-2*86400)/'legacy.bin'
        legacy.parent.mkdir(parents=True); legacy.write_bytes(b'l'*4096)
        old=self.retire('old.bin',self.now-86400)
        self.artifact('new.bin')
        with patch.object(retention,'trash_cap_bytes',return_value=4096):
            result=retention.prune(now=self.now,store=Store())
        self.assertEqual([p.name for p in self.calls],['legacy.bin','old.bin'])
        self.assertEqual(result['recycled_files'],2)
        self.assertTrue(next(self.trash.rglob('new.bin')).exists())
        self.assertFalse(old.exists())

    def test_failed_native_transfer_preserves_source_and_reports_failure(self):
        moved=self.retire('stuck.bin',self.now-8*86400)
        self.native.side_effect=None; self.native.return_value=False
        result=retention.prune(now=self.now,store=Store())
        self.assertTrue(moved.exists()); self.assertEqual(result['recycle_failures'],1)
        self.assertEqual(result['freed_bytes'],0); self.assertIn('macOS Trash',result['warning'])

    def test_legacy_age_uses_end_of_utc_day_and_ignores_source_mtime(self):
        born=self.now-7*86400
        folder=self.legacy/retention._day(born); folder.mkdir(parents=True)
        p=folder/'old.bin'; p.write_bytes(b'x'); os.utime(p,(0,0))
        retention.prune(now=self.now,store=Store()); self.assertTrue(p.exists())
        retention.prune(now=self.now+86400,store=Store()); self.assertFalse(p.exists())

    def test_low_disk_never_bypasses_age_or_claims_renames_freed_space(self):
        young=self.retire('retired.bin',self.now-86400)
        expired=self.artifact('expired.bin')
        result=retention.reclaim(10**12,now=self.now,store=Store())
        self.assertTrue(young.exists()); self.assertFalse(expired.exists())
        self.assertTrue(next(self.trash.rglob('expired.bin')).exists())
        self.assertEqual(result['freed_bytes'],0); self.assertFalse(result['enough'])
        self.assertEqual(self.calls,[])
        with patch.object(diskguard,'disk_usage',return_value=(500*1024**3,2*1024**3)), patch.object(retention,'reclaim',return_value=result):
            with self.assertRaises(diskguard.DiskLow): diskguard.require_room()

    def test_symlinks_and_escaped_paths_are_untouched(self):
        outside=self.root/'outside.bin'; outside.write_bytes(b'safe')
        (self.artifacts/'link.bin').symlink_to(outside)
        self.assertFalse(retention._move_to_trash(outside,self.now))
        retention.prune(now=self.now,store=Store())
        self.assertTrue((self.artifacts/'link.bin').is_symlink())
        folder=self.legacy/retention._day(self.now-10*86400); folder.mkdir(parents=True)
        (folder/'link.bin').symlink_to(outside)
        (self.legacy/retention._day(self.now-11*86400)).symlink_to(self.root,target_is_directory=True)
        retention.prune(now=self.now,store=Store())
        self.assertEqual(outside.read_bytes(),b'safe'); self.assertEqual(self.calls,[])

    def test_native_boundary_uses_constant_foundation_script_and_bounded_argv(self):
        from types import SimpleNamespace
        path=self.retire('quote " $ shell.bin',self.now-8*86400)
        def run(argv, **kwargs):
            self.assertEqual(argv[:4],['/usr/bin/osascript','-l','JavaScript','-e'])
            self.assertEqual(argv[4],retention._RECYCLE_SCRIPT)
            self.assertEqual(argv[5],str(path))
            self.assertNotIn(str(path),argv[4])
            self.assertEqual(kwargs['timeout'],30)
            self.assertNotIn('shell',kwargs)
            path.rename(self.recycled/path.name)
            return SimpleNamespace(returncode=0)
        with patch.object(retention.subprocess,'run',side_effect=run):
            self.assertTrue(_NATIVE_RECYCLE(path))
        failed=self.retire('failed.bin',self.now-8*86400)
        with patch.object(retention.subprocess,'run',return_value=SimpleNamespace(returncode=1)):
            self.assertFalse(_NATIVE_RECYCLE(failed)); self.assertTrue(failed.exists())

    def test_collisions_preserve_both_retired_payloads(self):
        first=self.artifact('same.bin',size=2)
        self.assertTrue(retention._move_to_trash(first,self.now))
        first=self.artifact('same.bin',size=3)
        self.assertTrue(retention._move_to_trash(first,self.now))
        self.assertEqual(sorted(p.stat().st_size for p in self.trash.rglob('same.bin*')),[2,3])

    def test_failed_transfer_does_not_remove_other_files_without_policy(self):
        stuck=self.retire('stuck.bin',self.now-8*86400)
        fresh=self.retire('fresh.bin',self.now-86400)
        self.native.side_effect=OSError('native failure')
        result=retention.prune(now=self.now,store=Store())
        self.assertTrue(stuck.exists() and fresh.exists())
        self.assertEqual(result['recycle_failures'],1)

    def test_same_day_cap_uses_retirement_order_even_with_reversed_source_mtimes(self):
        old=self.retire('z-older.bin',self.now-60)
        new=self.retire('a-newer.bin',self.now)
        os.utime(old,(self.now,self.now)); os.utime(new,(0,0))
        with patch.object(retention,'trash_cap_bytes',return_value=4096):
            retention.prune(now=self.now,store=Store())
        self.assertEqual([p.name for p in self.calls],['z-older.bin'])
        self.assertTrue(new.exists())

    def test_the_trash_policy_is_owner_adjustable_and_defaults_to_seven_days_and_twenty_gig(self):
        defaults=retention.settings()
        self.assertEqual((defaults['trash_days'],defaults['trash_cap_bytes']),(7.0,20*1024**3))
        saved=retention.save(trash_days=2,trash_cap_bytes=3*1024**3)
        self.assertEqual((saved['trash_days'],saved['trash_cap_bytes']),(2.0,3*1024**3))
        kept=retention.save(max_age_days=1)          # omitted values keep what is there
        self.assertEqual((kept['trash_days'],kept['trash_cap_bytes']),(2.0,3*1024**3))
        moved=self.retire('old.bin',self.now-3*86400)
        result=retention.prune(now=self.now,store=Store())
        self.assertEqual(result['recycled_files'],1)  # three days old, owner set two
        self.assertFalse(moved.exists())

    def test_retired_log_rotations_age_out_with_the_trash_policy(self):
        logs=self.trash/'logs'; logs.mkdir(parents=True)
        log=logs/'gateway-kh.log.2-1790000000'; log.write_bytes(b'log')
        born=self.now-8*86400; os.utime(log,(born,born))
        result=retention.prune(now=self.now,store=Store())
        self.assertFalse(log.exists())
        self.assertEqual(result['recycled_files'],1)
        self.assertEqual(len(self.calls),1)

    def test_cap_counts_allocated_blocks_instead_of_sparse_logical_size(self):
        from types import SimpleNamespace
        self.assertEqual(retention._physical(SimpleNamespace(st_blocks=0,st_size=30*1024**3)),0)
        self.assertEqual(retention._physical(SimpleNamespace(st_blocks=8,st_size=30*1024**3)),4096)
        self.assertEqual(retention._physical(SimpleNamespace(st_size=42)),42)

if __name__=='__main__': unittest.main()
