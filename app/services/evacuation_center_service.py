"""F5 — evacuation center CRUD service (mirrors the ResponderGroup 3-layer shape)."""

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser
from app.crud import admin_audit_log_crud, evacuation_center_crud, location_crud
from app.models import EvacuationCenter
from app.schemas import (
    AdminAuditLogCreate,
    EvacuationCenterCreate,
    EvacuationCenterResponse,
    EvacuationCenterUpdate,
)


class EvacuationCenterService:

    async def get_by_location(
        self, db: AsyncSession, location_id: int
    ) -> list[EvacuationCenterResponse]:
        centers = await evacuation_center_crud.get_by_location(db=db, location_id=location_id)
        return [EvacuationCenterResponse.model_validate(c) for c in centers]

    async def create_center(
        self, db: AsyncSession, payload: EvacuationCenterCreate, current_user: CurrentUser
    ) -> EvacuationCenterResponse:
        site = await location_crud.get(db=db, id=payload.location_id)
        if site is None or not site.is_active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Site not found or inactive.",
            )
        try:
            created = await evacuation_center_crud.create_and_return(db=db, obj_in=payload)
            await admin_audit_log_crud.create_only_no_commit(
                db=db,
                obj_in=AdminAuditLogCreate(
                    admin_user_id=current_user.id,
                    action=f"Created evacuation center '{payload.name}'",
                ),
            )
            await db.commit()
            await db.refresh(created)
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid evacuation center (check location_id).",
            )
        return EvacuationCenterResponse.model_validate(created)

    async def update_center(
        self,
        db: AsyncSession,
        center_id: int,
        payload: EvacuationCenterUpdate,
        current_user: CurrentUser,
    ) -> EvacuationCenterResponse:
        existing: EvacuationCenter | None = await evacuation_center_crud.get(db=db, id=center_id)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Evacuation center not found."
            )

        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            return EvacuationCenterResponse.model_validate(existing)

        previous_status = existing.status.value if hasattr(existing.status, "value") else existing.status
        try:
            updated = await evacuation_center_crud.update(db=db, db_obj=existing, obj_in=payload)

            if "status" in changes:
                action = (
                    f"Flipped evacuation center '{updated.name}' status "
                    f"{previous_status} → {changes['status']}"
                )
            else:
                action = f"Updated evacuation center '{updated.name}'"
            await admin_audit_log_crud.create_only_no_commit(
                db=db,
                obj_in=AdminAuditLogCreate(admin_user_id=current_user.id, action=action),
            )
            await db.commit()
            await db.refresh(updated)
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not update evacuation center.",
            )
        return EvacuationCenterResponse.model_validate(updated)

    async def delete_center(
        self, db: AsyncSession, center_id: int, current_user: CurrentUser
    ) -> None:
        existing: EvacuationCenter | None = await evacuation_center_crud.get(db=db, id=center_id)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Evacuation center not found."
            )
        name = existing.name
        await db.delete(existing)
        await admin_audit_log_crud.create_only_no_commit(
            db=db,
            obj_in=AdminAuditLogCreate(
                admin_user_id=current_user.id,
                action=f"Deleted evacuation center '{name}'",
            ),
        )
        await db.commit()


evacuation_center_service = EvacuationCenterService()
