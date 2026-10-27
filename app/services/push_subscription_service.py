from app.crud import push_subscription_crud, push_token_crud
from app.schemas import ExpoSubscriptionSchema, SubscriptionSchema
from sqlalchemy.ext.asyncio import AsyncSession


class PushSubscriptionService:

    async def subscribe(self, data: SubscriptionSchema, db: AsyncSession) -> None:

        existing = await push_subscription_crud.get_by_responder_id(
            responder_id=data.responder_id,
            endpoint=data.endpoint,
            db=db
        )

        if not existing:
            await push_subscription_crud.create(data=data, db=db)


    async def subscribe_expo(self, data: ExpoSubscriptionSchema, db: AsyncSession) -> None:

        existing = await push_token_crud.get_by_responder_id(
            responder_id=data.responder_id,
            token=data.token,
            db=db
        )

        if not existing:
            await push_token_crud.create(data=data, db=db)


push_subscription_service = PushSubscriptionService()