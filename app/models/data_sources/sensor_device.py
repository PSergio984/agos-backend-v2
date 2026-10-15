from sqlalchemy import Boolean, Column, ForeignKey, String, Integer, JSON, text
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import relationship
from ..base import Base
from pydantic import BaseModel, model_validator


class SensorConfig(BaseModel):
    installation_height: int
    warning_threshold: int
    critical_threshold: int

    @model_validator(mode="after")
    def validate_threshold_order(self):
        if self.warning_threshold >= self.critical_threshold:
            raise ValueError(
                "warning_threshold must be lower than critical_threshold"
            )
        return self


class SensorConfigType(TypeDecorator):
    impl = JSON
    cache_ok = True

    def process_bind_param(self, value, _):
        if isinstance(value, SensorConfig):
            return value.model_dump()
        return value

    def process_result_value(self, value, _):
        if value is not None:
            return SensorConfig(**value)
        return value


class SensorDevice(Base):
    __tablename__ = "sensor_devices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    device_name = Column(String(100), nullable=False)
    sensor_config = Column(SensorConfigType, nullable=False)
    is_active = Column(Boolean, nullable=False, server_default=text("true"))

    location = relationship("Location", back_populates="sensor_devices")
    sensor_readings = relationship("SensorReading", back_populates="sensor_device", cascade="all, delete-orphan")
