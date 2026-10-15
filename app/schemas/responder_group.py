from pydantic import BaseModel
from uuid import UUID

class ResponderGroupCreate(BaseModel):
    group_name: str
    member_ids: list[UUID]

class ResponderGroupItem(BaseModel):
    id: int
    group_name: str
    # NULL = global group; otherwise the site the group belongs to.
    location_id: int | None
    member_ids: list[UUID]

    class Config:
        from_attributes = True