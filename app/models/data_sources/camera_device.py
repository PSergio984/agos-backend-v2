from sqlalchemy import Boolean, Column, ForeignKey, String, Integer, text
from sqlalchemy.orm import relationship
from ..base import Base


class CameraDevice(Base):
    __tablename__ = "camera_devices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    device_name = Column(String(100), nullable=False)
    is_active = Column(Boolean, nullable=False, server_default=text("true"))

    location = relationship("Location", back_populates="camera_devices")
    model_readings = relationship("ModelReadings", back_populates="camera_device", cascade="all, delete-orphan")