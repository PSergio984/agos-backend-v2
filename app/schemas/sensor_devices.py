from pydantic import BaseModel

from app.models import SensorConfig

class SensorDeviceBase(BaseModel):
    device_name: str
    location_id: int
class SensorDeviceResponse(BaseModel):
    device_name: str
    location_name: str

    class Config:
        from_attributes = True

class SensorDeviceStatusResponse(BaseModel):
    device_name: str
    location_name: str
    connection: str
    last_updated: str | None
    signal: str | None

    class Config:
        from_attributes = True


# == Admin device registry (multi-site) == #

class SensorDeviceCreate(BaseModel):
    device_name: str
    location_id: int
    sensor_config: SensorConfig


class SensorDeviceUpdate(BaseModel):
    device_name: str | None = None
    location_id: int | None = None
    sensor_config: SensorConfig | None = None
    is_active: bool | None = None


class SensorDeviceItem(BaseModel):
    id: int
    device_name: str
    location_id: int
    sensor_config: SensorConfig
    is_active: bool

    class Config:
        from_attributes = True