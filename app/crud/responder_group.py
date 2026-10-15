from uuid import UUID

from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload

from app.crud.base import CRUDBase
from app.models import Group, Responder
from app.models.responder_related.responders import responder_groups


class CRUDResponderGroup(CRUDBase[Group, None, None]):
    
    async def get_by_name_and_location(self, db: AsyncSession, name: str, location_id: int | None) -> Group | None:

        """Find a group by name within one site. location_id=None matches the
        legacy global (NULL-site) group, never a site-scoped one."""
        stmt = select(self.model).where(self.model.name == name)
        if location_id is None:
            stmt = stmt.where(self.model.location_id.is_(None))
        else:
            stmt = stmt.where(self.model.location_id == location_id)
        result = await db.execute(stmt)
        return result.scalars().first()


    async def get_all_with_member_ids(self, db: AsyncSession) -> list[Group]:

        result = await db.execute(
            select(self.model)
            .options(
                load_only(self.model.name),
                selectinload(self.model.responders).load_only(Responder.id),
            )
            .order_by(self.model.id)
            .execution_options(populate_existing=False)
        )
        return result.scalars().unique().all()


    async def create_with_members(self, db: AsyncSession, name: str, responders: list[Responder]) -> Group:

        # Assign relationship while the instance is transient to avoid async lazy-load
        # of the previous collection state (MissingGreenlet).
        group = self.model(name=name, responders=responders)
        db.add(group)
        return group


    async def update_with_members(
        self,
        db: AsyncSession,
        group: Group,
        name: str,
        member_ids: list[UUID],
    ) -> tuple[Group, int, int]:
        
        desired_member_ids = set(member_ids)
        result = await db.execute(
            select(responder_groups.c.responder_id).where(
                responder_groups.c.group_id == group.id
            )
        )
        current_member_ids = set(result.scalars().all())

        to_add = desired_member_ids - current_member_ids
        to_remove = current_member_ids - desired_member_ids
        added_count = len(to_add)
        removed_count = len(to_remove)

        if to_remove:
            await db.execute(
                delete(responder_groups).where(
                    responder_groups.c.group_id == group.id,
                    responder_groups.c.responder_id.in_(to_remove),
                )
            )

        if to_add:
            await db.execute(
                insert(responder_groups),
                [
                    {"responder_id": responder_id, "group_id": group.id}
                    for responder_id in to_add
                ],
            )

        group.name = name
        db.add(group)
        return group, added_count, removed_count


    async def ensure_exists(self, db: AsyncSession, name: str, location_id: int | None = None) -> Group:

        """Get or create the named group within a site. location_id=None resolves
        the legacy global (NULL-site) group; a site id resolves that site's group."""
        existing = await self.get_by_name_and_location(db=db, name=name, location_id=location_id)
        if existing:
            return existing

        group = self.model(name=name, location_id=location_id)
        db.add(group)

        # try except to avoid race condition on unique name
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await self.get_by_name_and_location(db=db, name=name, location_id=location_id)
            if existing:
                return existing
            raise
        await db.refresh(group)
        return group


    async def add_member(
            self, 
            db: AsyncSession, 
            group_id: int, 
            responder_id: UUID, 
            *, 
            commit: bool = True) -> None:

        await db.execute(
            insert(responder_groups).values(
                responder_id=responder_id,
                group_id=group_id,
            )
        )
        if commit:
            await db.commit()


    async def delete_no_commit(self, db: AsyncSession, group: Group) -> None:

        await db.delete(group)


responder_group_crud = CRUDResponderGroup(Group)
