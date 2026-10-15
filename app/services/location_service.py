"""Site registry CRUD — the multi-site counterpart of the ResponderGroup
3-layer shape. "Site" is the domain term; the code/schema name is Location."""

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser
from app.crud import admin_audit_log_crud, location_crud
from app.schemas import (
    AdminAuditLogCreate,
    LocationCreate,
    LocationResponse,
    LocationUpdate,
)


class LocationService:

    async def list_sites(self, db: AsyncSession) -> list[LocationResponse]:
        locations = await location_crud.get_all(db=db)
        return [LocationResponse.model_validate(loc) for loc in locations]

    async def create_site(
        self, db: AsyncSession, payload: LocationCreate, current_user: CurrentUser
    ) -> LocationResponse:
        existing = await location_crud.get_by_name(db=db, name=payload.name)
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A site with that name already exists.",
            )
        try:
            created = await location_crud.create_and_return(db=db, obj_in=payload)
            await admin_audit_log_crud.create_only_no_commit(
                db=db,
                obj_in=AdminAuditLogCreate(
                    admin_user_id=current_user.id,
                    action=f"Created site '{payload.name}'",
                ),
            )
            await db.commit()
            await db.refresh(created)
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid site data.",
            )
        return LocationResponse.model_validate(created)

    async def update_site(
        self, db: AsyncSession, location_id: int, payload: LocationUpdate,
        current_user: CurrentUser
    ) -> LocationResponse:
        location = await location_crud.get(db=db, id=location_id)
        if location is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Site not found.",
            )
        if payload.name is not None and payload.name != location.name:
            existing = await location_crud.get_by_name(db=db, name=payload.name)
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A site with that name already exists.",
                )
        updated = await location_crud.update(db=db, db_obj=location, obj_in=payload)
        await admin_audit_log_crud.create_only_no_commit(
            db=db,
            obj_in=AdminAuditLogCreate(
                admin_user_id=current_user.id,
                action=f"Updated site '{updated.name}' (id={updated.id})",
            ),
        )
        await db.commit()
        await db.refresh(updated)
        return LocationResponse.model_validate(updated)


location_service = LocationService()
