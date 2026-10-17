from sqlalchemy import Column, DateTime, Enum, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum
import uuid
from ..base import Base

class DeliveryStatus(enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


"""What's actually being sent."""
class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("dispatch_id", "responder_id", name="uq_delivery_dispatch_responder"),
        UniqueConstraint("id", "responder_id", name="uq_delivery_id_responder"),
        Index("ix_notification_deliveries_responder_status", "responder_id", "status"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dispatch_id = Column(Integer, ForeignKey("notification_dispatches.id", ondelete="CASCADE"), nullable=False)
    responder_id = Column(UUID(as_uuid=True), ForeignKey("responders.id", ondelete="CASCADE"), nullable=False)
    subscription_id = Column(UUID(as_uuid=True), ForeignKey("push_subscriptions.id", ondelete="SET NULL"), nullable=True)
    status = Column(Enum(DeliveryStatus), nullable=False, default=DeliveryStatus.PENDING)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.timezone("UTC", func.now()), nullable=False)
    escalation_count = Column(Integer, nullable=False, default=0, server_default="0")
    # Set when the escalation job claims the delivery for a re-ping — drives
    # both the timeout spacing and the "already claimed" dedup (#26). NULL =
    # never escalated (falls back to sent_at for eligibility).
    last_escalated_at = Column(DateTime(timezone=True), nullable=True)

    dispatch = relationship("NotificationDispatch", back_populates="deliveries")
    responder = relationship("Responder", back_populates="deliveries")
    subscription = relationship("PushSubscription", back_populates="deliveries")
    acknowledgement = relationship(
        "Acknowledgement",
        back_populates="delivery",
        uselist=False,
        cascade="all, delete-orphan",
        foreign_keys="Acknowledgement.delivery_id",
    )
