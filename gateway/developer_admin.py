"""Owner-only Developer Mode console routes. No generation API or arbitrary relay path."""
from pathlib import Path

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .developer_contract import DeveloperTestError
from .developer_tests import DeveloperTests, WIRE_CEILING, closed
from .render_hash_v1 import decode_render_json

def owner(request: Request, authorization: str | None = Header(default=None)):
    from .admin import require_owner
    return require_owner(request, authorization)


def requested_by(request):
    from .admin import requested_by as identity
    return identity(request)


router = APIRouter(dependencies=[Depends(owner)])
_service = None


def service():
    global _service
    if _service is None:
        _service = DeveloperTests()
    return _service


def error_response(error):
    if isinstance(error, DeveloperTestError):
        return JSONResponse(status_code=error.status,
                            content={"ok": False, "code": error.code, "detail": error.detail})
    return JSONResponse(status_code=400,
                        content={"ok": False, "code": "INVALID_REQUEST", "detail": "The Developer Mode request or evidence is invalid."})


def call(function, *args):
    try:
        return function(*args)
    except (DeveloperTestError, ValueError, TypeError, KeyError) as error:
        return error_response(error)


async def body(request):
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > WIRE_CEILING:
            raise DeveloperTestError(413, "PAYLOAD_TOO_LARGE", "The request exceeds the displayed reader byte limit.")
        raw.extend(chunk)
    return bytes(raw)


async def action(request, operation, *args, parsed=False):
    try:
        raw = await body(request)
        payload = decode_render_json(raw) if parsed else raw
    except (ValueError, DeveloperTestError) as error:
        return error_response(error)
    return await run_in_threadpool(call, operation, *args, payload)


@router.get("/developer-tests.js", include_in_schema=False)
def developer_script():
    return FileResponse(Path(__file__).with_name("admin_developer_tests.js"), media_type="text/javascript")


@router.get("/api/developer-tests")
def index():
    return call(service().view)


@router.get("/api/developer-tests/targets/{controller_id}/{node_id}/capabilities")
def capabilities(controller_id: str, node_id: str):
    def read():
        return {"capabilities": service().capabilities(controller_id, node_id),
                "reason": None, "reader_byte_ceiling": WIRE_CEILING}
    result = call(read)
    if isinstance(result, JSONResponse):
        # Keep the exact refusal code/status rather than reporting unsupported as an empty model list.
        return result
    return result


@router.post("/api/developer-tests/targets/{controller_id}/{node_id}/setting")
async def setting(controller_id: str, node_id: str, request: Request):
    return await action(request, service().setting, controller_id, node_id)


@router.post("/api/developer-tests/runs")
async def run(request: Request):
    return await action(request, service().run)


@router.post("/api/developer-tests/runs/{attempt}/refresh")
def refresh(attempt: str):
    return call(service().refresh, attempt)


@router.post("/api/developer-tests/runs/{attempt}/cancel")
async def cancel(attempt: str, request: Request):
    return await action(request, service().cancel, attempt)


@router.get("/api/developer-tests/runs/{attempt}/log")
def log(attempt: str):
    return call(service().log, attempt)


@router.post("/api/developer-tests/runs/{attempt}/proposal")
def proposal(attempt: str):
    return call(service().proposal, attempt)


@router.post("/api/developer-tests/runs/{attempt}/approve")
async def approve(attempt: str, request: Request):
    actor = requested_by(request)["label"]
    def apply(payload):
        return service().approve(attempt, payload, actor)
    return await action(request, apply, parsed=True)


@router.get("/api/developer-tests/evidence")
def evidence():
    return call(service().store.evidence)


@router.get("/api/developer-tests/measurements")
def measurements():
    from .qualification import developer_measurements
    return call(developer_measurements, service().store)


@router.post("/api/developer-tests/approvals/{action_id}/rollback")
async def rollback(action_id: str, request: Request):
    actor = requested_by(request)["label"]
    def restore(payload):
        closed(payload, ("expected_store_revision",))
        return service().rollback(action_id, payload["expected_store_revision"], actor)
    return await action(request, restore, parsed=True)
