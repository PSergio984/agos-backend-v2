import asyncio
import json
import logging
from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import HTTPException
from pywebpush import webpush, WebPushException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.crud import (
    notification_delivery_crud,
    notification_dispatch_crud,
    notification_template_crud,
    push_subscription_crud,
    push_token_crud,
    responder_crud,
)
from app.models.notification_template import NotificationTemplate, NotificationType
from app.models.responder_related.notification_delivery import DeliveryStatus
from app.models.responder_related.push_subscription import PushSubscription
from app.models.responder_related.push_token import PushToken
from app.schemas.subscription import (
    CustomNotificationPayload,
    SendNotificationSchema,
)

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


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
        is_escalation: bool = False,
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
            is_escalation=is_escalation,
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

        # Persist intent before any push leaves the process: a crash between
        # send and commit must not leave a responder holding an alarm with no
        # durable dispatch/delivery to claim or acknowledge (PR #25 review).
        # Placeholders are PENDING, not FAILED — a stuck-in-flight row is
        # distinguishable from a real failure and escalation still picks it
        # up once it's stale. The final upsert rewrites them with real
        # outcomes (the dict defaults above stay FAILED for no-subscription
        # responders). This commit also ends the transaction, releasing the
        # FOR SHARE site lock before the push loop instead of holding it
        # across N pushes.
        await notification_delivery_crud.upsert_many_results(
            db=db,
            dispatch_id=dispatch.id,
            delivery_rows=[
                {
                    "responder_id": responder_id,
                    "subscription_id": None,
                    "status": DeliveryStatus.PENDING,
                    "sent_at": None,
                    "error_message": None,
                }
                for responder_id in responder_ids
            ],
        )

        now = datetime.now(timezone.utc)
        subscriptions_to_remove: list[PushSubscription] = []
        tokens_to_remove: list[PushToken] = []

        subscriptions = await push_subscription_crud.get_by_responder_ids(
            responder_ids=responder_ids,
            db=db,
        )
        # Expo tokens are the same fan-out, second device table: a responder
        # may carry VAPID subs (PWA) and Expo tokens (native app) at once.
        push_tokens = await push_token_crud.get_by_responder_ids(
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
            if result == PushResult.GONE:
                subscriptions_to_remove.append(subscription)
            self._apply_delivery_result(
                deliveries_by_responder,
                subscription.responder_id,
                result=result,
                subscription_id=subscription.id,
                gone_error="Push subscription expired or unsubscribed",
                now=now,
            )

        expo_tokens = [
            push_token
            for push_token in push_tokens
            if push_token.responder_id in deliveries_by_responder
        ]
        expo_results: list[PushResult] = []
        if expo_tokens:
            # One client for the whole fan-out, sends concurrent — a slow
            # Expo response must not stall every responder behind it.
            async with httpx.AsyncClient(timeout=10) as client:
                expo_results = await asyncio.gather(
                    *(
                        self.send_expo_push(
                            client=client,
                            push_token=push_token,
                            notif_title=notif_title,
                            notif_message=notif_message,
                            notif_type=notif_type,
                        )
                        for push_token in expo_tokens
                    )
                )
        for push_token, result in zip(expo_tokens, expo_results):
            if result == PushResult.GONE:
                tokens_to_remove.append(push_token)
            self._apply_delivery_result(
                deliveries_by_responder,
                push_token.responder_id,
                result=result,
                subscription_id=None,
                gone_error="Push token expired or device unregistered",
                now=now,
            )

        for sub in subscriptions_to_remove:
            await db.delete(sub)
        for push_token in tokens_to_remove:
            await db.delete(push_token)

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
            # the rest of the fan-out. Log so a systemic failure (bad VAPID
            # config) isn't indistinguishable from stale endpoints.
            logger.exception("Web push failed for endpoint %s", subscription.endpoint)
            return PushResult.FAILED


    @staticmethod
    def _apply_delivery_result(
        deliveries_by_responder: dict[UUID, dict],
        responder_id: UUID,
        *,
        result: PushResult,
        subscription_id: UUID | None,
        gone_error: str,
        now: datetime,
    ) -> None:
        """Fold one device's send outcome into the responder's delivery row.

        A responder can carry several devices (VAPID subs + Expo tokens):
        SENT on any device is terminal for the row — a sibling device's
        GONE/FAILED never downgrades it. GONE rows keep subscription_id NULL
        since the device row is about to be deleted."""
        if result == PushResult.SENT:
            deliveries_by_responder[responder_id] = {
                "responder_id": responder_id,
                "subscription_id": subscription_id,
                "status": DeliveryStatus.SENT,
                "sent_at": now,
                "error_message": None,
            }
            return

        if deliveries_by_responder[responder_id]["status"] == DeliveryStatus.SENT:
            return

        deliveries_by_responder[responder_id] = {
            "responder_id": responder_id,
            "subscription_id": None if result == PushResult.GONE else subscription_id,
            "status": DeliveryStatus.FAILED,
            "sent_at": None,
            "error_message": gone_error if result == PushResult.GONE else "Push delivery failed",
        }


    async def send_expo_push(self, client: httpx.AsyncClient, push_token: PushToken, notif_title: str, notif_message: str, notif_type: NotificationType) -> PushResult:

        """Expo Push Service sender — the native app's counterpart of
        send_push. One push per request so the ticket maps 1:1 onto
        PushResult: status "ok" → SENT, a DeviceNotRegistered ticket error →
        GONE (the token gets pruned), everything else → FAILED. Callers share
        one client across the fan-out."""
        # ALARM rides the app's "alarm" channel with the bundled siren — the
        # channelId/sound/interruptionLevel triple is what makes the takeover
        # UI audible through focus/DND. Other tiers stay on the default
        # channel/sound and carry no interruptionLevel.
        is_alarm = notif_type == NotificationType.ALARM
        payload = {
            "to": push_token.token,
            "title": notif_title,
            "body": notif_message,
            "sound": "siren.wav" if is_alarm else "default",
            "channelId": "alarm" if is_alarm else "default",
            "priority": "high",
            "ttl": 3600,
            # "type" mirrors the webpush data payload — the app renders the
            # tiered UI straight off the push.
            "data": {"type": notif_type.value},
        }
        if is_alarm:
            payload["interruptionLevel"] = "timeSensitive"

        try:
            response = await client.post(EXPO_PUSH_URL, json=payload)
            if response.status_code != 200:
                return PushResult.FAILED
            ticket = response.json().get("data") or {}
            if ticket.get("status") == "ok":
                return PushResult.SENT
            if (ticket.get("details") or {}).get("error") == "DeviceNotRegistered":
                return PushResult.GONE
            return PushResult.FAILED
        except Exception:
            # Same per-device boundary as send_push: transport errors and
            # malformed tickets fail this delivery row, never the fan-out.
            logger.exception("Expo push failed for token %s", push_token.token)
            return PushResult.FAILED

notification_service = NotificationService()
