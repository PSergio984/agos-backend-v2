from ..base import Base
from sqlalchemy import Column, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship


DEFAULT_ACTIVE_RESPONDERS_GROUP_NAME = "All Active Responders"


class Group(Base):
    __tablename__ = "groups"
    # NULLS NOT DISTINCT so legacy/global (NULL-site) groups keep the old
    # groups_name_key semantics — same name allowed per site, never twice
    # within one site or among NULL-site groups.
    __table_args__ = (
        UniqueConstraint(
            "name",
            "location_id",
            name="uq_groups_name_location",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    # NULL = legacy/global group; per-site groups carry their site's id.
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=True)

    location = relationship("Location")
    responders = relationship("Responder", secondary="responder_groups", back_populates="groups", passive_deletes=True)
