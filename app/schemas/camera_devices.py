from pydantic import BaseModel


# == Admin device registry (multi-site) == #

class CameraDeviceCreate(BaseModel):
    device_name: str
    location_id: int
    stream_url: str | None = None


class CameraDeviceUpdate(BaseModel):
    device_name: str | None = None
    location_id: int | None = None
    stream_url: str | None = None
    is_active: bool | None = None


class CameraDeviceItem(BaseModel):
    id: int
    device_name: str
    location_id: int
    stream_url: str | None
    is_active: bool

    class Config:
        from_attributes = True
