from app.crud.base import CRUDBase
from fastapi import HTTPException
from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError
from app.models import Responder
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


    async def get_responder_ids_with_push_subscription(self, db: AsyncSession) -> set[UUID]:

        """Returns responder IDs that have at least one push subscription. Lightweight query, no relationship load."""
        result = await db.execute(
            select(PushSubscription.responder_id).distinct()
        )
        return {row[0] for row in result.all()}


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


    async def approve_responder(self, db: AsyncSession, responder: Responder, user_id: str) -> None:

        responder.status = 'approved'
        responder.approved_at = datetime.now(timezone.utc)
        responder.approved_by = user_id
        db.add(responder)
        # commited at the service layer for idempotency


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
    ) -> Responder:
        result = await db.execute(
            select(self.model).filter(self.model.id == responder_id)
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
