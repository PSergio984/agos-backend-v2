"""Site registry CRUD — the multi-site counterpart of the ResponderGroup
3-layer shape. "Site" is the domain term; the code/schema name is Location."""

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser
from app.core.state import fusion_state_manager
from app.crud import admin_audit_log_crud, location_crud
from app.services.cache_service import cache_service
from app.schemas import (
    AdminAuditLogCreate,
    LocationCreate,
    LocationResponse,
    LocationUpdate,
)


class LocationService:

    async def list_sites(
        self, db: AsyncSession, include_inactive: bool = False
    ) -> list[LocationResponse]:
        locations = await location_crud.get_all(db=db)
        if not include_inactive:
            # Site pickers only show sites you can actually operate on.
            locations = [loc for loc in locations if loc.is_active]
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
            created = await location_crud.create_and_return_no_commit(db=db, obj_in=payload)
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
                status_code=status.HTTP_409_CONFLICT,
                detail="A site with that name already exists.",
            )
        # A site created at runtime still needs fusion state, otherwise its
        # readings cannot score until the next restart.
        fusion_state_manager.start_fusion_analysis_state(
            location_id=created.id,
            sensor_device_id=None,
            camera_device_id=None,
        )
        # Runtime site changes must drop the startup-built caches — weather
        # fetch, summary loops and WS initial data iterate them.
        cache_service.invalidate_location_caches()
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
        try:
            updated = await location_crud.update_no_commit(db=db, db_obj=location, obj_in=payload)
            await admin_audit_log_crud.create_only_no_commit(
                db=db,
                obj_in=AdminAuditLogCreate(
                    admin_user_id=current_user.id,
                    action=f"Updated site '{updated.name}' (id={updated.id})",
                ),
            )
            await db.commit()
            await db.refresh(updated)
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A site with that name already exists.",
            )
        cache_service.invalidate_location_caches()
        return LocationResponse.model_validate(updated)


location_service = LocationService()
