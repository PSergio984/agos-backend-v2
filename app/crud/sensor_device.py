from sqlalchemy.ext.asyncio import AsyncSession
from app.models import SensorDevice, SensorConfig
from app.schemas import SensorDeviceResponse
from app.crud.base import CRUDBase
from sqlalchemy import select
from sqlalchemy.orm import joinedload


class CRUDSensorDevice(CRUDBase[SensorDevice, None, None]):

    async def get_all(self, db: AsyncSession, location_id: int | None = None) -> list[SensorDevice]:

        query = select(self.model)
        if location_id is not None:
            query = query.filter(self.model.location_id == location_id)
        result = await db.execute(query)
        return result.scalars().all()


    async def get_entity(self, db: AsyncSession, id: int) -> SensorDevice | None:
        """ORM row — `get` above returns the SensorDeviceResponse schema."""

        result = await db.execute(
            select(self.model).filter(self.model.id == id)
        )
        return result.scalars().first()


    async def get_by_name_and_location(self, db: AsyncSession, device_name: str, location_id: int) -> SensorDevice | None:

        result = await db.execute(
            select(self.model)
            .filter(self.model.device_name == device_name)
            .filter(self.model.location_id == location_id)
        )
        return result.scalars().first()


    async def get_default_sensor_by_location(self, db: AsyncSession, location_id: int) -> SensorDevice | None:

        result = await db.execute(
            select(self.model.id, self.model.device_name)
            .filter(self.model.location_id == location_id)
            .limit(1)
        )
        return result.mappings().first()


    async def get_sensor_device_name(self, db: AsyncSession, sensor_device_id: int) -> str:

        result = await db.execute(
            select(self.model.device_name)
            .filter(self.model.id == sensor_device_id)
            .limit(1)
        )
        return result.scalars().first()


    async def get(self, db: AsyncSession, id: int) ->  SensorDeviceResponse | None:

        sensor_devices = await db.execute(
            select(self.model)
            .filter(self.model.id == id)
            .options(joinedload(self.model.location))        
            .execution_options(populate_existing=False)
        )
        sensor_device = sensor_devices.scalars().first()

        if sensor_device:
            return SensorDeviceResponse(
                device_name=sensor_device.device_name,
                location_name=sensor_device.location.name
            )
        
        return None


    async def get_device_config(self, db: AsyncSession, sensor_device_id: int) -> SensorConfig:
        
        sensor_device = await db.execute(
            select(self.model.sensor_config)
            .filter(self.model.id == sensor_device_id)
        )
        return sensor_device.scalars().first()


    async def get_device_config_by_location(self, db: AsyncSession, location_id: int) -> SensorConfig | None:

        sensor_device = await db.execute(
            select(self.model.sensor_config)
            .filter(self.model.location_id == location_id)
            .limit(1)
        )
        return sensor_device.scalars().first()


    async def get_id_by_location(self, db: AsyncSession, location_id: int) -> int | None:

        result = await db.execute(
            select(self.model.id)
            .filter(self.model.location_id == location_id)
            .limit(1)
        )
        return result.scalars().first()


    async def update_config(self, db: AsyncSession, sensor_device_id: int, config: SensorConfig) -> SensorConfig:

        result = await db.execute(
            select(self.model).filter(self.model.id == sensor_device_id)
        )
        device = result.scalars().first()

        if device is None:
            return None

        device.sensor_config = config
        await db.commit()
        await db.refresh(device)
        return device.sensor_config


sensor_device_crud = CRUDSensorDevice(SensorDevice)