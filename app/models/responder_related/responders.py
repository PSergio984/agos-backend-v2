import uuid
from sqlalchemy import Column, Table, String, UUID, DateTime, ForeignKey, Enum, Integer, JSON, text
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import enum
from ..base import Base
from pydantic import BaseModel
from sqlalchemy.types import TypeDecorator


class ResponderStatus(enum.Enum):
    PENDING = 'pending'
    ACTIVE = 'active'


class NotificationPreference(BaseModel):
    warning: bool
    critical: bool
    blockage: bool
    announcement: bool


class NotificationPreferenceType(TypeDecorator):
    impl = JSON
    cache_ok = True

    def process_bind_param(self, value, _):
        if isinstance(value, NotificationPreference):
            return value.model_dump()
        return value
    
    def process_result_value(self, value, _):
        if value is not None:
            return NotificationPreference(**value)
        return value


responder_groups = Table(
    'responder_groups',
    Base.metadata,
    Column('responder_id', UUID(as_uuid=True), ForeignKey('responders.id', ondelete='CASCADE'), primary_key=True),
    Column('group_id', Integer, ForeignKey('groups.id', ondelete='CASCADE'), primary_key=True)
)


class Responder(Base):
    __tablename__ = "responders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_number = Column(String(20), unique=True, nullable=False)
    first_name = Column(String(50), nullable=False)
    last_name = Column(String(50), nullable=False)
    status = Column(Enum(ResponderStatus), nullable=False, default=ResponderStatus.PENDING)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    active_location_id = Column(Integer, ForeignKey("locations.id"), nullable=True)
    activated_at = Column(DateTime(timezone=True), nullable=True)
    notif_preferences = Column(
        NotificationPreferenceType,
        nullable=False,
        default=lambda: NotificationPreference(warning=True, critical=True, blockage=True, announcement=True),
        server_default=text("'{\"warning\": true, \"critical\": true, \"blockage\": true, \"announcement\": true}'::json"),
    )
    created_at = Column(DateTime(timezone=True), server_default=func.timezone('UTC', func.now()))
    created_by = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=False)

    admin_user = relationship("AdminUser", back_populates="responders_created")
    location = relationship("Location", back_populates="responders", foreign_keys=[location_id])
    active_location = relationship("Location", foreign_keys=[active_location_id])
    deployments = relationship("ResponderDeployment", back_populates="responder", cascade="all, delete-orphan")
    groups = relationship("Group", secondary=responder_groups, back_populates="responders", passive_deletes=True)
    otp_verification = relationship("RespondersOTPVerification", back_populates="responder", uselist=False)
    acknowledgements = relationship("Acknowledgement", back_populates="responder", cascade="all, delete-orphan")
    deliveries = relationship("NotificationDelivery", back_populates="responder", cascade="all, delete-orphan")
    push_subscriptions = relationship("PushSubscription", back_populates="responder", cascade="all, delete-orphan")
