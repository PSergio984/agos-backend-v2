from app.crud.base import CRUDBase
from fastapi import HTTPException
from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from app.models import Location, Responder
from app.models.responder_related.push_subscription import PushSubscription
from app.models.responder_related.responders import ResponderStatus
from datetime import datetime, timezone
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID


class CRUDResponder(CRUDBase[None, None, None]):

    async def get_all(self, db: AsyncSession) -> list[Responder]:

        result = await db.execute(
            select(self.model)
            .options(selectinload(self.model.groups))
            .execution_options(populate_existing=False)
        )
        return result.scalars().unique().all()


    async def get_responder_ids_with_push_subscription(self, db: AsyncSession, location_id: int | None = None) -> set[UUID]:

        """Returns responder IDs that have at least one push subscription. Lightweight query, no relationship load.
        Only ACTIVE responders whose effective site is active — a non-active responder
        or a deactivated site must not receive fan-out. When location_id is given,
        only responders whose effective site matches are returned."""
        stmt = (
            select(PushSubscription.responder_id)
            .join(Responder, Responder.id == PushSubscription.responder_id)
            .join(
                Location,
                Location.id
                == func.coalesce(Responder.active_location_id, Responder.location_id),
            )
            .where(
                Responder.status == ResponderStatus.ACTIVE,
                Location.is_active,
            )
            .distinct()
        )
        if location_id is not None:
            stmt = stmt.where(
                func.coalesce(Responder.active_location_id, Responder.location_id) == location_id
            )
        result = await db.execute(stmt)
        return {row[0] for row in result.all()}


    async def get_ids_with_push_subscription(self, db: AsyncSession) -> set[UUID]:

        """Responders owning at least one push subscription — the admin list's
        has_push_subscription badge. Unlike get_responder_ids_with_push_subscription,
        no status/site filtering: the badge reports a stored subscription, not
        current reachability."""
        result = await db.execute(select(PushSubscription.responder_id).distinct())
        return {row[0] for row in result.all()}


    async def get_ids_in_effective_location(self, db: AsyncSession, responder_ids: list[UUID], location_id: int) -> list[UUID]:

        """Subset of the given responder IDs who can receive a send at
        location_id: effective site (active_location_id when set, else home
        location_id) matches, responder is ACTIVE, and that site is active —
        the same hygiene as the fan-out resolver, so retries/escalations can't
        reach non-active responders or deactivated sites."""
        if not responder_ids:
            return []
        result = await db.execute(
            select(self.model.id)
            .join(
                Location,
                Location.id
                == func.coalesce(self.model.active_location_id, self.model.location_id),
            )
            .where(
                self.model.id.in_(responder_ids),
                self.model.status == ResponderStatus.ACTIVE,
                Location.is_active,
                func.coalesce(self.model.active_location_id, self.model.location_id) == location_id,
            )
        )
        return list(result.scalars().all())


    async def get_reachable_ids(self, db: AsyncSession, responder_ids: list[UUID]) -> list[UUID]:

        """Subset of the given responder IDs who can currently receive a send
        — ACTIVE responders at an active effective site. The site-agnostic
        counterpart of get_ids_in_effective_location for barangay-wide sends."""
        if not responder_ids:
            return []
        result = await db.execute(
            select(self.model.id)
            .join(
                Location,
                Location.id
                == func.coalesce(self.model.active_location_id, self.model.location_id),
            )
            .where(
                self.model.id.in_(responder_ids),
                self.model.status == ResponderStatus.ACTIVE,
                Location.is_active,
            )
        )
        return list(result.scalars().all())


    async def clear_active_site(self, db: AsyncSession, location_id: int) -> None:
        """Site deactivation cleanup — presence overrides pointing at the dead
        site clear so affected responders fall back to their home site."""
        await db.execute(
            update(self.model)
            .where(self.model.active_location_id == location_id)
            .values(active_location_id=None)
        )


    async def get_by_phone_number(self, db: AsyncSession, phone_number: str) -> Responder | None:

        result = await db.execute(
            select(self.model)
            .filter(Responder.phone_number == phone_number)
            .execution_options(populate_existing=False)
        )
        return result.scalars().first()


    async def get_details(self, db : AsyncSession, id: str) -> Responder | None:

        result = await db.execute(
            select(self.model)
            .options(joinedload(self.model.admin_user)) # eager load
            .filter(self.model.id == id)
            .execution_options(populate_existing=False) # 
        )
        return result.scalars().first()


    async def get_responder_details_for_app(self, responder_id: UUID, db: AsyncSession) -> Responder:

        result = await db.execute(
            select(self.model)
            .options(
                joinedload(self.model.location),
                joinedload(self.model.active_location),
            ) # eager load home + active site names
            .filter(self.model.id == responder_id)
            .execution_options(populate_existing=False)
        )
        return result.scalars().first()


    # used in checking existing phone numbers
    async def record_exists(self, db: AsyncSession, phone_number: str) -> bool:

        result = await db.execute(
            select(exists().where(Responder.phone_number == phone_number))
        )
        return result.scalar()


    async def get_existing_phone_numbers(self, db: AsyncSession, phone_numbers: list[str]) -> set[str]:

        if not phone_numbers:
            return set()
        result = await db.execute(
            select(Responder.phone_number).where(Responder.phone_number.in_(phone_numbers))
        )
        return {row[0] for row in result.all()}


    async def get_by_ids(self, db: AsyncSession, ids: list) -> list[Responder]:

        if not ids:
            return []
        result = await db.execute(
            select(self.model).where(self.model.id.in_(ids))
            .execution_options(populate_existing=False)
        )
        return list(result.scalars().unique().all())


    async def bulk_create_and_return(self, db: AsyncSession, objs_in: list, created_by_id: UUID) -> list[Responder]:

        # Build all objects in one pass
        db_objs = [
            self.model(**obj_in.model_dump(), created_by=created_by_id)
            for obj_in in objs_in
        ]
        db.add_all(db_objs)
        await db.flush()  # Flush to get IDs without committing
        
        # Collect IDs and fetch all in a single query with groups preloaded
        obj_ids = [obj.id for obj in db_objs]
        result = await db.execute(
            select(self.model)
            .options(selectinload(self.model.groups))
            .where(self.model.id.in_(obj_ids))
        )
        await db.commit()
        return list(result.scalars().unique().all())


    async def update_details(
        self,
        db: AsyncSession,
        responder_id: UUID,
        *,
        first_name: str | None,
        last_name: str | None,
        phone_number: str | None,
        location_id: int | None = None,
    ) -> Responder:
        # FOR UPDATE: read-modify-write. Site-moving callers already hold the
        # location FOR SHARE (lock order location → responder); the row lock
        # serializes the location/active_location move against other
        # responder-row writers (#29).
        result = await db.execute(
            select(self.model)
            .filter(self.model.id == responder_id)
            .with_for_update()
        )
        responder = result.scalars().first()
        if not responder:
            raise HTTPException(status_code=404, detail="Responder not found.")

        if first_name is not None:
            responder.first_name = first_name
        if last_name is not None:
            responder.last_name = last_name
        if phone_number is not None:
            responder.phone_number = phone_number
        if location_id is not None:
            responder.location_id = location_id
            # Home move makes any presence override meaningless.
            responder.active_location_id = None

        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(status_code=409, detail="Phone number already in use.")
        await db.refresh(responder)
        return responder


    async def activate(self, db: AsyncSession, responder_id: UUID, *, commit: bool = True) -> None:

        stmt = (
            update(self.model)
            .where(self.model.id == responder_id)
            .values(
                status=ResponderStatus.ACTIVE,
                activated_at=datetime.now(timezone.utc)
            )
        )
        await db.execute(stmt)
        if commit:
            await db.commit()


responder_crud = CRUDResponder(Responder)
