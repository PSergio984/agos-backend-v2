from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from app.api.v1.dependencies import CurrentResponder, CurrentUser
from app.crud import admin_audit_log_crud, location_crud
from app.crud import notification_delivery_crud
from app.crud import notification_dispatch_crud
from app.crud import responder_deployment_crud
from app.crud import responder_otp_verification_crud
from app.crud.responder_group import responder_group_crud
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta, timezone
from app.core.config import settings
from uuid import UUID
import secrets
from app.schemas import ResponderDetails, ResponderOTPVerificationCreate, ResponderOTPVerifyRequest, ResponderSendSMSRequest, ResponderSelfUpdate
from app.models.responder_related.responders import NotificationPreference
from app.schemas.responder import ResponderForApproval
from app.services.sms_service import sms_service
from app.crud import acknowledgement_crud
from app.models import Acknowledgement
from app.core.security import get_otp_hash, verify_otp as verify_otp_hash, create_responder_token
from app.models import Responder
from app.crud.responder import responder_crud
from app.schemas import ResponderOTPVerifyResponse, AlertListItem, AlertPaginatedResponse
from app.models.notification_template import NotificationType
from app.models.responder_related.group import DEFAULT_ACTIVE_RESPONDERS_GROUP_NAME
from app.schemas import AcknowledgeNotifRequest, AcknowledgeNotifResponse, ClaimResponse
from app.schemas import ResponderActiveSiteResponse, AdminAuditLogCreate
from app.models.responder_related.responders import ResponderStatus
from app.services.websocket_service import websocket_service


class ResponderAppService:
    

    async def get_responder_for_approval(self, phone_number: str, db: AsyncSession) -> ResponderForApproval | None:
        responder: Responder = await responder_crud.get_by_phone_number(db=db, phone_number=phone_number)

        if not responder:
            raise HTTPException(status_code=404, detail="Phone number not registered.")
        
        dev_otp = await self.send_otp(responder=responder, db=db)

        return ResponderForApproval(
            responder_id=responder.id,
            first_name=responder.first_name,
            last_name=responder.last_name,
            phone_number=responder.phone_number,
            status=responder.status,
            dev_otp=dev_otp,
        )


    async def get_responder_details_for_app(self, responder_id: UUID, db: AsyncSession) -> ResponderDetails:
        responder: Responder = await responder_crud.get_responder_details_for_app(db=db, responder_id=responder_id)
        
        if not responder:
            raise HTTPException(status_code=404, detail="Responder not found.")
        
        return ResponderDetails(
            id=str(responder.id),
            first_name=responder.first_name,
            last_name=responder.last_name,
            status=responder.status,
            phone_number=responder.phone_number,
            location_id=responder.location_id,
            location_name=responder.location.name,
            active_location_id=responder.active_location_id,
            active_location_name=responder.active_location.name if responder.active_location else None,
            created_at=responder.created_at,
            activated_at=responder.activated_at
        )


    async def update_responder_profile(
        self,
        responder_id: UUID,
        payload: ResponderSelfUpdate,
        db: AsyncSession,
    ) -> ResponderDetails:
        await responder_crud.update_details(
            db=db,
            responder_id=responder_id,
            first_name=payload.first_name,
            last_name=payload.last_name,
            phone_number=None,
        )
        return await self.get_responder_details_for_app(responder_id=responder_id, db=db)


    async def get_unread_alerts_count(self, responder_id: UUID, db: AsyncSession) -> int:
        # Same site scope as the feed — a badge that counts hidden alerts lies.
        responder = await responder_crud.get(db=db, id=responder_id)
        effective_site = (
            responder.active_location_id or responder.location_id if responder else None
        )
        return await notification_delivery_crud.get_unread_alerts_count(
            responder_id=responder_id,
            db=db,
            effective_location_id=effective_site,
        )


    async def get_responder_alerts(
        self,
        responder_id: UUID,
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
        notification_type: NotificationType | None = None,
    ) -> AlertPaginatedResponse:
        # Presence-scoped feed: current site + site-less announcements only.
        responder = await responder_crud.get(db=db, id=responder_id)
        effective_site = (
            responder.active_location_id or responder.location_id if responder else None
        )
        deliveries, has_more = await notification_delivery_crud.get_alerts_per_responder(
            responder_id=responder_id,
            db=db,
            page=page,
            page_size=page_size,
            notification_type=notification_type,
            effective_location_id=effective_site,
        )

        return AlertPaginatedResponse(
            items=[
                AlertListItem(
                    id=delivery.id,
                    dispatch_id=delivery.dispatch.id,
                    type=delivery.dispatch.type,
                    title=delivery.dispatch.title,
                    message=delivery.dispatch.message,
                    timestamp=delivery.sent_at or delivery.created_at,
                    is_acknowledged=delivery.acknowledgement is not None,
                    acknowledged_at=delivery.acknowledgement.acknowledged_at if delivery.acknowledgement else None,
                    acknowledge_message=delivery.acknowledgement.message if delivery.acknowledgement else None,
                    location_id=delivery.dispatch.location_id,
                    claimed_by=delivery.dispatch.claimed_by,
                    claimed_by_name=(
                        f"{delivery.dispatch.claimed_by_responder.first_name} "
                        f"{delivery.dispatch.claimed_by_responder.last_name}"
                        if delivery.dispatch.claimed_by_responder
                        else None
                    ),
                    claimed_at=delivery.dispatch.claimed_at,
                ) for delivery in deliveries
            ],
            has_more=has_more,
        )


    async def acknowledge_alert(self, payload: AcknowledgeNotifRequest, db: AsyncSession) -> AcknowledgeNotifResponse:
        # Verify the delivery exists and belongs to this responder so an invalid
        # or foreign delivery_id returns a clear 404 instead of a raw 500.
        delivery = await notification_delivery_crud.get(db=db, id=payload.delivery_id)
        if delivery is None or delivery.responder_id != payload.responder_id:
            raise HTTPException(status_code=404, detail="Notification delivery not found.")

        try:
            ack: Acknowledgement = await acknowledgement_crud.create_acknowledgement(
                responder_id=payload.responder_id,
                delivery_id=payload.delivery_id,
                message=payload.message,
                db=db
            )
        except IntegrityError:
            # Unique constraint on delivery_id — already acknowledged (possibly a
            # concurrent request that won the race).
            await db.rollback()
            raise HTTPException(status_code=409, detail="This alert has already been acknowledged.")

        return AcknowledgeNotifResponse(
            acknowledged_at=ack.acknowledged_at,
            acknowledge_message=ack.message
        )


    async def claim_alert(
        self,
        dispatch_id: int,
        current_responder: CurrentResponder,
        db: AsyncSession,
    ) -> ClaimResponse:
        """"On it" claim — marks the shared dispatch as taken by this responder
        and broadcasts it to the location so nobody else drives out."""
        # Lock the row so two concurrent claims can't both pass the checks.
        dispatch = await notification_dispatch_crud.get_with_lock(db=db, id=dispatch_id)
        # Mirror acknowledge_alert: a responder can only act on a dispatch they
        # were actually sent — unknown/foreign ids get the same 404.
        delivery = await notification_delivery_crud.get_for_responder_dispatch(
            db=db, dispatch_id=dispatch_id, responder_id=current_responder.id
        )
        if dispatch is None or delivery is None:
            raise HTTPException(status_code=404, detail="Alert not found.")
        responder: Responder = await responder_crud.get(db=db, id=current_responder.id)
        if responder is None:
            raise HTTPException(status_code=404, detail="Alert not found.")
        # The claim follows the feed's scoping: a dispatch for another site is
        # not claimable even though the delivery still exists (e.g. the
        # responder switched sites after the alert went out). NULL-site
        # announcements stay claimable — they reached every site.
        effective_site = responder.active_location_id or responder.location_id
        if dispatch.location_id is not None and dispatch.location_id != effective_site:
            raise HTTPException(status_code=404, detail="Alert not found.")
        if dispatch.type != NotificationType.BLOCKAGE:
            raise HTTPException(
                status_code=403, detail="Alerts of this type cannot be claimed."
            )
        if dispatch.claimed_by is not None:
            raise HTTPException(
                status_code=409, detail="This alert has already been claimed."
            )
        if await notification_delivery_crud.dispatch_has_acknowledgement(
            db=db, dispatch_id=dispatch_id
        ):
            raise HTTPException(
                status_code=409, detail="This alert has already been handled."
            )

        dispatch.claimed_by = responder.id
        dispatch.claimed_at = datetime.now(timezone.utc)
        await db.commit()

        claimer_name = f"{responder.first_name} {responder.last_name}"
        claim_data = {
            "dispatch_id": dispatch.id,
            "claimed_by": str(responder.id),
            "claimed_by_name": claimer_name,
            "claimed_at": dispatch.claimed_at.isoformat(),
        }
        # The claim broadcast follows the dispatch's site — that's the channel
        # the alert went out on. A NULL-site dispatch is an announcement every
        # site received, so every site's channel gets the claim too.
        if dispatch.location_id is None:
            location_ids = await location_crud.get_all_ids(db=db)
        else:
            location_ids = [dispatch.location_id]
        for location_id in location_ids:
            await websocket_service.broadcast_update(
                "alert_claimed", claim_data, location_id=location_id
            )

        return ClaimResponse(
            dispatch_id=dispatch.id,
            claimed_by=responder.id,
            claimed_by_name=claimer_name,
            claimed_at=dispatch.claimed_at,
        )


    async def set_active_site(
        self, responder_id: UUID, location_id: int, db: AsyncSession,
        current_user: CurrentUser | None = None,
    ) -> ResponderActiveSiteResponse:
        """Presence: the responder marks which site they're on. Valid choices
        are their home site or the site of their open deployment — anything
        else means they're not a member there."""
        # Lock the responder row so a concurrent deployment close can't race
        # this write (the close path clears active_location_id under the lock).
        responder: Responder = await responder_crud.get_with_lock(db=db, id=responder_id)
        if not responder:
            raise HTTPException(status_code=404, detail="Responder not found.")

        if location_id != responder.location_id:
            open_deployment = await responder_deployment_crud.get_open_for_responder(
                db=db, responder_id=responder.id
            )
            # A deployment open-ended but scheduled for the future isn't a
            # valid presence target yet — "open" means started and unclosed.
            if (
                open_deployment is None
                or open_deployment.location_id != location_id
                or open_deployment.starts_at > datetime.now(timezone.utc)
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Responder is not deployed to that site.",
                )

        # Presence on a deactivated site is meaningless — applies to the home
        # site too, since a site can be deactivated while it's someone's home.
        target = await location_crud.get(db=db, id=location_id)
        if target is None or not target.is_active:
            raise HTTPException(
                status_code=400,
                detail="That site is not active.",
            )

        responder.active_location_id = location_id
        db.add(responder)
        if current_user is not None:
            await admin_audit_log_crud.create_only_no_commit(
                db=db,
                obj_in=AdminAuditLogCreate(
                    admin_user_id=current_user.id,
                    action=(
                        f"Set active site to '{target.name}' for responder "
                        f"'{responder.first_name} {responder.last_name}'"
                    ),
                ),
            )
        await db.commit()

        return ResponderActiveSiteResponse(
            location_id=responder.location_id,
            active_location_id=responder.active_location_id,
        )

    async def admin_set_active_site(
        self, responder_id: UUID, location_id: int | None, db: AsyncSession,
        current_user: CurrentUser | None = None,
    ) -> ResponderActiveSiteResponse:
        """Admin override of presence: NULL clears (falls back to home site);
        a concrete site must still be one the responder is eligible for —
        admins override *selection*, not membership."""
        if location_id is None:
            responder: Responder = await responder_crud.get_with_lock(db=db, id=responder_id)
            if not responder:
                raise HTTPException(status_code=404, detail="Responder not found.")
            responder.active_location_id = None
            db.add(responder)
            if current_user is not None:
                await admin_audit_log_crud.create_only_no_commit(
                    db=db,
                    obj_in=AdminAuditLogCreate(
                        admin_user_id=current_user.id,
                        action=(
                            f"Cleared active site for responder "
                            f"'{responder.first_name} {responder.last_name}'"
                        ),
                    ),
                )
            await db.commit()
            return ResponderActiveSiteResponse(
                location_id=responder.location_id,
                active_location_id=None,
            )
        return await self.set_active_site(
            responder_id=responder_id, location_id=location_id, db=db,
            current_user=current_user,
        )


    async def get_responder_notif_preferences(self, responder_id: UUID, db: AsyncSession) -> NotificationPreference:
        responder: Responder = await responder_crud.get(db=db, id=responder_id)
        
        if not responder:
            raise HTTPException(status_code=404, detail="Responder not found.")
        
        return responder.notif_preferences


    async def update_responder_notif_preferences(
        self, responder_id: UUID, key: str, value: bool, db: AsyncSession
    ) -> None:
        responder: Responder = await responder_crud.get(db=db, id=responder_id)
        if not responder:
            raise HTTPException(status_code=404, detail="Responder not found.")

        prefs = responder.notif_preferences
        responder.notif_preferences = prefs.model_copy(update={key: value})
        db.add(responder)
        await db.commit()


    async def send_otp(self, responder: Responder, db: AsyncSession) -> str | None:

        # Generate OTP
        otp = "".join(secrets.choice("0123456789") for _ in range(settings.OTP_LENGTH))
        otp_hash = get_otp_hash(otp=otp)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.OTP_EXPIRY_MINUTES)
        
        # Store OTP (upsert: deletes existing and creates new)
        obj_in = ResponderOTPVerificationCreate(
            responder_id=responder.id,
            otp_hash=otp_hash,
            expires_at=expires_at,
        )
        await responder_otp_verification_crud.upsert_otp(db=db, obj_in=obj_in)
        
        await sms_service.send_one_sms(
            phone_number=responder.phone_number,
            message=f"Your AGOS OTP code is: {otp}"
        )

        return otp if settings.EXPOSE_DEV_OTP else None


    async def resend_otp(self, responder_id: UUID, db: AsyncSession) -> str | None:
        responder = await responder_crud.get(db=db, id=responder_id)
        
        if not responder:
            raise HTTPException(status_code=404, detail="Responder not found.")
        
        return await self.send_otp(responder=responder, db=db)


    async def verify_otp(self, verify_request: ResponderOTPVerifyRequest, db: AsyncSession):
        record = await responder_otp_verification_crud.get_by_responder_id(
            db=db, 
            responder_id=verify_request.responder_id
        )
        
        if not record:
            return ResponderOTPVerifyResponse(
                success=False,
                message="No OTP request found. Please request a new one.",
                requires_resend=True
            )
        
        # Check expiration
        if record.expires_at < datetime.now(timezone.utc):
            await responder_otp_verification_crud.delete_by_responder_id(db=db, responder_id=verify_request.responder_id)
            return ResponderOTPVerifyResponse(
                success=False,
                message="OTP has expired. Please request a new one.",
                requires_resend=True
            )
        
        # Verify OTP
        if not verify_otp_hash(plain_otp=verify_request.otp, hashed_otp=record.otp_hash):
            if record.attempt_count + 1 >= settings.OTP_ATTEMPT_LIMIT:
                await responder_otp_verification_crud.delete_by_responder_id(db=db, responder_id=verify_request.responder_id)
                return ResponderOTPVerifyResponse(
                    success=False,
                    message="Too many incorrect attempts. Please request a new OTP.",
                    requires_resend=True
                )
            
            await responder_otp_verification_crud.increment_attempt_count(db=db, record=record)
            return ResponderOTPVerifyResponse(
                success=False,
                message="Incorrect OTP code.",
                requires_resend=False  # Can still retry
            )
        
        # Success - activate and add to group only if still pending (active responders re-logging in skip this)
        responder = await responder_crud.get(db=db, id=verify_request.responder_id)
        if responder.status == ResponderStatus.PENDING:
            # Join the site's own "All Active Responders" group (home site, not
            # the legacy NULL group). ensure_exists may commit internally, so it
            # runs before the staged writes below.
            active_group = await responder_group_crud.ensure_exists(
                db=db,
                name=DEFAULT_ACTIVE_RESPONDERS_GROUP_NAME,
                location_id=responder.location_id,
            )
            await responder_crud.activate(db=db, responder_id=verify_request.responder_id, commit=False)
            await responder_group_crud.add_member(db=db, group_id=active_group.id, responder_id=verify_request.responder_id, commit=False)
        await responder_otp_verification_crud.delete_by_responder_id(db=db, responder_id=verify_request.responder_id)

        token = create_responder_token(str(verify_request.responder_id))

        return ResponderOTPVerifyResponse(
            success=True,
            message="OTP verified successfully.",
            requires_resend=False,
            responder_token=token,
        )


    async def send_sms(self, send_request: ResponderSendSMSRequest, db: AsyncSession) -> None:
        """Send SMS to multiple responders. Fails if any responder ID is invalid."""
        responders = await responder_crud.get_by_ids(db=db, ids=send_request.responder_ids)
        
        if len(responders) != len(send_request.responder_ids):
            raise HTTPException(
                status_code=404, 
                detail="One or more responder IDs not found."
            )
        
        phone_numbers = [responder.phone_number for responder in responders]

        await sms_service.send_bulk_sms(
            phone_numbers=phone_numbers,
            message=send_request.message
        )


responder_app_service = ResponderAppService()
