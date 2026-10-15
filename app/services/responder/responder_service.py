from app.models import Responder
from app.schemas import ResponderCreate, ResponderListItem, ResponderDetailsResponse, ResponderAdminUpdate
from fastapi import HTTPException
from uuid import UUID
from app.crud.responder import responder_crud
from app.crud.location import location_crud
from app.models.responder_related.push_subscription import PushSubscription
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class ResponderService:

    async def get_all_responders(self, db: AsyncSession) -> list[ResponderListItem]:
        responders = await responder_crud.get_all(db=db)
        ids_with_push = await responder_crud.get_responder_ids_with_push_subscription(db=db)
        return [
            ResponderListItem(
                id=r.id,
                first_name=r.first_name,
                last_name=r.last_name,
                phone_number=r.phone_number,
                status=r.status,
                location_id=r.location_id,
                has_push_subscription=r.id in ids_with_push,
            )
            for r in responders
        ]


    async def get_responder_details(self, responder_id: str, db: AsyncSession) -> ResponderDetailsResponse | None:
        responder: Responder = await responder_crud.get_details(db=db, id=responder_id)
        
        if not responder:
            raise HTTPException(status_code=404, detail="Responder not found.")
        
        return ResponderDetailsResponse(
            created_at=responder.created_at,
            created_by=f"{responder.admin_user.first_name} {responder.admin_user.last_name}",
            activated_at=responder.activated_at
        )


    async def bulk_create_responders(self, responders: list[ResponderCreate], db: AsyncSession, user_id: str) -> list[ResponderListItem]:
        phone_numbers = [r.phone_number for r in responders]

        seen: set[str] = set()
        intra_duplicates: list[str] = []
        for phone in phone_numbers:
            if phone in seen and phone not in intra_duplicates:
                intra_duplicates.append(phone)
            seen.add(phone)
        if intra_duplicates:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate phone numbers in upload: {', '.join(intra_duplicates)}",
            )

        existing = await responder_crud.get_existing_phone_numbers(db=db, phone_numbers=phone_numbers)
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Phone numbers already registered: {', '.join(sorted(existing))}",
            )

        # Home site is client-supplied now — a bad id must surface as a 400,
        # not a FK-violation 500, and inactive sites aren't valid homes.
        location_ids = {r.location_id for r in responders}
        active_ids = await location_crud.get_active_ids(db=db, ids=list(location_ids))
        invalid_ids = location_ids - active_ids
        if invalid_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown or inactive location_ids: {sorted(invalid_ids)}",
            )

        created_responders = await responder_crud.bulk_create_and_return(db=db, objs_in=responders, created_by_id=user_id)
        return [
            ResponderListItem(
                id=responder.id,
                first_name=responder.first_name,
                last_name=responder.last_name,
                phone_number=responder.phone_number,
                status=responder.status,
                location_id=responder.location_id,
                has_push_subscription=False,  # newly created responders have no subscriptions yet
            ) for responder in created_responders
        ]
    

    async def update_responder_details(
        self,
        responder_id: UUID,
        payload: ResponderAdminUpdate,
        db: AsyncSession,
    ) -> ResponderListItem:
        # Phone change does not invalidate the responder JWT or push subscriptions:
        # both key off responder_id, not phone_number.
        updated = await responder_crud.update_details(
            db=db,
            responder_id=responder_id,
            first_name=payload.first_name,
            last_name=payload.last_name,
            phone_number=payload.phone_number,
        )
        has_push = await db.execute(
            select(PushSubscription.id).where(PushSubscription.responder_id == updated.id).limit(1)
        )
        return ResponderListItem(
            id=updated.id,
            first_name=updated.first_name,
            last_name=updated.last_name,
            phone_number=updated.phone_number,
            status=updated.status,
            location_id=updated.location_id,
            has_push_subscription=has_push.scalar() is not None,
        )


responder_service = ResponderService()