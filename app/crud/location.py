from app.models import Location
from app.crud.base import CRUDBase
from app.schemas import LocationCreate, LocationUpdate
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select


class CRUDLocation(CRUDBase[Location, LocationCreate, LocationUpdate]):

    async def get_default_location(self, db: AsyncSession) -> Location | None:

        result = await db.execute(
            select(self.model.id, self.model.name)
            .filter(self.model.is_active.is_(True))
            .order_by(self.model.id)
            .limit(1)
        )
        return result.mappings().first()


    async def get_by_name(self, db: AsyncSession, name: str) -> Location | None:

        result = await db.execute(
            select(self.model).filter(self.model.name == name)
        )
        return result.scalars().first()


    async def get_active_ids(self, db: AsyncSession, ids: list[int]) -> set[int]:
        """Subset of `ids` that exist AND are active — lets bulk callers
        validate client-supplied site ids in one query."""
        result = await db.execute(
            select(self.model.id).where(
                self.model.id.in_(ids), self.model.is_active.is_(True)
            )
        )
        return set(result.scalars().all())


    async def get_all_ids(self, db: AsyncSession) -> list[int]:
        # Active sites only — callers fan out alerts / rebuild caches, and a
        # deactivated site participates in neither.
        result = await db.execute(
            select(self.model.id).where(self.model.is_active.is_(True))
        )
        ids = result.scalars().all()
        return ids


    async def get_all_coordinates(self, db: AsyncSession):
        # Same active-only scope: weather/fusion work for a deactivated site
        # is wasted (and its devices no longer ingest).
        result = await db.execute(
            select(self.model.id, self.model.latitude, self.model.longitude)
            .where(self.model.is_active.is_(True))
        )
        return result.all()


location_crud = CRUDLocation(Location)
