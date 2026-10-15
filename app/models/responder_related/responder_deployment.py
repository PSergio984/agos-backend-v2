from sqlalchemy import Column, DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..base import Base


class ResponderDeployment(Base):
    """A temporary/additional site posting: the responder may select this site
    as their active site while the deployment is open (ends_at IS NULL)."""

    __tablename__ = "responder_deployments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    responder_id = Column(
        UUID(as_uuid=True),
        ForeignKey("responders.id", ondelete="CASCADE"),
        nullable=False,
    )
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    starts_at = Column(
        DateTime(timezone=True),
        server_default=func.timezone("UTC", func.now()),
        nullable=False,
    )
    ends_at = Column(DateTime(timezone=True), nullable=True)
    planned_ends_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=False)

    responder = relationship("Responder", back_populates="deployments")
    location = relationship("Location")
    creator = relationship("AdminUser")
