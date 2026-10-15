from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from fastapi import Depends
from uuid import UUID
from app.schemas import ResponderDetails, NotifPreferenceUpdateRequest, AlertPaginatedResponse, ResponderSelfUpdate
from app.schemas import ResponderForApproval, ResponderOTPDispatchResponse, ResponderOTPVerifyRequest, ResponderOTPVerifyResponse, ResponderSendSMSRequest, ResponderRegistrationRequest
from app.schemas import AcknowledgeNotifRequest, AcknowledgeNotifResponse
from app.schemas import ClaimRequest, ClaimResponse
from app.schemas import ResponderActiveSiteResponse, ResponderActiveSiteUpdate
from app.models.responder_related.responders import NotificationPreference
from app.models.notification_template import NotificationType
from app.services import responder_app_service
from app.api.v1.dependencies import require_responder_auth, CurrentResponder, require_auth, CurrentUser
from app.core.rate_limiter import limiter

router = APIRouter(prefix="/responder", tags=["responder app"])


def _validate_responder_id(current_responder: CurrentResponder, responder_id: UUID) -> None:
    """Ensure the authenticated responder can only access their own data."""
    if current_responder.id != str(responder_id):
        raise HTTPException(status_code=403, detail="Access denied")


@router.get("/{responder_id}", response_model=ResponderDetails)
async def get_responder_details_for_app(
    responder_id: UUID,
    current_responder: CurrentResponder = Depends(require_responder_auth),
    db: AsyncSession = Depends(get_db)) -> ResponderDetails:

    _validate_responder_id(current_responder, responder_id)
    return await responder_app_service.get_responder_details_for_app(responder_id=responder_id, db=db)


@router.patch("/{responder_id}/profile", response_model=ResponderDetails)
async def update_responder_profile(
    responder_id: UUID,
    payload: ResponderSelfUpdate,
    current_responder: CurrentResponder = Depends(require_responder_auth),
    db: AsyncSession = Depends(get_db),
) -> ResponderDetails:

    _validate_responder_id(current_responder, responder_id)
    return await responder_app_service.update_responder_profile(responder_id=responder_id, payload=payload, db=db)


@router.get("/unread-alerts-count/{responder_id}", response_model=int)
async def get_unread_alerts_count(
    responder_id: UUID,
    current_responder: CurrentResponder = Depends(require_responder_auth),
    db: AsyncSession = Depends(get_db)) -> int:

    _validate_responder_id(current_responder, responder_id)
    return await responder_app_service.get_unread_alerts_count(responder_id=responder_id, db=db)


@router.get("/alerts/{responder_id}", response_model=AlertPaginatedResponse)
async def get_responder_alerts(
    responder_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    type: NotificationType | None = None,
    current_responder: CurrentResponder = Depends(require_responder_auth),
    db: AsyncSession = Depends(get_db)):

    _validate_responder_id(current_responder, responder_id)
    return await responder_app_service.get_responder_alerts(
        responder_id=responder_id, db=db, page=page, page_size=page_size, notification_type=type
    )


@router.post("/acknowledge-alert", response_model=AcknowledgeNotifResponse)
async def acknowledge_alert(
    payload: AcknowledgeNotifRequest,
    current_responder: CurrentResponder = Depends(require_responder_auth),
    db: AsyncSession = Depends(get_db)) -> AcknowledgeNotifResponse:

    _validate_responder_id(current_responder, payload.responder_id)
    return await responder_app_service.acknowledge_alert(payload=payload, db=db)


@router.post("/claim", response_model=ClaimResponse)
@limiter.limit("20/minute")
async def claim_alert(
    request: Request,
    payload: ClaimRequest,
    current_responder: CurrentResponder = Depends(require_responder_auth),
    db: AsyncSession = Depends(get_db)) -> ClaimResponse:

    # Identity comes from the token only — a responder can't claim as someone else.
    return await responder_app_service.claim_alert(
        dispatch_id=payload.dispatch_id,
        current_responder=current_responder,
        db=db,
    )


@router.get("/notif-preferences/{responder_id}", response_model=NotificationPreference)
async def get_responder_notif_preferences(
    responder_id: UUID,
    current_responder: CurrentResponder = Depends(require_responder_auth),
    db: AsyncSession = Depends(get_db)) -> NotificationPreference:

    _validate_responder_id(current_responder, responder_id)
    return await responder_app_service.get_responder_notif_preferences(responder_id=responder_id, db=db)


@router.put("/notif-preferences/{responder_id}", status_code=204)
async def update_responder_notif_preferences(
    responder_id: UUID,
    request: NotifPreferenceUpdateRequest,
    current_responder: CurrentResponder = Depends(require_responder_auth),
    db: AsyncSession = Depends(get_db)) -> None:

    _validate_responder_id(current_responder, responder_id)
    await responder_app_service.update_responder_notif_preferences(
        responder_id=responder_id, key=request.key, value=request.value, db=db
    )


@router.post("/active-site", response_model=ResponderActiveSiteResponse)
async def set_active_site(
    payload: ResponderActiveSiteUpdate,
    current_responder: CurrentResponder = Depends(require_responder_auth),
    db: AsyncSession = Depends(get_db)) -> ResponderActiveSiteResponse:

    # Identity comes from the token — a responder can only set their own site.
    return await responder_app_service.set_active_site(
        responder_id=current_responder.id,
        location_id=payload.location_id,
        db=db,
    )


# --- Pre-auth endpoints (no token required) ---

@router.post("/for-approval", response_model=ResponderForApproval)
@limiter.limit("5/minute")
async def get_responder_for_approval(
    request: Request,
    payload: ResponderRegistrationRequest,
    db: AsyncSession = Depends(get_db)) -> ResponderForApproval:

    return await responder_app_service.get_responder_for_approval(phone_number=payload.phone_number, db=db)


@router.post("/resend-otp/{responder_id}", response_model=ResponderOTPDispatchResponse)
@limiter.limit("3/minute")
async def resend_otp(
    request: Request,
    responder_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> ResponderOTPDispatchResponse:
    dev_otp = await responder_app_service.resend_otp(responder_id=responder_id, db=db)
    return ResponderOTPDispatchResponse(dev_otp=dev_otp)


@router.post("/verify-otp", response_model=ResponderOTPVerifyResponse)
@limiter.limit("5/minute")
async def verify_otp(
    request: Request,
    verify_request: ResponderOTPVerifyRequest,
    db: AsyncSession = Depends(get_db)) -> ResponderOTPVerifyResponse:

    return await responder_app_service.verify_otp(verify_request=verify_request, db=db)


@router.get("/water-level-trend/{location_id}")
async def get_water_level_trend(
    location_id: int,
    current_responder: CurrentResponder = Depends(require_responder_auth),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    from app.services.cache_service import cache_service
    from app.crud.sensor_reading import sensor_reading_crud

    try:
        device_ids = await cache_service.get_device_ids_per_location(db, location_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Site not found.")
    if not device_ids or not device_ids.sensor_device_id:
        return []

    return await sensor_reading_crud.get_recent_trend(
        db=db, sensor_device_id=device_ids.sensor_device_id
    )


# --- Admin-only endpoint ---

@router.post("/send-sms", status_code=204)
@limiter.limit("10/minute")
async def send_sms(
    request: Request,
    send_request: ResponderSendSMSRequest,
    current_user: CurrentUser = Depends(require_auth),
    db: AsyncSession = Depends(get_db)) -> None:

    await responder_app_service.send_sms(send_request=send_request, db=db)
