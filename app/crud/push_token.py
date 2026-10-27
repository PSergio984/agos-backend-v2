from app.schemas.subscription import ExpoSubscriptionSchema
from .base import CRUDBase
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.responder_related.push_token import PushToken
from uuid import UUID

class CRUDPushToken(CRUDBase):

    async def get_by_token(self, token: str, db: AsyncSession) -> PushToken | None:

        result = await db.execute(
            select(self.model).filter_by(token=token)
        )
        return result.scalar_one_or_none()


    async def reassign(self, push_token: PushToken, responder_id: UUID, platform: str, db: AsyncSession) -> None:

        push_token.responder_id = responder_id
        push_token.platform = platform
        await db.commit()


    async def create(self, data: ExpoSubscriptionSchema, db: AsyncSession) -> None:

        push_token = PushToken(
            responder_id=data.responder_id,
            token=data.token,
            platform=data.platform
        )
        db.add(push_token)
        await db.commit()


    async def get_by_responder_ids(self, responder_ids: list[UUID], db: AsyncSession) -> list[PushToken]:

        if not responder_ids:
            return []

        result = await db.execute(
            select(self.model)
            .where(PushToken.responder_id.in_(responder_ids))
            .execution_options(populate_existing=False)
        )
        return list(result.scalars().all())

push_token_crud = CRUDPushToken(PushToken)
