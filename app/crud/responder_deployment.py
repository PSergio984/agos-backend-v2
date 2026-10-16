from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.crud.base import CRUDBase
from app.models import ResponderDeployment


class CRUDResponderDeployment(CRUDBase[ResponderDeployment, None, None]):

    async def create(
        self,
        db: AsyncSession,
        responder_id: UUID,
        location_id: int,
        created_by: UUID,
        starts_at: datetime | None = None,
        planned_ends_at: datetime | None = None,
    ) -> ResponderDeployment:

        # starts_at stays out of the constructor when unset so the column's
        # server_default fills it — passing None would insert a literal NULL.
        fields = {
            "responder_id": responder_id,
            "location_id": location_id,
            "created_by": created_by,
            "planned_ends_at": planned_ends_at,
        }
        if starts_at is not None:
            fields["starts_at"] = starts_at
        deployment = self.model(**fields)
        db.add(deployment)
        await db.flush()  # populate id + server-default starts_at; committed at the service layer
        return deployment


    async def get_open_for_responder(self, db: AsyncSession, responder_id: UUID) -> ResponderDeployment | None:

        """The responder's single open deployment (ends_at IS NULL), if any."""
        result = await db.execute(
            select(self.model)
            .where(
                self.model.responder_id == responder_id,
                self.model.ends_at.is_(None),
            )
            .execution_options(populate_existing=False)
        )
        return result.scalars().first()


    async def get_for_responder(
        self, db: AsyncSession, deployment_id: int, responder_id: UUID
    ) -> ResponderDeployment | None:

        """Fetch a deployment scoped to its responder — unknown/foreign ids both
        return None so the service can answer 404 for either."""
        result = await db.execute(
            select(self.model)
            .options(joinedload(self.model.location))  # eager load site name
            .where(
                self.model.id == deployment_id,
                self.model.responder_id == responder_id,
            )
            .execution_options(populate_existing=False)
        )
        return result.scalars().first()


    async def list_for_responder(self, db: AsyncSession, responder_id: UUID) -> list[ResponderDeployment]:

        result = await db.execute(
            select(self.model)
            .options(joinedload(self.model.location))  # eager load site name
            .where(self.model.responder_id == responder_id)
            .order_by(self.model.starts_at.desc())
            .execution_options(populate_existing=False)
        )
        return result.scalars().unique().all()


    async def close(self, db: AsyncSession, deployment: ResponderDeployment) -> None:

        deployment.ends_at = datetime.now(timezone.utc)
        db.add(deployment)


    async def close_open_for_location(self, db: AsyncSession, location_id: int) -> None:
        """Site deactivation cleanup — every open posting to the dead site
        closes (flush-only; the caller owns the commit)."""
        from sqlalchemy import update
        await db.execute(
            update(self.model)
            .where(
                self.model.location_id == location_id,
                self.model.ends_at.is_(None),
            )
            .values(ends_at=datetime.now(timezone.utc))
        )
        # committed at the service layer


responder_deployment_crud = CRUDResponderDeployment(ResponderDeployment)
