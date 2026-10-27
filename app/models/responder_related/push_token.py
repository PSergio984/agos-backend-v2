from sqlalchemy import Column, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid
from ..base import Base


class PushToken(Base):
    """Expo push tokens for the native responder app — a separate table from
    push_subscriptions so a responder can carry both VAPID web-push subs and
    Expo tokens (#33)."""
    __tablename__ = "push_tokens"
    # A token identifies the app install, not the responder — it must be
    # unique on its own so a device changing hands can't fan out the old
    # responder's alarms to the new owner's phone.
    __table_args__ = (UniqueConstraint("token", name="uq_push_token_token"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    responder_id = Column(UUID(as_uuid=True), ForeignKey("responders.id", ondelete="CASCADE"), nullable=False)
    token = Column(Text, nullable=False)
    platform = Column(Text, nullable=False, default="expo")
    created_at = Column(DateTime(timezone=True), server_default=func.timezone("UTC", func.now()), nullable=False)

    responder = relationship("Responder", back_populates="push_tokens")
