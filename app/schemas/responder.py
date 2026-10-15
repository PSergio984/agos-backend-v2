import re
from typing import Literal

from app.models.notification_template import NotificationType
from app.models.responder_related.responders import ResponderStatus
from pydantic import BaseModel, field_validator

NotifPreferenceKey = Literal["warning", "critical", "blockage", "announcement", "maintenance"]
from datetime import datetime
from uuid import UUID

class ResponderSendSMSRequest(BaseModel):
    responder_ids: list[UUID]
    message: str

class ResponderRegistrationRequest(BaseModel):
    phone_number: str

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, v: str) -> str:
        cleaned = re.sub(r'[\s\-()]', '', v)
        if not re.match(r'^(\+?63|0)9\d{9}$', cleaned):
            raise ValueError("Invalid Philippine phone number format. Use 09XXXXXXXXX or +639XXXXXXXXX.")
        return cleaned

class ResponderOTPVerificationCreate(BaseModel):
    responder_id: UUID
    otp_hash: str
    expires_at: datetime

class ResponderOTPVerifyRequest(BaseModel):
    responder_id: UUID
    otp: str

class ResponderOTPVerifyResponse(BaseModel):
    success: bool
    message: str
    requires_resend: bool  # True = need to request new OTP, False = can retry current OTP
    responder_token: str | None = None

class ResponderOTPDispatchResponse(BaseModel):
    dev_otp: str | None = None

class ResponderBase(BaseModel):
    first_name: str
    last_name: str
    phone_number: str

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, v: str) -> str:
        cleaned = re.sub(r'[\s\-()]', '', v)
        if not re.match(r'^(\+?63|0)9\d{9}$', cleaned):
            raise ValueError("Invalid Philippine phone number format. Use 09XXXXXXXXX or +639XXXXXXXXX.")
        return cleaned

class ResponderCreate(ResponderBase):
    # Multi-site: a responder's home site is explicit, never silently site 1.
    location_id: int


def _validate_name(v: str | None) -> str | None:
    if v is None:
        return None
    cleaned = v.strip()
    if not cleaned:
        raise ValueError("Name cannot be empty.")
    if len(cleaned) > 100:
        raise ValueError("Name must be 100 characters or fewer.")
    return cleaned


def _validate_optional_phone_number(v: str | None) -> str | None:
    if v is None:
        return None
    cleaned = re.sub(r'[\s\-()]', '', v)
    if not re.match(r'^(\+?63|0)9\d{9}$', cleaned):
        raise ValueError("Invalid Philippine phone number format. Use 09XXXXXXXXX or +639XXXXXXXXX.")
    return cleaned


class ResponderAdminUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    phone_number: str | None = None

    @field_validator("first_name", "last_name")
    @classmethod
    def _names(cls, v: str | None) -> str | None:
        return _validate_name(v)

    @field_validator("phone_number")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        return _validate_optional_phone_number(v)


class ResponderSelfUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None

    @field_validator("first_name", "last_name")
    @classmethod
    def _names(cls, v: str | None) -> str | None:
        return _validate_name(v)

class ResponderForApproval(ResponderBase):
    responder_id: UUID
    status: ResponderStatus
    dev_otp: str | None = None

class ResponderListItem(BaseModel):
    id: UUID
    first_name: str
    last_name: str
    phone_number: str
    status: ResponderStatus
    location_id: int
    has_push_subscription: bool = False

    class Config:
        from_attributes = True

class ResponderDetailsResponse(BaseModel):
    created_at: datetime
    created_by: str
    activated_at: datetime | None

    class Config:
        from_attributes = True


# Admin-managed membership: a deployment is a temporary posting to a site
# other than the responder's home site (responder_deployments table).
class ResponderDeploymentCreate(BaseModel):
    location_id: int
    planned_ends_at: datetime | None = None  # optional planned end of the posting


class ResponderDeploymentItem(BaseModel):
    id: int
    location_id: int
    location_name: str
    starts_at: datetime
    ends_at: datetime | None
    planned_ends_at: datetime | None
    created_by: UUID


# For responders app
class ResponderDetails(BaseModel):
    id: str
    first_name: str
    last_name: str
    status: ResponderStatus
    phone_number: str
    location_id: int
    location_name: str
    active_location_id: int | None
    active_location_name: str | None
    created_at: datetime
    activated_at: datetime | None


# Responder-controlled presence: which eligible site (home or open
# deployment) the responder is currently on.
class ResponderActiveSiteUpdate(BaseModel):
    location_id: int


class ResponderActiveSiteResponse(BaseModel):
    location_id: int
    active_location_id: int | None


# Admin override of presence — unlike the responder's own update this allows
# null, which clears the override back to the home-site fallback.
class ResponderAdminActiveSiteUpdate(BaseModel):
    location_id: int | None = None


class NotifPreferenceUpdateRequest(BaseModel):
    key: NotifPreferenceKey
    value: bool


class AlertListItem(BaseModel):
    id: UUID
    dispatch_id: int
    type: NotificationType
    title: str
    message: str
    timestamp: datetime
    is_acknowledged: bool
    acknowledged_at: datetime | None
    acknowledge_message: str | None
    location_id: int | None = None
    claimed_by: UUID | None = None
    claimed_by_name: str | None = None
    claimed_at: datetime | None = None


class AlertPaginatedResponse(BaseModel):
    items: list[AlertListItem]
    has_more: bool


class AcknowledgeNotifRequest(BaseModel):
    delivery_id: UUID
    responder_id: UUID
    message: str | None = None


class AcknowledgeNotifResponse(BaseModel):
    acknowledged_at: datetime
    acknowledge_message: str | None = None


class ClaimRequest(BaseModel):
    dispatch_id: int


class ClaimResponse(BaseModel):
    dispatch_id: int
    claimed_by: UUID
    claimed_by_name: str
    claimed_at: datetime