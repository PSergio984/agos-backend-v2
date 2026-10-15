from datetime import datetime
from uuid import UUID
from pydantic import BaseModel

from app.models.notification_template import NotificationType
from app.models.responder_related.notification_delivery import DeliveryStatus
from app.models.responder_related.responders import ResponderStatus


class ResponderNotificationSummary(BaseModel):
    id: UUID
    first_name: str
    last_name: str
    phone_number: str
    status: ResponderStatus
    total_notifications: int
    total_sent: int
    total_failed: int
    total_pending: int
    total_acknowledged: int
    last_notified_at: datetime | None

    class Config:
        from_attributes = True


class ResponderNotificationSummaryPaginatedResponse(BaseModel):
    items: list[ResponderNotificationSummary]
    page: int
    page_size: int
    total: int
    total_pages: int
    has_more: bool


class DeliveryLogItem(BaseModel):
    id: UUID
    status: DeliveryStatus
    sent_at: datetime | None
    error_message: str | None
    created_at: datetime
    type: NotificationType
    title: str
    message: str
    dispatched_at: datetime
    location_id: int | None = None
    is_acknowledged: bool
    acknowledged_at: datetime | None
    acknowledge_message: str | None

    class Config:
        from_attributes = True


class DeliveryLogPaginatedResponse(BaseModel):
    items: list[DeliveryLogItem]
    has_more: bool
