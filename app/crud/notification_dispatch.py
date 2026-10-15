from sqlalchemy.ext.asyncio import AsyncSession
from app.models.notification_dispatch import NotificationDispatch
from app.models.notification_template import NotificationType
from .base import CRUDBase


class CRUDNotificationDispatch(CRUDBase):

    async def create_for_send(
        self,
        db: AsyncSession,
        notif_type: NotificationType,
        title: str,
        message: str,
        location_id: int | None = None,
    ) -> NotificationDispatch:

        dispatch = NotificationDispatch(
            type=notif_type,
            title=title,
            message=message,
            location_id=location_id,
        )
        db.add(dispatch)
        await db.flush()
        return dispatch


notification_dispatch_crud = CRUDNotificationDispatch(NotificationDispatch)
