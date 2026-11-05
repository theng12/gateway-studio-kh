"""Owner-only image limits and saved intent; no unsigned upstream policy relay."""
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse

from . import image_limits
from .developer_admin import owner

router = APIRouter(dependencies=[Depends(owner)])


@router.get("/image-limits.js", include_in_schema=False)
def script():
    return FileResponse(Path(__file__).with_name("admin_image_limits.js"), media_type="text/javascript")


@router.get("/api/settings/image-limits")
def index():
    try:
        return image_limits.view()
    except ValueError as error:
        return JSONResponse(status_code=503, content={"code": "IMAGE_LIMITS_UNREADABLE", "detail": str(error)})


@router.put("/api/settings/image-limits")
def update(payload: dict):
    try:
        return image_limits.save(payload)
    except (ValueError, TypeError) as error:
        return JSONResponse(status_code=422, content={"code": "INVALID_IMAGE_LIMITS", "detail": str(error)})
