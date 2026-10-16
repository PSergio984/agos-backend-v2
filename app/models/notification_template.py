import enum

from sqlalchemy import Column, DateTime, Enum, Integer, String, UUID, ForeignKey, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .base import Base


class NotificationType(enum.Enum):
    WARNING = "warning"
    CRITICAL = "critical"
    BLOCKAGE = "blockage"
    ANNOUNCEMENT = "announcement"
    MAINTENANCE = "maintenance"
    # Alarm tier (T8): NDRRMC-style takeover in the responder app. Treated as
    # critical-or-above by deliveries/escalation.
    ALARM = "alarm"


class NotificationTemplate(Base):
    __tablename__ = "notification_templates"

    id = Column(Integer, primary_key=True, index=True)
    type = Column(Enum(NotificationType), nullable=False)
    title = Column(String, nullable=False)
    message = Column(String, nullable=False)
    created_by_id = Column(UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.timezone("UTC", func.now()), nullable=False)

    __table_args__ = (
        Index(
            "uq_notification_templates_single_system_template_type",
            "type",
            unique=True,
            postgresql_where=type.in_(
                [
                    NotificationType.WARNING,
                    NotificationType.CRITICAL,
                    NotificationType.BLOCKAGE,
                ]
            ),
        ),
    )

    creator = relationship("AdminUser", back_populates="notifications")
