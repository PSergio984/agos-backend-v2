from fastapi import APIRouter, Depends, HTTPException, Request
from app.api.v1.dependencies import require_auth, require_responder_auth, CurrentResponder
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limiter import limiter
from sqlalchemy.ext.asyncio import AsyncSession
from app.crud import citizen_subscription_crud, location_crud
from app.schemas import SubscriptionSchema, CitizenSubscriptionCreate, CitizenSubscriptionDelete
from app.schemas.subscription import SendAlarmSchema, SendNotificationSchema
from app.services import push_subscription_service, notification_service

router = APIRouter(
    prefix="/push",
    tags=["push"],
)

@router.get('/vapid-public-key')
def get_vapid_public_key():
    return {"public_key": settings.VAPID_PUBLIC_KEY}


@router.post("/subscribe", status_code=204)
async def save_subscription(
    data: SubscriptionSchema,
    current_responder: CurrentResponder = Depends(require_responder_auth),
    db: AsyncSession = Depends(get_db),
) -> None:
    if current_responder.id != str(data.responder_id):
        raise HTTPException(status_code=403, detail="Cannot subscribe for another responder")
    await push_subscription_service.subscribe(data=data, db=db)


@router.post("/send-notification", status_code=204, dependencies=[Depends(require_auth)])
async def send_notification_to_responders(payload: SendNotificationSchema, db: AsyncSession = Depends(get_db)) -> None:
    await notification_service.send_notification_to_subscribers(
        payload=payload,
        db=db,
    )


@router.post("/send-alarm", status_code=204, dependencies=[Depends(require_auth)])
async def send_alarm(payload: SendAlarmSchema, db: AsyncSession = Depends(get_db)) -> None:
    """Admin-triggered alarm (T8): same dispatch pipeline, ALARM tier. A NULL
    location_id is the barangay-wide path."""
    if payload.location_id is not None:
        # FOR SHARE — the gated-writer pattern: an alarm to a site being
        # deactivated serializes against update_site's deactivation commit.
        location = await location_crud.get_for_share(db=db, id=payload.location_id)
        if location is None:
            raise HTTPException(status_code=404, detail="Location not found")
        if not location.is_active:
            raise HTTPException(status_code=400, detail="Site is not active")
    await notification_service.send_alarm(
        db=db,
        location_id=payload.location_id,
        title=payload.title,
        message=payload.message,
    )


@router.post("/subscribe-citizen", status_code=204)
@limiter.limit("20/minute")
async def subscribe_citizen(
    request: Request,
    data: CitizenSubscriptionCreate,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Anonymous, location-scoped citizen push subscription (F4/F6). No auth, no PII."""
    # FOR SHARE: the upsert serializes against site deactivation.
    location = await location_crud.get_for_share(db=db, id=data.location_id)
    if location is None:
        raise HTTPException(status_code=404, detail="Location not found")
    if not location.is_active:
        raise HTTPException(status_code=400, detail="Site is not active")
    await citizen_subscription_crud.upsert(db=db, data=data)


@router.delete("/subscribe-citizen", status_code=204)
@limiter.limit("20/minute")
async def unsubscribe_citizen(
    request: Request,
    data: CitizenSubscriptionDelete,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Citizen turns off alerts — remove the subscription by endpoint."""
    await citizen_subscription_crud.delete_by_endpoint(
        db=db, location_id=data.location_id, endpoint=data.endpoint
    )
