import base64
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from app.schemas import CameraFrameSnapshot, CameraStatus
from app.services.camera_status_service import camera_status_service
from app.services.camera_device_service import camera_device_service
from app.services.ml_service import ml_service
from app.core.config import settings
from app.core.rate_limiter import limiter
from app.core.ws_manager import ws_manager
from app.api.v1.dependencies import require_iot_api_key
from app.core.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stream", tags=["stream"])


@router.get("/status", response_model=CameraStatus)
async def get_camera_status(location_id: int = Query(...)):
    """Return whether the camera for a location is actively sending frames."""
    return camera_status_service.get_status(location_id)


@router.get("/latest-frame", response_model=CameraFrameSnapshot)
async def get_latest_camera_frame(location_id: int = Query(...)):
    """Return the newest frame received for a monitored public location."""
    frame = camera_status_service.get_latest_frame(location_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="No camera frame available")
    return frame


@router.post("/upload-image")
@limiter.limit("35/minute")
async def upload_camera_image(
    request: Request,
    camera_device_id: int,
    location_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_iot_api_key),
):
    """Receive an image from the camera, broadcast it to WebSocket clients, and run ML inference."""
    # Camera↔site binding: the device's registered site must match the
    # claimed location_id; the registration is authoritative downstream.
    device = await camera_device_service.get_for_ingest(
        db=db, camera_device_id=camera_device_id, location_id=location_id
    )
    if device is None:
        raise HTTPException(
            status_code=400,
            detail="Camera device is not registered for this location_id",
        )
    location_id = device.location_id

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    image_bytes = await file.read(settings.MAX_IMAGE_UPLOAD_BYTES + 1)
    if len(image_bytes) > settings.MAX_IMAGE_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail="Image exceeds maximum allowed size"
        )

    encoded = base64.b64encode(image_bytes).decode("utf-8")
    camera_status_service.record_frame(location_id, encoded_image=encoded)
    await ws_manager.broadcast_to_location(
        {
            "type": "camera_update",
            "data": {
                "image": encoded,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        },
        location_id=location_id,
    )

    await ml_service.process_frame_bytes(
        image_bytes=image_bytes,
        camera_device_id=camera_device_id,
        location_id=location_id,
    )

    return {"status": "ok"}
