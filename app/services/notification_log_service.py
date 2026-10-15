from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.notification_log import notification_log_crud
from app.models.notification_template import NotificationType
from app.schemas.notification_log import (
    DeliveryLogItem,
    DeliveryLogPaginatedResponse,
    ResponderNotificationSummary,
    ResponderNotificationSummaryPaginatedResponse,
)


class NotificationLogService:

    async def get_responders_summary(
        self,
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
        search: str | None = None,
    ) -> ResponderNotificationSummaryPaginatedResponse:
        rows, total = (
            await notification_log_crud.get_responders_with_notification_summary(
                db=db,
                page=page,
                page_size=page_size,
                search=search,
            )
        )
        items = [
            ResponderNotificationSummary(
                id=row.Responder.id,
                first_name=row.Responder.first_name,
                last_name=row.Responder.last_name,
                phone_number=row.Responder.phone_number,
                status=row.Responder.status,
                total_notifications=row.total_notifications,
                total_sent=row.total_sent,
                total_failed=row.total_failed,
                total_pending=row.total_pending,
                total_acknowledged=row.total_acknowledged,
                last_notified_at=row.last_notified_at,
            )
            for row in rows
        ]
        total_pages = (total + page_size - 1) // page_size
        return ResponderNotificationSummaryPaginatedResponse(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            has_more=page < total_pages,
        )


    async def get_responder_deliveries(
        self,
        db: AsyncSession,
        responder_id: UUID,
        page: int = 1,
        page_size: int = 10,
        notification_type: NotificationType | None = None,
    ) -> DeliveryLogPaginatedResponse:

        items, has_more = await notification_log_crud.get_deliveries_for_responder(
            db=db,
            responder_id=responder_id,
            page=page,
            page_size=page_size,
            notification_type=notification_type,
        )

        return DeliveryLogPaginatedResponse(
            items=[
                DeliveryLogItem(
                    id=d.id,
                    status=d.status,
                    sent_at=d.sent_at,
                    error_message=d.error_message,
                    created_at=d.created_at,
                    type=d.dispatch.type,
                    title=d.dispatch.title,
                    message=d.dispatch.message,
                    dispatched_at=d.dispatch.created_at,
                    location_id=d.dispatch.location_id,
                    is_acknowledged=d.acknowledgement is not None,
                    acknowledged_at=d.acknowledgement.acknowledged_at if d.acknowledgement else None,
                    acknowledge_message=d.acknowledgement.message if d.acknowledgement else None,
                )
                for d in items
            ],
            has_more=has_more,
        )


    async def get_analytics(
        self,
        db: AsyncSession,
        date_from: str | None = None,
        date_to: str | None = None,
    ):
        from app.schemas.notification_analytics import (
            NotificationAnalyticsResponse,
            TypeBreakdown,
            ResponderRanking,
        )

        data = await notification_log_crud.get_analytics(db=db, date_from=date_from, date_to=date_to)

        overall = data["overall"]
        total_sent = overall.total_sent or 0
        total_acknowledged = overall.total_acknowledged or 0

        return NotificationAnalyticsResponse(
            total_sent=total_sent,
            total_acknowledged=total_acknowledged,
            acknowledgement_rate=round(total_acknowledged / total_sent, 4) if total_sent > 0 else 0,
            avg_response_time_seconds=round(overall.avg_response_seconds, 1) if overall.avg_response_seconds else None,
            per_type_breakdown=[
                TypeBreakdown(
                    type=row.notif_type.value if hasattr(row.notif_type, 'value') else str(row.notif_type),
                    total=row.total,
                    acknowledged=row.acknowledged,
                    avg_response_time_seconds=round(row.avg_response_seconds, 1) if row.avg_response_seconds else None,
                )
                for row in data["type_rows"]
            ],
            top_responders=[
                ResponderRanking(
                    responder_id=str(row.id),
                    first_name=row.first_name,
                    last_name=row.last_name,
                    avg_response_time_seconds=round(row.avg_response_seconds, 1),
                    total_acknowledged=row.total_acknowledged,
                )
                for row in data["top_rows"]
            ],
        )

    async def get_deliveries_for_export(
        self,
        db: AsyncSession,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 10000,
    ) -> list[dict]:
        deliveries = await notification_log_crud.get_deliveries_for_export(
            db=db, date_from=date_from, date_to=date_to, limit=limit
        )

        return [
            {
                "responder_name": f"{d.responder.first_name} {d.responder.last_name}" if d.responder else "Unknown",
                "responder_phone": d.responder.phone_number if d.responder else "",
                "type": d.dispatch.type.value if hasattr(d.dispatch.type, 'value') else str(d.dispatch.type),
                "title": d.dispatch.title,
                "message": d.dispatch.message,
                "status": d.status.value if hasattr(d.status, 'value') else str(d.status),
                "sent_at": str(d.sent_at) if d.sent_at else "",
                "is_acknowledged": d.acknowledgement is not None,
                "acknowledged_at": str(d.acknowledgement.acknowledged_at) if d.acknowledgement else "",
                "acknowledge_message": d.acknowledgement.message if d.acknowledgement else "",
            }
            for d in deliveries
        ]


notification_log_service = NotificationLogService()
