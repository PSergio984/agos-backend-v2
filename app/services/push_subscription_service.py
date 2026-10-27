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

        existing = await push_token_crud.get_by_token(token=data.token, db=db)

        if existing is None:
            await push_token_crud.create(data=data, db=db)
        elif existing.responder_id != data.responder_id:
            # An Expo token identifies the install, not the responder — on a
            # shared device the token must follow whoever just subscribed, or
            # the previous owner's alarms keep ringing this phone.
            await push_token_crud.reassign(
                push_token=existing,
                responder_id=data.responder_id,
                platform=data.platform,
                db=db,
            )


push_subscription_service = PushSubscriptionService()