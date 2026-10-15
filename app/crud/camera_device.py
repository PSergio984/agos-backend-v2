from app.crud.base import CRUDBase
from app.models import CameraDevice
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select


class CRUDCameraDevice(CRUDBase[CameraDevice, None, None]):

    async def get_all(self, db: AsyncSession, location_id: int | None = None) -> list[CameraDevice]:

        query = select(self.model)
        if location_id is not None:
            query = query.filter(self.model.location_id == location_id)
        result = await db.execute(query)
        return result.scalars().all()


    async def get_by_name_and_location(self, db: AsyncSession, device_name: str, location_id: int) -> CameraDevice | None:

        result = await db.execute(
            select(self.model)
            .filter(self.model.device_name == device_name)
            .filter(self.model.location_id == location_id)
        )
        return result.scalars().first()


    async def get_default_camera_by_location(self, db: AsyncSession, location_id: int) -> CameraDevice | None:
        
        result = await db.execute(
            select(self.model.id, self.model.device_name)
            .filter(self.model.location_id == location_id)
            .limit(1)
        )
        return result.mappings().first()


    async def get_id_by_location(self, db: AsyncSession, location_id: int) -> int | None:

        result = await db.execute(
            select(self.model.id)
            .filter(self.model.location_id == location_id)
            .limit(1)
        )
        camera_device = result.scalars().first()
        return camera_device
    

camera_device_crud = CRUDCameraDevice(CameraDevice)