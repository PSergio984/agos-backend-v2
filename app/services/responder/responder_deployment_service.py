"""Responder deployments: admin-managed membership — who MAY be posted to a
site other than their home site. Presence (which eligible site the responder
is actually on) is responder-controlled via the active-site endpoint in
responder_app_service."""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser
from app.crud import (
    admin_audit_log_crud,
    location_crud,
    responder_crud,
    responder_deployment_crud,
)
from app.models.responder_related.responder_deployment import ResponderDeployment
from app.schemas import (
    AdminAuditLogCreate,
    ResponderDeploymentCreate,
    ResponderDeploymentItem,
)


def _to_item(deployment: ResponderDeployment, location_name: str) -> ResponderDeploymentItem:
    return ResponderDeploymentItem(
        id=deployment.id,
        location_id=deployment.location_id,
        location_name=location_name,
        starts_at=deployment.starts_at,
        ends_at=deployment.ends_at,
        created_by=deployment.created_by,
    )


class ResponderDeploymentService:

    async def list_deployments(self, db: AsyncSession, responder_id: UUID) -> list[ResponderDeploymentItem]:
        responder = await responder_crud.get(db=db, id=responder_id)
        if responder is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Responder not found.",
            )
        deployments = await responder_deployment_crud.list_for_responder(db=db, responder_id=responder_id)
        return [_to_item(d, location_name=d.location.name) for d in deployments]


    async def open_deployment(
        self,
        db: AsyncSession,
        responder_id: UUID,
        payload: ResponderDeploymentCreate,
        current_user: CurrentUser,
    ) -> ResponderDeploymentItem:
        # Lock the responder row so open/close/active-site can't interleave —
        # "at most one open deployment" is enforced here, not by a constraint.
        responder = await responder_crud.get_with_lock(db=db, id=responder_id)
        if responder is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Responder not found.",
            )
        location = await location_crud.get(db=db, id=payload.location_id)
        if location is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Site not found.",
            )
        if payload.location_id == responder.location_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A responder's home site is not a deployment.",
            )

        open_deployment = await responder_deployment_crud.get_open_for_responder(
            db=db, responder_id=responder_id
        )
        if open_deployment is not None:
            if open_deployment.location_id == payload.location_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="An open deployment to this site already exists.",
                )
            # Supersede: the previous posting ends before the new one opens. If
            # the responder's active site pointed at it, fall back to home.
            await responder_deployment_crud.close(db=db, deployment=open_deployment)
            if responder.active_location_id == open_deployment.location_id:
                responder.active_location_id = None
                db.add(responder)

        deployment = await responder_deployment_crud.create(
            db=db,
            responder_id=responder_id,
            location_id=payload.location_id,
            created_by=current_user.id,
        )
        await admin_audit_log_crud.create_only_no_commit(
            db=db,
            obj_in=AdminAuditLogCreate(
                admin_user_id=current_user.id,
                action=(
                    f"Deployed responder '{responder.first_name} {responder.last_name}' "
                    f"to site '{location.name}'"
                ),
            ),
        )
        await db.commit()
        return _to_item(deployment, location_name=location.name)


    async def close_deployment(
        self,
        db: AsyncSession,
        responder_id: UUID,
        deployment_id: int,
        current_user: CurrentUser,
    ) -> ResponderDeploymentItem:
        responder = await responder_crud.get_with_lock(db=db, id=responder_id)
        if responder is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Responder not found.",
            )
        deployment = await responder_deployment_crud.get_for_responder(
            db=db, deployment_id=deployment_id, responder_id=responder_id
        )
        if deployment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Deployment not found.",
            )
        if deployment.ends_at is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Deployment is already closed.",
            )

        await responder_deployment_crud.close(db=db, deployment=deployment)
        # An ended deployment can no longer be the responder's active site.
        if responder.active_location_id == deployment.location_id:
            responder.active_location_id = None
            db.add(responder)

        await admin_audit_log_crud.create_only_no_commit(
            db=db,
            obj_in=AdminAuditLogCreate(
                admin_user_id=current_user.id,
                action=(
                    f"Closed deployment of responder '{responder.first_name} {responder.last_name}' "
                    f"to site '{deployment.location.name}'"
                ),
            ),
        )
        await db.commit()
        return _to_item(deployment, location_name=deployment.location.name)


responder_deployment_service = ResponderDeploymentService()
