from app.models import Location
from app.crud.base import CRUDBase
from app.schemas import LocationCreate, LocationUpdate
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select


class CRUDLocation(CRUDBase[Location, LocationCreate, LocationUpdate]):

    async def get_default_location(self, db: AsyncSession) -> Location | None:

        result = await db.execute(
            select(self.model.id, self.model.name).limit(1)
        )
        return result.mappings().first()


    async def get_by_name(self, db: AsyncSession, name: str) -> Location | None:

        result = await db.execute(
            select(self.model).filter(self.model.name == name)
        )
        return result.scalars().first()


    async def get_all_ids(self, db: AsyncSession) -> list[int]:

        result = await db.execute(
            select(self.model.id)
        )
        ids = result.scalars().all()
        return ids


    async def get_all_coordinates(self, db: AsyncSession):

        result = await db.execute(
            select(self.model.id, self.model.latitude, self.model.longitude)
        )
        return result.all()


location_crud = CRUDLocation(Location)
