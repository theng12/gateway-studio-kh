"""Fixed registered-controller app-gate operations; no arbitrary relay or job replay."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path

import httpx

from . import fleet
from .developer_contract import DeveloperTestError
from .developer_tests import HubDeveloperClient, segment

# ponytail: these are Gateway reader ceilings, not node/gate limits; version/expose larger
# readers when a packaged gate needs them. Advertised node bounds may only lower them.
JSON_CEILING = 4 * 1024 * 1024
SOURCE_CEILING = 32 * 1024 * 1024
RESULT_CEILING = 32 * 1024 * 1024
LOCAL_SPOOL_CEILING = 256 * 1024 * 1024


def file_digest(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class GateTransport(HubDeveloperClient):
    OPERATIONS = {'capabilities': ('GET', '/capabilities'), 'submit': ('POST', ''),
                  'status': ('GET', '/by-key/{key}'), 'cancel': ('POST', '/by-key/{key}/cancel'),
                  'source': ('PUT', '/by-key/{key}/source'),
                  'result': ('GET', '/by-key/{key}/result'), 'ack': ('POST', '/by-key/{key}/ack')}

    def identity(self, controller):
        segment(controller)
        # The shared client's local path reads an operator token, never a Fleet credential.
        if controller == fleet.LOCAL_ID:
            raise DeveloperTestError(503, 'CREDENTIAL_UNAVAILABLE',
                                     'App gates require a registered Fleet credential; local operator credentials are refused.')
        return super().identity(controller)

    @contextmanager
    def exchange(self, controller, node, operation, *, key=None, pin=None, content=None,
                 length=None):
        segment(node)
        if operation not in self.OPERATIONS:
            raise DeveloperTestError(400, 'INVALID_OPERATION', 'Unknown packaged gate operation.')
        method, suffix = self.OPERATIONS[operation]
        if '{key}' in suffix:
            suffix = suffix.format(key=segment(key))
        base, token, current = self.identity(controller)
        if pin is not None and current != pin:
            raise DeveloperTestError(409, 'DESTINATION_CHANGED', 'Original gate destination or credential changed.')
        headers = {'X-Hub-Token': token}
        if operation == 'submit':
            headers['Idempotency-Key'] = segment(key)
        if content is not None:
            headers['Content-Type'] = 'application/x-tar' if operation == 'source' else 'application/json'
        if length is not None:
            headers['Content-Length'] = str(length)
        client = self.client or httpx.Client(timeout=httpx.Timeout(20, connect=6), follow_redirects=False)
        try:
            path = f'/api/hub/fleet/nodes/{node}/developer-gates{suffix}'
            with client.stream(method, base + path, headers=headers, content=content,
                               follow_redirects=False) as response:
                if response.status_code not in (200, 202):
                    # Do not reflect upstream private text or infer known rejection after send.
                    status = response.status_code
                    raise DeveloperTestError(status if status in (400, 401, 403, 404, 409, 413, 503) else 502,
                                             'HUB_REFUSED', 'The original Hub refused this gate operation.')
                yield response, token
        except httpx.HTTPError:
            raise DeveloperTestError(502, 'TRANSPORT_UNCERTAIN',
                                     'Original Hub acceptance is unresolved; reconcile the same key.') from None
        finally:
            if self.client is None:
                client.close()

    def json(self, controller, node, operation, *, key=None, raw=None, pin=None,
             ceiling=JSON_CEILING, source: Path | None = None):
        ceiling = min(JSON_CEILING, ceiling)
        if raw is not None and len(raw) > ceiling:
            raise DeveloperTestError(413, 'PAYLOAD_TOO_LARGE', 'Gate JSON exceeds its displayed reader bound.')
        handle = None
        try:
            if source is not None:
                handle = source.open('rb')
                content = iter(lambda: handle.read(65536), b'')
                length = source.stat().st_size
            else:
                content, length = raw, None if raw is None else len(raw)
            result = bytearray()
            with self.exchange(controller, node, operation, key=key, pin=pin,
                               content=content, length=length) as (response, token):
                for chunk in response.iter_bytes():
                    if len(result) + len(chunk) > ceiling:
                        raise DeveloperTestError(502, 'INVALID_HUB_RESPONSE', 'Gate JSON exceeds its reader bound.')
                    result.extend(chunk)
                if token.encode() in result:
                    raise DeveloperTestError(502, 'INVALID_HUB_RESPONSE', 'Gate response contained credential material.')
            return bytes(result)
        finally:
            if handle is not None:
                handle.close()

    def download(self, controller, node, key, descriptor, destination: Path, *, pin, ceiling):
        expected = descriptor['size_bytes']
        if expected > min(RESULT_CEILING, ceiling):
            raise DeveloperTestError(413, 'RESULT_TOO_LARGE', 'Result exceeds its displayed reader bound.')
        digest, size, carry = hashlib.sha256(), 0, b''
        with self.exchange(controller, node, 'result', key=key, pin=pin) as (response, token):
            secret = token.encode()
            # Exclusive private partial file. Failed partials are retained and count against spool.
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                for chunk in response.iter_bytes():
                    if size + len(chunk) > expected:
                        raise DeveloperTestError(502, 'INVALID_RESULT', 'Result exceeds the pinned byte count.')
                    if secret in carry + chunk:
                        raise DeveloperTestError(502, 'INVALID_RESULT', 'Result contained credential material.')
                    carry = (carry + chunk)[-max(1, len(secret) - 1):]
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        if size != expected or digest.hexdigest() != descriptor['sha256']:
            raise DeveloperTestError(502, 'INVALID_RESULT', 'Result bytes do not match their pinned identity.')
        sync_directory(destination.parent)
