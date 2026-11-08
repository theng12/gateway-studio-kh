"""Durable owner-run app gates; unknown acceptance never becomes another submission."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
import shutil
from pathlib import Path
import threading
import tarfile
import time
from uuid import uuid4

from . import fleet
from .config import DATA_DIR
from .developer_contract import DeveloperTestError
from .developer_store import DeveloperStore
from .developer_tests import segment, closed
from .render_hash_v1 import decode_render_json
from .gate_contract import decode, encoded, validate_request, validate_status, verify_source, verify_result
from .gate_transport import (GateTransport, JSON_CEILING, SOURCE_CEILING, RESULT_CEILING,
                             LOCAL_SPOOL_CEILING, file_digest, sync_directory)

TERMINAL = {'passed', 'failed', 'cancelled', 'interrupted', 'not_run'}
_LOCK = threading.RLock()


def fail(status, code, detail):
    raise DeveloperTestError(status, code, detail)


class GateService:
    def __init__(self, root: Path | None = None, transport=None, inventory=None):
        self.root = root or DATA_DIR / 'developer-gates'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = DeveloperStore(self.root / 'attempts.sqlite3')
        self.transport = transport or GateTransport()
        self.inventory = inventory or fleet.snapshot

    def policy(self):
        path = self.root / 'policy.json'
        if path.exists():
            return decode(path.read_bytes(), 'policy', JSON_CEILING)
        # No target is enabled automatically, including when a new node appears.
        return {'format': 'studio.developer-gate.policy.v1', 'revision': 'unconfigured',
                'minimum_internal_physical_bytes': 500000000000,
                'allowed_targets': [], 'excluded_targets': []}

    def save_policy(self, raw):
        body = decode_render_json(raw)
        closed(body, ('expected_revision', 'policy'))
        policy = decode(encoded(body['policy']), 'policy', JSON_CEILING)
        with _LOCK:
            if body['expected_revision'] != self.policy()['revision']:
                fail(409, 'STALE_POLICY', 'Re-read the current gate policy before editing it.')
            if policy['revision'] == body['expected_revision']:
                fail(409, 'STALE_POLICY', 'A changed policy requires a new revision.')
            temporary = self.root / ('policy-' + uuid4().hex + '.partial')
            self._write_new(temporary, encoded(policy))
            os.replace(temporary, self.root / 'policy.json')
            sync_directory(self.root)
        return policy

    @staticmethod
    def _write_new(path, data):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        sync_directory(path.parent)

    def _target(self, controller, node):
        segment(controller), segment(node)
        snapshot = self.inventory()
        at = snapshot.get('generated_at')
        if type(at) not in (int, float) or not 0 <= time.time() - at <= fleet._MEMBERS_STALE_AFTER_S:
            fail(409, 'STALE_INVENTORY', 'Fresh registered fleet inventory is required.')
        locations = [r for r in snapshot.get('locations', []) if r.get('id') == controller]
        machines = [r for r in snapshot.get('machines', [])
                    if r.get('location_id') == controller and r.get('machine') == node]
        if len(locations) != 1 or len(machines) != 1:
            fail(404, 'UNKNOWN_TARGET', 'Select one exact registered physical Mac.')
        location, machine = locations[0], machines[0]
        if (location.get('enabled') is False or location.get('paused_at') or location.get('paused')
                or machine.get('node_paused') is not False or machine.get('paused')):
            fail(409, 'TARGET_PAUSED', 'The Mac is paused or its pause state is unknown.')
        if location.get('reachable') is not True or machine.get('reachable') is not True:
            fail(503, 'TARGET_OFFLINE', 'The exact Mac has no fresh reachable inventory.')
        return location, machine

    def capabilities(self, controller, node, *, pin=None):
        location, _ = self._target(controller, node)
        if pin is None:
            _, _, pin = self.transport.identity(controller)
        raw = self.transport.json(controller, node, 'capabilities', pin=pin)
        cap = decode(raw, 'capabilities', JSON_CEILING)
        if (node != 'local' and cap['node_id'] != node) or (
                location.get('controller_id') is None or cap['controller_id'] != location['controller_id']):
            fail(502, 'IDENTITY_MISMATCH', 'Gate capability identity does not match registered inventory.')
        observed = datetime.fromisoformat(cap['server_time'].replace('Z', '+00:00')).timestamp()
        if not 0 <= time.time() - observed <= cap['bounds']['inventory_max_age_seconds']:
            fail(503, 'STALE_CAPABILITIES', 'Gate capability authority is stale.')
        if len(raw) > min(JSON_CEILING, cap['bounds']['json_bytes']):
            fail(502, 'INVALID_HUB_RESPONSE', 'Capabilities exceed their advertised JSON bound.')
        return cap

    def view(self):
        return {'policy': self.policy(), 'runs': [self.public(r) for r in self.store.rows()],
                'reader_limits': {'json_bytes': JSON_CEILING, 'source_bytes': SOURCE_CEILING,
                                  'result_bytes': RESULT_CEILING, 'local_spool_bytes': LOCAL_SPOOL_CEILING},
                'bootstrap': 'No target enabled by default; paired runner and real packaged pins required.'}

    @staticmethod
    def public(row):
        return {'attempt': row['id'], 'controller_id': row['controller_id'], 'node_id': row['node_id'],
                'state': row['state'], 'reason': row.get('reason'), 'status': row.get('hub_status'),
                'result_verified': row.get('result_verified', False),
                'ack_confirmed': row.get('ack_confirmed', False),
                'reader_limits': row.get('reader_limits')}

    def _spool_room(self, reserve):
        used = sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file())
        pending = 0
        for row in self.store.rows():
            if row.get('result_verified'):
                continue
            directory = self.root / row['id']
            existing = sum(p.stat().st_size for p in directory.glob('*') if p.is_file())
            source_size = json.loads(row['request_bytes'])['source']['size_bytes']
            pending += max(0, source_size + row['reader_limits']['result_bytes'] - existing)
        if reserve + pending > LOCAL_SPOOL_CEILING - used:
            fail(413, 'LOCAL_SPOOL_FULL', 'Retained gate evidence fills the displayed local spool cap; nothing was evicted.')
        if reserve + pending > shutil.disk_usage(self.root).free:
            fail(507, 'LOCAL_DISK_FULL', 'Not enough local disk to retain the gate evidence safely.')

    def start(self, controller, node, raw):
        request = decode(raw, 'gateRequest', JSON_CEILING)
        canonical = encoded(request)
        campaign = segment(request['run_id'])
        with _LOCK:
            previous = self.store.rows(campaign)
            envelope = encoded({'controller_id': controller, 'node_id': node, 'request': request})
            if previous:
                self.store.create(campaign, envelope, [])  # Same campaign cannot change bytes/target.
                return self.public(previous[0])
            _, _, pin = self.transport.identity(controller)
            cap = self.capabilities(controller, node, pin=pin)
            validate_request(request, cap, self.policy(), controller, node)
            if request['source']['size_bytes'] > SOURCE_CEILING:
                fail(413, 'SOURCE_TOO_LARGE', 'Source exceeds the displayed Gateway reader bound.')
            result_bound = min(cap['bounds']['result_bytes'], request['requested_limits']['result_spool_bytes'], RESULT_CEILING)
            self._spool_room(request['source']['size_bytes'] + result_bound)
            attempt, key = uuid4().hex, uuid4().hex
            row = {'id': attempt, 'campaign': campaign, 'controller_id': controller, 'node_id': node,
                   'key': key, 'request_bytes': canonical, 'request_sha256': hashlib.sha256(canonical).hexdigest(),
                   'transport_pin': pin, 'capabilities': cap, 'submitted': False, 'state': 'not_run',
                   'reason': 'Not submitted yet.', 'hub_status': None,
                   'result_verified': False, 'source_started': False, 'ack_started': False,
                   'ack_confirmed': False, 'reader_limits': {'source_bytes': SOURCE_CEILING,
                                                            'result_bytes': result_bound}}
            if not self.store.create(campaign, envelope, [row]):
                return self.public(self.store.rows(campaign)[0])
            # The pre-wire uncertainty marker survives crashes. There is no submit retry path.
            self.store.claim_send(attempt)
            try:
                answer = self.transport.json(controller, node, 'submit', key=key, raw=canonical,
                    pin=pin, ceiling=cap['bounds']['json_bytes'])
                self.accept(attempt, answer)
            except DeveloperTestError as error:
                self.hold(attempt, error.detail)
            return self.public(self.store.get(attempt))

    def _claim(self, attempt, flag, **fields):
        with self.store.connection() as db:
            data = json.loads(db.execute('SELECT data FROM attempts WHERE id=?', (attempt,)).fetchone()[0])
            if data.get(flag):
                return False
            data.update(fields)
            data[flag] = True
            db.execute('UPDATE attempts SET data=? WHERE id=?', (encoded(data).decode('ascii'), attempt))
        return True

    def hold(self, attempt, detail):
        return self.store.update(attempt, state='recovery_hold',
            reason=detail + ' Original controller, node and key retained; no automatic resubmission.')

    def accept(self, attempt, raw):
        row = self.store.get(attempt)
        cap = row['capabilities']
        status = validate_status(decode(raw, 'status', min(JSON_CEILING, cap['bounds']['json_bytes'])),
                                 json.loads(row['request_bytes']), cap, row.get('acceptance'))
        acceptance = status['acceptance']
        if (acceptance['idempotency_key'] != row['key'] or acceptance['request_sha256'] != row['request_sha256']
                or acceptance['controller_id'] != cap['controller_id'] or acceptance['node_id'] != cap['node_id']
                or (row.get('acceptance') is not None and acceptance != row['acceptance'])):
            fail(502, 'IDENTITY_MISMATCH', 'Status is not the exact accepted gate attempt.')
        descriptor = status['result']
        prior_status = row.get('hub_status')
        if prior_status and prior_status['state'] in TERMINAL and status['state'] != prior_status['state']:
            fail(502, 'IMMUTABLE_RESULT', 'The original terminal outcome changed.')
        previous = row.get('result_descriptor')
        if previous is not None and descriptor != previous:
            fail(502, 'IMMUTABLE_RESULT', 'The original result descriptor changed.')
        if status['state'] in TERMINAL and (not status['resource_release_confirmed'] or descriptor is None):
            fail(502, 'INCOMPLETE_RESULT', 'Terminal gate status lacks process-release proof or result.')
        if descriptor is not None and status['state'] not in TERMINAL:
            fail(502, 'INCOMPLETE_RESULT', 'A nonterminal status cannot publish an immutable result.')
        acknowledged = status['result_acknowledged']
        if (acknowledged and (status['acknowledged_result'] != descriptor or descriptor is None
                             or status['acknowledged_at'] is None)) or (
                not acknowledged and (status['acknowledged_result'] is not None or status['acknowledged_at'] is not None)):
            fail(502, 'INVALID_ACK', 'Status has inconsistent durable result acknowledgement.')
        if acknowledged and (not row.get('ack_started') or not row.get('result_verified')):
            fail(502, 'INVALID_ACK', 'Hub acknowledged a result Gateway has not verified and acknowledged.')
        if status['workspace_removed'] and (status['cleanup_after'] is None
                                           or status['cleanup_after']['workspace_bytes'] != 0):
            fail(502, 'INVALID_CLEANUP', 'Workspace removal has no measured zero-workspace readback.')
        if row.get('ack_confirmed') and not acknowledged:
            fail(502, 'INVALID_ACK', 'The durable acknowledgement disappeared.')
        collecting = status['state'] in TERMINAL and not row.get('result_verified')
        return self.store.update(attempt, acceptance=acceptance, hub_status=status,
            result_descriptor=descriptor, ack_confirmed=acknowledged,
            state='collecting' if collecting else status['state'],
            reason='Collecting and verifying the immutable result.' if collecting else
                   (None if status['reason'] is None else status['reason']['detail']))

    def new_source(self, attempt):
        with _LOCK:
            row = self.store.get(segment(attempt))
            if row.get('acceptance') is None or row.get('source_started') or row.get('source_staging') or row.get('hub_status', {}).get('state') != 'waiting_source':
                fail(409, 'SOURCE_UNRESOLVED', 'Reconcile original acceptance; source was already attempted or is not requested.')
            request = json.loads(row['request_bytes'])
            size = request['source']['size_bytes']
            self._spool_room(0)
            if not self._claim(attempt, 'source_staging'):
                fail(409, 'SOURCE_UNRESOLVED', 'Source staging already belongs to the original transfer.')
            directory = self.root / attempt
            directory.mkdir(mode=0o700, exist_ok=True)
            return directory / ('source-' + uuid4().hex + '.tar'), size

    def upload(self, attempt, path):
        with _LOCK:
            row = self.store.get(segment(attempt))
            if path.parent != self.root / attempt or path.is_symlink():
                fail(400, 'INVALID_SOURCE', 'Source must be staged in the owned attempt workspace.')
            request, cap = json.loads(row['request_bytes']), row['capabilities']
            verify_source(path, request, cap)
            # Recheck owner policy/pause before upload can trigger installation or execution.
            self._target(row['controller_id'], row['node_id'])
            validate_request(request, self.capabilities(row['controller_id'], row['node_id'], pin=row['transport_pin']),
                             self.policy(), row['controller_id'], row['node_id'])
            if row.get('source_started') or row.get('acceptance') is None:
                fail(409, 'SOURCE_UNRESOLVED', 'Original source was already attempted; reconcile status only.')
            if not self._claim(attempt, 'source_started', source_file=path.name):
                fail(409, 'SOURCE_UNRESOLVED', 'Original source transfer already started; reconcile only.')
            try:
                raw = self.transport.json(row['controller_id'], row['node_id'], 'source', key=row['key'],
                    source=path, pin=row['transport_pin'], ceiling=cap['bounds']['json_bytes'])
                self.accept(attempt, raw)
            except DeveloperTestError as error:
                self.hold(attempt, error.detail)
            return self.public(self.store.get(attempt))

    def refresh(self, attempt):
        with _LOCK:
            row = self.store.get(segment(attempt))
            try:
                cap = row['capabilities']
                raw = self.transport.json(row['controller_id'], row['node_id'], 'status', key=row['key'],
                    pin=row['transport_pin'], ceiling=cap['bounds']['json_bytes'])
                row = self.accept(attempt, raw)
                if row.get('result_descriptor') is not None and not row.get('result_verified'):
                    descriptor = row['result_descriptor']
                    self._spool_room(descriptor['size_bytes'])
                    directory = self.root / attempt
                    directory.mkdir(mode=0o700, exist_ok=True)
                    partial = directory / ('result-' + uuid4().hex + '.partial')
                    self.transport.download(row['controller_id'], row['node_id'], row['key'], descriptor,
                        partial, pin=row['transport_pin'], ceiling=row['reader_limits']['result_bytes'])
                    manifest = verify_result(partial, json.loads(row['request_bytes']), cap, row['acceptance'])
                    if manifest['outcome'] != row['hub_status']['state']:
                        fail(502, 'RESULT_OUTCOME_MISMATCH', 'Archived outcome does not match the terminal status.')
                    final = partial.with_suffix('.tar')
                    os.replace(partial, final)
                    sync_directory(directory)
                    row = self.store.update(attempt, receipt=encoded(manifest), result_file=final.name,
                                            result_verified=True)
                if row.get('result_verified') and not row.get('ack_started'):
                    self._ack(row)
            except DeveloperTestError as error:
                self.hold(attempt, error.detail)
            except (OSError, ValueError, tarfile.TarError):
                self.hold(attempt, 'Result storage or archive verification failed; retained evidence needs reconciliation.')
            return self.public(self.store.get(attempt))

    def _ack(self, row):
        path = self.result_path(row['id'])
        digest, size = file_digest(path)
        descriptor = row['result_descriptor']
        if digest != descriptor['sha256'] or size != descriptor['size_bytes']:
            fail(502, 'INVALID_RESULT', 'Durably retained result bytes changed; no ACK sent.')
        ack = {'format': 'studio.developer-gate.ack.v1', 'request_id': uuid4().hex,
               'acceptance': row['acceptance'], 'result': descriptor}
        if not self._claim(row['id'], 'ack_started', ack_bytes=encoded(ack).decode('ascii')):
            return
        raw = self.transport.json(row['controller_id'], row['node_id'], 'ack', key=row['key'],
            raw=encoded(ack), pin=row['transport_pin'], ceiling=row['capabilities']['bounds']['json_bytes'])
        result = self.accept(row['id'], raw)
        if not result['ack_confirmed']:
            fail(502, 'ACK_UNRESOLVED', 'Exact durable hash ACK is not confirmed; reconcile original key.')

    def cancel(self, attempt):
        with _LOCK:
            row = self.store.get(segment(attempt))
            raw = encoded({'format': 'studio.developer-gate.cancel.v1', 'request_id': uuid4().hex})
            self.store.update(attempt, reason='Cancellation requested; resource release is not confirmed.')
            try:
                answer = self.transport.json(row['controller_id'], row['node_id'], 'cancel', key=row['key'],
                    raw=raw, pin=row['transport_pin'], ceiling=row['capabilities']['bounds']['json_bytes'])
                self.accept(attempt, answer)
            except DeveloperTestError as error:
                self.hold(attempt, error.detail)
            return self.public(self.store.get(attempt))

    def result_path(self, attempt):
        row = self.store.get(segment(attempt))
        if not row.get('result_verified'):
            fail(409, 'RESULT_UNVERIFIED', 'The complete result archive has not been durably verified.')
        name = row['result_file']
        if Path(name).name != name:
            fail(502, 'INVALID_RESULT', 'Retained result path is invalid.')
        path = self.root / attempt / name
        if path.is_symlink() or not path.is_file():
            fail(502, 'INVALID_RESULT', 'Retained result archive is unavailable.')
        return path
