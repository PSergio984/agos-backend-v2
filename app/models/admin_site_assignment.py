from sqlalchemy import Column, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from .base import Base


class AdminSiteAssignment(Base):
    """Dormant admin↔site join — provision for future site-scoped admin
    assignment (decision record #20). Nothing reads or writes this table yet:
    no schemas, no service, no endpoints. See docs/adr/0001."""

    __tablename__ = "admin_site_assignments"
    __table_args__ = (
        UniqueConstraint("admin_user_id", "location_id", name="uq_admin_site_assignment"),
    )

    id = Column(Integer, primary_key=True, index=True)
    admin_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    location_id = Column(
        Integer,
        ForeignKey("locations.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.timezone("UTC", func.now()), nullable=False)
