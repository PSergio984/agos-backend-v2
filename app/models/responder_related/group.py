from ..base import Base
from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.orm import relationship


DEFAULT_ACTIVE_RESPONDERS_GROUP_NAME = "All Active Responders"


class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    # NULL = legacy/global group; per-site groups carry their site's id.
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=True)

    location = relationship("Location")
    responders = relationship("Responder", secondary="responder_groups", back_populates="groups", passive_deletes=True)
