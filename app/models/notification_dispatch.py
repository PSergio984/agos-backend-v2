from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .base import Base
from .notification_template import NotificationType


class NotificationDispatch(Base):
    __tablename__ = "notification_dispatches"

    id = Column(Integer, primary_key=True, index=True)
    type = Column(Enum(NotificationType), nullable=False)
    title = Column(String, nullable=False)
    message = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.timezone("UTC", func.now()), nullable=False)

    # "On it" claim — the responder who said they're handling this alert.
    # Claim is deliberately not completion: acknowledge stays the closing log.
    claimed_by = Column(
        UUID(as_uuid=True),
        ForeignKey("responders.id", ondelete="SET NULL"),
        nullable=True,
    )
    claimed_at = Column(DateTime(timezone=True), nullable=True)

    deliveries = relationship("NotificationDelivery", back_populates="dispatch", cascade="all, delete-orphan")
    claimed_by_responder = relationship("Responder", foreign_keys=[claimed_by])
