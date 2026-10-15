from pydantic import BaseModel, field_validator

class LocationCoordinate(BaseModel):
    id: int
    latitude: float
    longitude: float

class DevicePerLocation(BaseModel):
    sensor_device_id: int
    camera_device_id: int


def _validate_site_name(v: str | None) -> str | None:
    if v is None:
        return None
    cleaned = v.strip()
    if not cleaned:
        raise ValueError("Site name cannot be empty.")
    if len(cleaned) > 100:
        raise ValueError("Site name must be 100 characters or fewer.")
    return cleaned


class LocationCreate(BaseModel):
    name: str
    latitude: float
    longitude: float

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _validate_site_name(v)


class LocationUpdate(BaseModel):
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        return _validate_site_name(v)


class LocationResponse(BaseModel):
    id: int
    name: str
    latitude: float
    longitude: float
    is_active: bool

    class Config:
        from_attributes = True