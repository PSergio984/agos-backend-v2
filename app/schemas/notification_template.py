from pydantic import BaseModel, field_validator
from app.models.notification_template import NotificationType

class NotificationTemplateResponse(BaseModel):
    id: int
    type: NotificationType
    title: str
    message: str


class CreateNotificationTemplateRequest(BaseModel):
    type: NotificationType
    title: str
    message: str

    @field_validator("type")
    @classmethod
    def _alarm_is_system_reserved(cls, v: NotificationType) -> NotificationType:
        # ALARM has no template path yet — until the autonomous trigger lands,
        # an admin-authored alarm template would be undeletable and would be
        # picked up by _send_system_template.
        if v == NotificationType.ALARM:
            raise ValueError("Alarm is a system-reserved tier and cannot be used for templates")
        return v