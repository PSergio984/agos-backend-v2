from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, require_auth
from app.core.database import get_db
from app.schemas import CameraDeviceCreate, CameraDeviceItem, CameraDeviceUpdate
from app.services import camera_device_service

router = APIRouter(prefix="/camera-devices", tags=["camera-devices"])


@router.get("", response_model=list[CameraDeviceItem], dependencies=[Depends(require_auth)])
async def list_camera_devices(
    location_id: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[CameraDeviceItem]:
    return await camera_device_service.list_devices(db=db, location_id=location_id)


@router.post("", response_model=CameraDeviceItem, status_code=201)
async def register_camera_device(
    payload: CameraDeviceCreate,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_auth),
) -> CameraDeviceItem:
    return await camera_device_service.register_device(
        db=db, payload=payload, current_user=current_user
    )


@router.put("/{device_id}", response_model=CameraDeviceItem)
async def update_camera_device(
    device_id: int,
    payload: CameraDeviceUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_auth),
) -> CameraDeviceItem:
    """Update a registered camera device. Deactivate = PUT {"is_active": false}."""
    return await camera_device_service.update_device(
        db=db, device_id=device_id, payload=payload, current_user=current_user
    )
