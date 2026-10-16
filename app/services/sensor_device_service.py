from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import SensorConfig
from app.schemas import (
    AdminAuditLogCreate,
    SensorDeviceCreate,
    SensorDeviceItem,
    SensorDeviceResponse,
    SensorDeviceStatusResponse,
    SensorDeviceUpdate,
)
from app.crud import admin_audit_log_crud, location_crud, sensor_device_crud
from app.crud import sensor_reading_crud
from datetime import datetime, timedelta
from fastapi import HTTPException, status
from app.api.v1.dependencies import CurrentUser
from app.core.config import settings
from app.services import sensor_reading_service
from app.services.cache_service import cache_service


class SensorDeviceService:
    
    async def get_device_status(self, db: AsyncSession, sensor_device_id: int) -> SensorDeviceStatusResponse:
        
        sensor_device: SensorDeviceResponse = await sensor_device_crud.get(db=db, id=sensor_device_id)

        if not sensor_device:
            raise HTTPException(status_code=404, detail="Sensor device not found")

        latest_reading = await sensor_reading_crud.get_latest_reading(db=db, sensor_device_id=sensor_device_id)

        # Default values if no readings yet
        if not latest_reading:
            return SensorDeviceStatusResponse(
                device_name=sensor_device.device_name,
                location_name=sensor_device.location_name,
                connection="Offline",
                last_updated=None,
                signal=None
            )

        # Calculate connection status
        now = datetime.now(latest_reading.timestamp.tzinfo)
        time_since = now - latest_reading.timestamp

        if time_since <= timedelta(minutes=settings.SENSOR_GRACE_PERIOD_MINUTES):
            connection = "Online"
        elif time_since <= timedelta(minutes=settings.SENSOR_WARNING_PERIOD_MINUTES):
            connection = "Warning"
        else:
            connection = "Offline"

        # Response if the last reading is way off the grace period
        if connection == "Offline":
            return SensorDeviceStatusResponse(
            device_name=sensor_device.device_name,
            location_name=sensor_device.location_name,
            connection=connection,
            last_updated=latest_reading.timestamp.isoformat(),
            signal=None
        )

        # Normal response with signal quality
        return SensorDeviceStatusResponse(
            device_name=sensor_device.device_name,
            location_name=sensor_device.location_name,
            connection=connection,
            last_updated=latest_reading.timestamp.isoformat(),
            signal=sensor_reading_service.get_signal_quality(latest_reading.signal_strength)
        )


    async def get_device_config(self, db: AsyncSession, sensor_device_id: int) -> SensorConfig:

        sensor_config = await sensor_device_crud.get_device_config(db=db, sensor_device_id=sensor_device_id)
        if not sensor_config:
            raise HTTPException(status_code=404, detail="Sensor device config not found")
        return sensor_config


    async def get_device_config_by_location(self, db: AsyncSession, location_id: int) -> SensorConfig:

        sensor_config = await sensor_device_crud.get_device_config_by_location(db=db, location_id=location_id)
        if not sensor_config:
            raise HTTPException(status_code=404, detail="Sensor device config not found for this location")
        return sensor_config


    async def update_device_config(self, db: AsyncSession, sensor_device_id: int, config: SensorConfig) -> SensorConfig:

        updated_config = await sensor_device_crud.update_config(db=db, sensor_device_id=sensor_device_id, config=config)
        if not updated_config:
            raise HTTPException(status_code=404, detail="Sensor device not found")

        await cache_service.update_sensor_config_cache(db=db, sensor_device_id=sensor_device_id)
        return updated_config


    # -- Admin device registry (multi-site) -----------------------------------

    async def list_devices(
        self, db: AsyncSession, location_id: int | None = None
    ) -> list[SensorDeviceItem]:
        devices = await sensor_device_crud.get_all(db=db, location_id=location_id)
        return [SensorDeviceItem.model_validate(d) for d in devices]

    async def register_device(
        self, db: AsyncSession, payload: SensorDeviceCreate, current_user: CurrentUser
    ) -> SensorDeviceItem:
        location = await self._require_active_site(db=db, location_id=payload.location_id)
        existing = await sensor_device_crud.get_by_name_and_location(
            db=db, device_name=payload.device_name, location_id=payload.location_id
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A sensor device with that name already exists on this site.",
            )
        try:
            created = await sensor_device_crud.create_and_return_no_commit(db=db, obj_in=payload)
            await admin_audit_log_crud.create_only_no_commit(
                db=db,
                obj_in=AdminAuditLogCreate(
                    admin_user_id=current_user.id,
                    action=(
                        f"Registered sensor device '{payload.device_name}' "
                        f"on site '{location.name}' (id={location.id})"
                    ),
                ),
            )
            await db.commit()
            await db.refresh(created)
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid sensor device data.",
            )
        # A device registered at runtime must land in the device-id caches,
        # otherwise ingest for it misses until restart.
        cache_service.invalidate_device_caches()
        return SensorDeviceItem.model_validate(created)

    async def update_device(
        self,
        db: AsyncSession,
        device_id: int,
        payload: SensorDeviceUpdate,
        current_user: CurrentUser,
    ) -> SensorDeviceItem:
        device = await sensor_device_crud.get_entity(db=db, id=device_id)
        if device is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Sensor device not found.",
            )

        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            return SensorDeviceItem.model_validate(device)

        new_location_id = (
            payload.location_id if payload.location_id is not None else device.location_id
        )
        new_name = (
            payload.device_name if payload.device_name is not None else device.device_name
        )

        if payload.location_id is not None and payload.location_id != device.location_id:
            await self._require_active_site(db=db, location_id=payload.location_id)
        clash = await sensor_device_crud.get_by_name_and_location(
            db=db, device_name=new_name, location_id=new_location_id
        )
        if clash is not None and clash.id != device.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A sensor device with that name already exists on this site.",
            )

        was_active = device.is_active
        try:
            updated = await sensor_device_crud.update_no_commit(db=db, db_obj=device, obj_in=payload)
            if "is_active" in changes and updated.is_active != was_active:
                verb = "Deactivated" if not updated.is_active else "Reactivated"
            else:
                verb = "Updated"
            await admin_audit_log_crud.create_only_no_commit(
                db=db,
                obj_in=AdminAuditLogCreate(
                    admin_user_id=current_user.id,
                    action=f"{verb} sensor device '{updated.device_name}' (id={updated.id})",
                ),
            )
            await db.commit()
            await db.refresh(updated)
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not update sensor device.",
            )
        if "sensor_config" in changes:
            await cache_service.update_sensor_config_cache(
                db=db, sensor_device_id=updated.id
            )
        if "location_id" in changes or "is_active" in changes:
            cache_service.invalidate_device_caches()
        return SensorDeviceItem.model_validate(updated)

    async def _require_active_site(self, db: AsyncSession, location_id: int):
        location = await location_crud.get(db=db, id=location_id)
        if location is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Site not found.",
            )
        if not location.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Site is inactive.",
            )
        return location


sensor_device_service = SensorDeviceService()