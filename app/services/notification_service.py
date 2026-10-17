import asyncio
import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from pywebpush import webpush, WebPushException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.crud import (
    notification_delivery_crud,
    notification_dispatch_crud,
    notification_template_crud,
    push_subscription_crud,
    responder_crud,
)
from app.models.notification_template import NotificationTemplate, NotificationType
from app.models.responder_related.notification_delivery import DeliveryStatus
from app.models.responder_related.push_subscription import PushSubscription
from app.schemas.subscription import (
    CustomNotificationPayload,
    SendNotificationSchema,
)


class PushResult:
    SENT = "sent"
    FAILED = "failed"
    GONE = "gone"  # 410 - subscription expired/unsubscribed, should be removed


class NotificationService:

    async def send_notification_to_subscribers(
        self,
        payload: SendNotificationSchema,
        db: AsyncSession,
        *,
        system_initiated: bool = False,
        location_id: int | None = None,
    ) -> int:

        responder_ids = list(dict.fromkeys(payload.responder_ids))
        if not responder_ids:
            return 0

        # Site-scoped dispatch: deliver only to responders whose effective site
        # (active_location_id ?? location_id) matches. A NULL location_id is a
        # global send — delivered regardless of site, but still only to
        # reachable responders, so retries/escalations can't re-ping suspended
        # responders or responders at deactivated sites.
        if location_id is not None:
            responder_ids = await responder_crud.get_ids_in_effective_location(
                db=db,
                responder_ids=responder_ids,
                location_id=location_id,
            )
        else:
            responder_ids = await responder_crud.get_reachable_ids(
                db=db, responder_ids=responder_ids
            )
        if not responder_ids:
            return 0

        notif_title, notif_message, notif_type = await self._resolve_notification_content(
            payload=payload,
            db=db,
            system_initiated=system_initiated,
        )
        dispatch = await notification_dispatch_crud.create_for_send(
            db=db,
            notif_type=notif_type,
            title=notif_title,
            message=notif_message,
            location_id=location_id,
        )

        deliveries_by_responder: dict[UUID, dict] = {
            responder_id: {
                "responder_id": responder_id,
                "subscription_id": None,
                "status": DeliveryStatus.FAILED,
                "sent_at": None,
                "error_message": "No active push subscription",
            }
            for responder_id in responder_ids
        }
        now = datetime.now(timezone.utc)
        subscriptions_to_remove: list[PushSubscription] = []

        subscriptions = await push_subscription_crud.get_by_responder_ids(
            responder_ids=responder_ids,
            db=db,
        )

        for subscription in subscriptions:
            if subscription.responder_id not in deliveries_by_responder:
                continue

            result = await self.send_push(
                subscription=subscription,
                notif_title=notif_title,
                notif_message=notif_message,
                notif_type=notif_type,
            )
            current = deliveries_by_responder[subscription.responder_id]

            if result == PushResult.GONE:
                subscriptions_to_remove.append(subscription)
                if current["status"] == DeliveryStatus.SENT:
                    continue
                deliveries_by_responder[subscription.responder_id] = {
                    "responder_id": subscription.responder_id,
                    "subscription_id": None,
                    "status": DeliveryStatus.FAILED,
                    "sent_at": None,
                    "error_message": "Push subscription expired or unsubscribed",
                }
                continue

            if result == PushResult.SENT:
                deliveries_by_responder[subscription.responder_id] = {
                    "responder_id": subscription.responder_id,
                    "subscription_id": subscription.id,
                    "status": DeliveryStatus.SENT,
                    "sent_at": now,
                    "error_message": None,
                }
                continue

            if current["status"] == DeliveryStatus.SENT:
                continue

            deliveries_by_responder[subscription.responder_id] = {
                "responder_id": subscription.responder_id,
                "subscription_id": subscription.id,
                "status": DeliveryStatus.FAILED,
                "sent_at": None,
                "error_message": "Push delivery failed",
            }

        for sub in subscriptions_to_remove:
            await db.delete(sub)

        await notification_delivery_crud.upsert_many_results(
            db=db,
            dispatch_id=dispatch.id,
            delivery_rows=list(deliveries_by_responder.values()),
        )

        # Responders actually reached — a caller reporting "sent" must not
        # count resolved-but-undelivered rows.
        return sum(
            1 for row in deliveries_by_responder.values()
            if row["status"] == DeliveryStatus.SENT
        )


    async def send_alarm(
        self,
        db: AsyncSession,
        *,
        location_id: int | None,
        title: str,
        message: str,
    ) -> int:
        """T8 — alarm tier. Same dispatch → fan-out path as any send; the
        ALARM type on the dispatch (and the push payload) is what makes the
        responder app render the NDRRMC-style takeover UI.

        Audience = the site's responders via active presence — the same
        resolver the fusion auto-notify path uses. location_id NULL =
        barangay-wide. A future autonomous trigger calls this with the site
        resolved from sensor_devices.location_id at fire time.

        Raises 400 when the resolved audience is empty — an alarm fired at
        zero push-subscribed responders must surface instead of passing
        silently. Returns the number of responders a push actually reached
        (delivery rows carry the per-responder failures for the rest).
        """
        # The site gate lives here, not in the endpoint, so a future
        # autonomous trigger inherits the FOR SHARE active-site check.
        from app.services.location_service import require_active_site

        if location_id is not None:
            await require_active_site(db=db, location_id=location_id)

        responder_ids = await responder_crud.get_responder_ids_with_push_subscription(
            db=db, location_id=location_id
        )
        if not responder_ids:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No push-subscribed responders for this site"
                    if location_id is not None
                    else "No push-subscribed responders"
                ),
            )

        payload = SendNotificationSchema(
            responder_ids=list(responder_ids),
            custom_notification=CustomNotificationPayload(
                type=NotificationType.ALARM, title=title, message=message
            ),
        )
        # Trusted internal send — same announcements-only bypass escalation and
        # the fusion path use for non-announcement types.
        return await self.send_notification_to_subscribers(
            payload=payload,
            db=db,
            system_initiated=True,
            location_id=location_id,
        )


    async def send_to_citizen_subscribers(
        self,
        db: AsyncSession,
        location_id: int,
        title: str,
        message: str,
        notif_type: NotificationType = NotificationType.CRITICAL,
    ):
        """F4 — fire-and-forget public alert to a location's citizen subscribers.

        Reuses ``send_push`` and records a single ``NotificationDispatch`` (no
        per-device delivery rows, no acknowledgements — citizens are anonymous).
        Prunes subscriptions that return 410 Gone. Returns (dispatch, sent_count).
        """
        from app.crud import citizen_subscription_crud

        dispatch = await notification_dispatch_crud.create_for_send(
            db=db, notif_type=notif_type, title=title, message=message,
            location_id=location_id,
        )
        # Capture the PK after flush, before commit expires the ORM instance.
        dispatch_id = dispatch.id

        subscriptions = await citizen_subscription_crud.get_by_location(
            db=db, location_id=location_id
        )

        sent_count = 0
        ids_to_prune: list = []
        for subscription in subscriptions:
            result = await self.send_push(
                subscription=subscription,
                notif_title=title,
                notif_message=message,
                notif_type=notif_type,
            )
            if result == PushResult.SENT:
                sent_count += 1
            elif result == PushResult.GONE:
                ids_to_prune.append(subscription.id)

        # Commit the dispatch row regardless of send outcomes (authorization happened).
        await db.commit()

        if ids_to_prune:
            await citizen_subscription_crud.delete_by_ids(db=db, ids=ids_to_prune)

        return dispatch_id, sent_count


    async def _resolve_notification_content(
        self,
        payload: SendNotificationSchema,
        db: AsyncSession,
        *,
        system_initiated: bool = False,
    ) -> tuple[str, str, NotificationType]:
        
        has_template = payload.template_id is not None
        has_custom_notification = payload.custom_notification is not None

        if has_template == has_custom_notification:
            raise HTTPException(
                status_code=400,
                detail="Provide exactly one of template_id or custom_notification",
            )

        if payload.template_id is not None:
            template: NotificationTemplate = await notification_template_crud.get(db=db, id=payload.template_id)
            if not template:
                raise HTTPException(
                    status_code=404,
                    detail="Notification template not found",
                )
            if not system_initiated and template.type != NotificationType.ANNOUNCEMENT:
                raise HTTPException(
                    status_code=400,
                    detail="Manual notifications are only supported for announcements",
                )
            return template.title, template.message, template.type

        custom_notification = payload.custom_notification
        if not system_initiated and custom_notification.type != NotificationType.ANNOUNCEMENT:
            raise HTTPException(
                status_code=400,
                detail="Custom notifications must be announcement type",
            )

        custom_title = custom_notification.title.strip()
        custom_message = custom_notification.message.strip()
        if not custom_title or not custom_message:
            raise HTTPException(
                status_code=400,
                detail="Custom notification title and message cannot be empty",
            )
        return custom_title, custom_message, custom_notification.type


    async def send_push(self, subscription: PushSubscription, notif_title: str, notif_message: str, notif_type: NotificationType) -> PushResult:

        try:
            # to_thread: pywebpush's send() is a sync requests.post — on the
            # loop it stalls every concurrent request, and on the alarm path
            # the stall happens inside the txn holding the FOR SHARE site
            # lock. timeout=10 bounds each call.
            await asyncio.to_thread(
                webpush,
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {
                        "p256dh": subscription.p256dh,
                        "auth": subscription.auth,
                    },
                },
                # "type" lets the app render tiered UI (e.g. alarm takeover)
                # straight off the push, without a feed fetch first.
                data=json.dumps({
                    "title": notif_title,
                    "message": notif_message,
                    "type": notif_type.value,
                }),
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_CLAIM_EMAIL},
                timeout=10,
            )
            return PushResult.SENT
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None) or getattr(
                getattr(e, "response", None), "status", None
            )
            if status == 410: # subscription is no longer valid
                return PushResult.GONE
            return PushResult.FAILED
        except Exception:
            # Any per-subscription failure — transport error, malformed stored
            # keys, library internals — fails this delivery row, never aborts
            # the rest of the fan-out.
            return PushResult.FAILED

notification_service = NotificationService()
