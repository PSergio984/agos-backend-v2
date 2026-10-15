"""Camera device registry CRUD + camera↔site binding verification (T6).

"Site" is the domain term; the code/schema name is Location / location_id.
The ingest path (WS /ws/rpi, POST /stream/upload-image) treats the device's
own registration as authoritative: a claimed location_id that doesn't match
the registered site is rejected.
"""

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser
from app.crud import admin_audit_log_crud, camera_device_crud, location_crud
from app.models import CameraDevice
from app.schemas import (
    AdminAuditLogCreate,
    CameraDeviceCreate,
    CameraDeviceItem,
    CameraDeviceUpdate,
)


class CameraDeviceService:

    async def list_devices(
        self, db: AsyncSession, location_id: int | None = None
    ) -> list[CameraDeviceItem]:
        devices = await camera_device_crud.get_all(db=db, location_id=location_id)
        return [CameraDeviceItem.model_validate(d) for d in devices]

    async def register_device(
        self, db: AsyncSession, payload: CameraDeviceCreate, current_user: CurrentUser
    ) -> CameraDeviceItem:
        location = await self._require_active_site(db=db, location_id=payload.location_id)
        existing = await camera_device_crud.get_by_name_and_location(
            db=db, device_name=payload.device_name, location_id=payload.location_id
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A camera device with that name already exists on this site.",
            )
        try:
            created = await camera_device_crud.create_and_return(db=db, obj_in=payload)
            await admin_audit_log_crud.create_only_no_commit(
                db=db,
                obj_in=AdminAuditLogCreate(
                    admin_user_id=current_user.id,
                    action=(
                        f"Registered camera device '{payload.device_name}' "
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
                detail="Invalid camera device data.",
            )
        return CameraDeviceItem.model_validate(created)

    async def update_device(
        self,
        db: AsyncSession,
        device_id: int,
        payload: CameraDeviceUpdate,
        current_user: CurrentUser,
    ) -> CameraDeviceItem:
        device = await camera_device_crud.get(db=db, id=device_id)
        if device is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Camera device not found.",
            )

        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            return CameraDeviceItem.model_validate(device)

        new_location_id = (
            payload.location_id if payload.location_id is not None else device.location_id
        )
        new_name = (
            payload.device_name if payload.device_name is not None else device.device_name
        )

        if payload.location_id is not None and payload.location_id != device.location_id:
            await self._require_active_site(db=db, location_id=payload.location_id)
        clash = await camera_device_crud.get_by_name_and_location(
            db=db, device_name=new_name, location_id=new_location_id
        )
        if clash is not None and clash.id != device.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A camera device with that name already exists on this site.",
            )

        was_active = device.is_active
        try:
            updated = await camera_device_crud.update(db=db, db_obj=device, obj_in=payload)
            if "is_active" in changes and updated.is_active != was_active:
                verb = "Deactivated" if not updated.is_active else "Reactivated"
            else:
                verb = "Updated"
            await admin_audit_log_crud.create_only_no_commit(
                db=db,
                obj_in=AdminAuditLogCreate(
                    admin_user_id=current_user.id,
                    action=f"{verb} camera device '{updated.device_name}' (id={updated.id})",
                ),
            )
            await db.commit()
            await db.refresh(updated)
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not update camera device.",
            )
        return CameraDeviceItem.model_validate(updated)

    async def get_for_ingest(
        self, db: AsyncSession, camera_device_id: int, location_id: int
    ) -> CameraDevice | None:
        """Return the camera iff it exists, is active, and is bound to
        `location_id`. None on any binding failure — callers map that to
        WS close 1008 / HTTP 400."""
        device = await camera_device_crud.get(db=db, id=camera_device_id)
        if device is None or not device.is_active:
            return None
        if device.location_id != location_id:
            return None
        return device

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


camera_device_service = CameraDeviceService()
