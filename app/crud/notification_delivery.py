from typing import Any
from uuid import UUID
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from app.models.responder_related.acknowledgement import Acknowledgement
from app.models.responder_related.notification_delivery import DeliveryStatus, NotificationDelivery
from app.models.notification_template import NotificationType
from .base import CRUDBase


# Tiers that escalate when unacknowledged — critical-or-above (#21/T8).
ESCALATING_TYPES = (NotificationType.CRITICAL, NotificationType.ALARM)


class CRUDNotificationDelivery(CRUDBase):

    async def upsert_many_results(self, db: AsyncSession, dispatch_id: int, delivery_rows: list[dict[str, Any]]) -> None:

        if not delivery_rows:
            return

        rows = [
            {
                "dispatch_id": dispatch_id,
                **row,
            }
            for row in delivery_rows
        ]

        stmt = insert(self.model).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_delivery_dispatch_responder",
            set_={
                "subscription_id": stmt.excluded.subscription_id,
                "status": stmt.excluded.status,
                "sent_at": stmt.excluded.sent_at,
                "error_message": stmt.excluded.error_message,
            },
        )
        await db.execute(stmt)
        await db.commit()


    async def get_alerts_per_responder(
        self,
        responder_id: UUID,
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
        notification_type: NotificationType | None = None,
        effective_location_id: int | None = None,
    ) -> tuple[list[NotificationDelivery], bool]:
        from app.models.notification_dispatch import NotificationDispatch

        stmt = (
            select(self.model)
            .options(
                joinedload(NotificationDelivery.dispatch).joinedload(
                    NotificationDispatch.claimed_by_responder
                ),
                joinedload(NotificationDelivery.acknowledgement),
            )
            .where(NotificationDelivery.responder_id == responder_id)
        )

        if notification_type is not None or effective_location_id is not None:
            stmt = stmt.join(NotificationDispatch)
        if notification_type is not None:
            stmt = stmt.where(NotificationDispatch.type == notification_type)
        if effective_location_id is not None:
            # Presence-scoped feed: the responder sees alerts for their current
            # site plus site-less announcements (NULL reaches every site).
            stmt = stmt.where(
                (NotificationDispatch.location_id == effective_location_id)
                | NotificationDispatch.location_id.is_(None)
            )

        # FAILED deliveries have sent_at=None; coalesce to created_at so they
        # don't sort first (Postgres NULLS FIRST) or crash the response schema.
        stmt = stmt.order_by(
            func.coalesce(NotificationDelivery.sent_at, NotificationDelivery.created_at).desc(),
            NotificationDelivery.created_at.desc(),
        )

        offset = (page - 1) * page_size
        stmt = stmt.offset(offset).limit(page_size + 1)

        result = await db.execute(stmt)
        items = list(result.scalars().unique().all())

        has_more = len(items) > page_size
        if has_more:
            items = items[:page_size]

        return items, has_more


    async def get_for_responder_dispatch(
        self, db: AsyncSession, dispatch_id: int, responder_id: UUID
    ) -> NotificationDelivery | None:
        """The delivery row tying this responder to this dispatch — None when
        the alert was never sent to them (used to gate claims)."""
        result = await db.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.dispatch_id == dispatch_id,
                NotificationDelivery.responder_id == responder_id,
            )
        )
        return result.scalar_one_or_none()

    async def dispatch_has_acknowledgement(
        self, db: AsyncSession, dispatch_id: int
    ) -> bool:
        """True if ANY delivery of this dispatch has been acknowledged —
        used by the claim flow: already-handled alerts can't be claimed."""
        stmt = (
            select(func.count(Acknowledgement.id))
            .join(
                NotificationDelivery,
                Acknowledgement.delivery_id == NotificationDelivery.id,
            )
            .where(NotificationDelivery.dispatch_id == dispatch_id)
        )
        result = await db.execute(stmt)
        return int(result.scalar_one()) > 0

    async def get_unread_alerts_count(
        self,
        responder_id: UUID,
        db: AsyncSession,
        effective_location_id: int | None = None,
    ) -> int:
        from app.models.notification_dispatch import NotificationDispatch

        stmt = (
            select(func.count(NotificationDelivery.id))
            .outerjoin(
                Acknowledgement,
                Acknowledgement.delivery_id == NotificationDelivery.id,
            )
            .where(
                NotificationDelivery.responder_id == responder_id,
                NotificationDelivery.status == DeliveryStatus.SENT,
                Acknowledgement.id.is_(None),
            )
        )
        if effective_location_id is not None:
            stmt = stmt.join(NotificationDispatch).where(
                (NotificationDispatch.location_id == effective_location_id)
                | NotificationDispatch.location_id.is_(None)
            )
        result = await db.execute(stmt)
        return int(result.scalar_one())


    async def get_unacknowledged_critical_past_threshold(
        self,
        db: AsyncSession,
        timeout_minutes: int,
        max_escalation: int,
    ) -> list[NotificationDelivery]:
        from datetime import datetime, timezone, timedelta
        from app.models.notification_dispatch import NotificationDispatch

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)

        stmt = (
            select(self.model)
            .options(
                joinedload(NotificationDelivery.dispatch),
            )
            .outerjoin(Acknowledgement, Acknowledgement.delivery_id == NotificationDelivery.id)
            .join(NotificationDispatch)
            .where(
                # SENT rows re-ping when unacked past the timeout; PENDING
                # rows are send-intent placeholders orphaned by a crash
                # mid-fan-out — their push may have landed, so they escalate
                # too (at-most-one extra ping beats a silently dead alarm).
                NotificationDelivery.status.in_(
                    [DeliveryStatus.SENT, DeliveryStatus.PENDING]
                ),
                Acknowledgement.id.is_(None),
                # ALARM is the critical-or-above tier: unacknowledged alarms
                # escalate like criticals (#21/T8).
                NotificationDispatch.type.in_(ESCALATING_TYPES),
                # PENDING placeholders have no sent_at — fall back to
                # created_at so a crash-orphaned row still ages past the
                # cutoff instead of sitting invisible forever.
                func.coalesce(
                    NotificationDelivery.sent_at,
                    NotificationDelivery.created_at,
                ) < cutoff,
                NotificationDelivery.escalation_count < max_escalation,
            )
            .with_for_update(of=NotificationDelivery)
        )

        result = await db.execute(stmt)
        return list(result.scalars().unique().all())

    async def increment_escalation_count(self, db: AsyncSession, delivery_id: UUID) -> None:
        from sqlalchemy import update
        stmt = (
            update(self.model)
            .where(self.model.id == delivery_id)
            .values(escalation_count=self.model.escalation_count + 1)
        )
        await db.execute(stmt)
        await db.commit()


notification_delivery_crud = CRUDNotificationDelivery(NotificationDelivery)
