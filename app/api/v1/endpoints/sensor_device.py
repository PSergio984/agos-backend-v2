from fastapi import APIRouter, Depends
from app.services import sensor_device_service
from sqlalchemy.ext.asyncio import AsyncSession
from app.schemas import (
    SensorDeviceCreate,
    SensorDeviceItem,
    SensorDeviceStatusResponse,
    SensorDeviceUpdate,
)
from app.core.database import get_db
from app.api.v1.dependencies import CurrentUser, require_auth
from app.models import SensorConfig

router = APIRouter(prefix="/sensor-devices", tags=["sensor-devices"])


# -- Admin device registry (multi-site) ---------------------------------------

@router.get("", response_model=list[SensorDeviceItem], dependencies=[Depends(require_auth)])
async def list_sensor_devices(
    location_id: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[SensorDeviceItem]:
    return await sensor_device_service.list_devices(db=db, location_id=location_id)


@router.post("", response_model=SensorDeviceItem, status_code=201)
async def register_sensor_device(
    payload: SensorDeviceCreate,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_auth),
) -> SensorDeviceItem:
    return await sensor_device_service.register_device(
        db=db, payload=payload, current_user=current_user
    )


@router.put("/{device_id}", response_model=SensorDeviceItem)
async def update_sensor_device(
    device_id: int,
    payload: SensorDeviceUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_auth),
) -> SensorDeviceItem:
    """Update a registered sensor device. Deactivate = PUT {"is_active": false}."""
    return await sensor_device_service.update_device(
        db=db, device_id=device_id, payload=payload, current_user=current_user
    )


@router.get("/{id}/status", response_model=SensorDeviceStatusResponse, dependencies=[Depends(require_auth)])
async def get_sensor_device(id: int = 1, db: AsyncSession = Depends(get_db)) -> SensorDeviceStatusResponse:
    return await sensor_device_service.get_device_status(db=db, sensor_device_id=id)


@router.get("/{id}/config", response_model=SensorConfig, dependencies=[Depends(require_auth)])
async def get_sensor_device_config(id: int = 1, db: AsyncSession = Depends(get_db)) -> SensorConfig:
    return await sensor_device_service.get_device_config(db=db, sensor_device_id=id)


@router.put("/{id}/config", response_model=SensorConfig, dependencies=[Depends(require_auth)])
async def update_sensor_device_config(config: SensorConfig, id: int = 1, db: AsyncSession = Depends(get_db)) -> SensorConfig:
    return await sensor_device_service.update_device_config(db=db, sensor_device_id=id, config=config)


# used in responders app
@router.get("/{location_id}/config/by-location", response_model=SensorConfig)
async def get_sensor_device_config_by_location(location_id: int = 1, db: AsyncSession = Depends(get_db)) -> SensorConfig:
    return await sensor_device_service.get_device_config_by_location(db=db, location_id=location_id)