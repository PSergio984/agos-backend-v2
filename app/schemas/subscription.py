from uuid import UUID

from pydantic import BaseModel

from app.models.notification_template import NotificationType

class SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str

class SubscriptionSchema(BaseModel):
    endpoint: str
    keys: SubscriptionKeys
    responder_id: UUID


class CustomNotificationPayload(BaseModel):
    title: str
    message: str
    type: NotificationType = NotificationType.ANNOUNCEMENT


class SendNotificationSchema(BaseModel):
    responder_ids: list[UUID]
    template_id: int | None = None
    custom_notification: CustomNotificationPayload | None = None


class SendAlarmSchema(BaseModel):
    """Admin-triggered alarm (T8): same dispatch pipeline, ALARM tier.
    location_id NULL = barangay-wide."""
    title: str
    message: str
    location_id: int | None = None
