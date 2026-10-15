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
from app.schemas.subscription import SendNotificationSchema


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
    ) -> None:

        responder_ids = list(dict.fromkeys(payload.responder_ids))
        if not responder_ids:
            return

        # Site-scoped dispatch: deliver only to responders whose effective site
        # (active_location_id ?? location_id) matches. A NULL location_id is a
        # global announcement — the given ids are delivered regardless of site.
        if location_id is not None:
            responder_ids = await responder_crud.get_ids_in_effective_location(
                db=db,
                responder_ids=responder_ids,
                location_id=location_id,
            )
            if not responder_ids:
                return

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


    async def send_push(self, subscription: PushSubscription, notif_title: str, notif_message: str) -> PushResult:
        
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {
                        "p256dh": subscription.p256dh,
                        "auth": subscription.auth,
                    },
                },
                data=json.dumps({
                    "title": notif_title,
                    "message": notif_message,
                }),
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_CLAIM_EMAIL},
            )
            return PushResult.SENT
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None) or getattr(
                getattr(e, "response", None), "status", None
            )
            if status == 410: # subscription is no longer valid
                return PushResult.GONE
            return PushResult.FAILED

notification_service = NotificationService()
