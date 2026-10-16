from sqlalchemy import Boolean, Column, Integer, String, Float, text
from sqlalchemy.orm import relationship
from ..base import Base


class Location(Base):
    __tablename__ = 'locations'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    is_active = Column(Boolean, nullable=False, server_default=text("true"))

    weather_conditions = relationship("Weather", back_populates="location", cascade="all, delete-orphan")
    sensor_devices = relationship("SensorDevice", back_populates="location", cascade="all, delete-orphan")
    camera_devices = relationship("CameraDevice", back_populates="location", cascade="all, delete-orphan")
    daily_summaries = relationship("DailySummary", back_populates="location", cascade="all, delete-orphan")
    responders = relationship("Responder", back_populates="location", foreign_keys="Responder.location_id")