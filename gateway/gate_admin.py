"""Owner-only app-gate consumer; source/result files stay in generated owned paths."""
import os
import tarfile

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .developer_admin import owner, call, error_response
from .developer_contract import DeveloperTestError
from .developer_tests import closed
from .gate_contract import encoded
from .gate_service import GateService
from .gate_transport import JSON_CEILING, SOURCE_CEILING, file_digest, sync_directory
from .render_hash_v1 import decode_render_json

router = APIRouter(dependencies=[Depends(owner)])
_service = None


def service():
    global _service
    if _service is None:
        _service = GateService()
    return _service


async def json_body(request):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > JSON_CEILING:
            raise DeveloperTestError(413, 'PAYLOAD_TOO_LARGE', 'Gate JSON exceeds the displayed reader bound.')
        data.extend(chunk)
    return bytes(data)


@router.get('/api/developer-gates')
def index():
    return call(service().view)


@router.post('/api/developer-gates/policy')
async def policy(request: Request):
    try:
        raw = await json_body(request)
    except DeveloperTestError as error:
        return error_response(error)
    return await run_in_threadpool(call, service().save_policy, raw)


@router.get('/api/developer-gates/targets/{controller_id}/{node_id}/capabilities')
def capabilities(controller_id: str, node_id: str):
    return call(service().capabilities, controller_id, node_id)


@router.post('/api/developer-gates/runs')
async def start(request: Request):
    try:
        payload = decode_render_json(await json_body(request))
        closed(payload, ('controller_id', 'node_id', 'request'))
        raw = encoded(payload['request'])
    except (DeveloperTestError, ValueError) as error:
        return error_response(error)
    return await run_in_threadpool(call, service().start, payload['controller_id'], payload['node_id'], raw)


@router.put('/api/developer-gates/runs/{attempt}/source')
async def source(attempt: str, request: Request):
    staged = False
    try:
        path, expected = service().new_source(attempt)
        staged = True
        if expected > SOURCE_CEILING:
            raise DeveloperTestError(413, 'SOURCE_TOO_LARGE', 'Source exceeds the displayed reader bound.')
        size = 0
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            async for chunk in request.stream():
                if size + len(chunk) > expected:
                    raise DeveloperTestError(413, 'SOURCE_TOO_LARGE', 'Source exceeds its original byte count.')
                handle.write(chunk)
                size += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if size != expected:
            raise DeveloperTestError(400, 'INCOMPLETE_SOURCE', 'Source does not match its original byte count.')
        sync_directory(path.parent)
        return await run_in_threadpool(service().upload, attempt, path)
    except (DeveloperTestError, OSError, ClientDisconnect, tarfile.TarError) as error:
        if isinstance(error, (OSError, ClientDisconnect, tarfile.TarError)):
            error = DeveloperTestError(503, 'SOURCE_TRANSFER_INCOMPLETE', 'Owned source transfer failed; partial evidence retained.')
        if staged:
            service().hold(attempt, error.detail)
        return error_response(error)


@router.post('/api/developer-gates/runs/{attempt}/refresh')
def refresh(attempt: str):
    return call(service().refresh, attempt)


@router.post('/api/developer-gates/runs/{attempt}/cancel')
def cancel(attempt: str):
    return call(service().cancel, attempt)


@router.get('/api/developer-gates/runs/{attempt}/result')
def result(attempt: str):
    try:
        row = service().store.get(attempt)
        path = service().result_path(attempt)
        digest, size = file_digest(path)
        if (digest, size) != (row['result_descriptor']['sha256'], row['result_descriptor']['size_bytes']):
            raise DeveloperTestError(502, 'INVALID_RESULT', 'Retained archive bytes changed; download refused.')
        return FileResponse(path, media_type='application/x-tar', filename='gate-result.tar')
    except (DeveloperTestError, OSError) as error:
        if isinstance(error, OSError):
            error = DeveloperTestError(503, 'RESULT_STORAGE_FAILED', 'Verified result storage is unavailable.')
        return error_response(error)
