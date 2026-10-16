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


    async def get_for_share(self, db: AsyncSession, id: int) -> Location | None:
        """SELECT … FOR SHARE — for writers that check `is_active` and then
        write a DIFFERENT table (deployments, presence, readings, centers,
        subscriptions, responder rows). A share lock blocks `update_site`'s
        deactivation UPDATE until this txn commits without serializing
        concurrent gated writers against each other. Deadlock ordering:
        location locks must be taken before responder row locks, since
        `update_site` holds the location lock while updating responder rows.
        """
        result = await db.execute(
            select(self.model)
            .filter(self.model.id == id)
            .with_for_update(read=True)
        )
        return result.scalars().first()


    async def get_active_ids_for_share(self, db: AsyncSession, ids: list[int]) -> set[int]:
        """FOR SHARE variant of get_active_ids — bulk writers that assign
        responders to sites serialize the check against deactivation.
        Ordered so multi-row lock acquisition is deterministic."""
        result = await db.execute(
            select(self.model.id)
            .where(
                self.model.id.in_(ids), self.model.is_active.is_(True)
            )
            .order_by(self.model.id)
            .with_for_update(read=True)
        )
        return set(result.scalars().all())


    async def get_all_coordinates(self, db: AsyncSession):
        # Same active-only scope: weather/fusion work for a deactivated site
        # is wasted (and its devices no longer ingest).
        result = await db.execute(
            select(self.model.id, self.model.latitude, self.model.longitude)
            .where(self.model.is_active.is_(True))
        )
        return result.all()


location_crud = CRUDLocation(Location)
