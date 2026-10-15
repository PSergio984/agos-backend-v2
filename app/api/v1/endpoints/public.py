from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.rate_limiter import limiter
from app.crud import evacuation_center_crud, location_crud
from app.schemas import (
    EvacuationRouteRequest,
    EvacuationRouteResponse,
    PublicStatusResponse,
)
from app.services import core_service, evacuation_route_service


router = APIRouter(prefix="/public", tags=["public"])


@router.get("/status", response_model=PublicStatusResponse)
async def get_public_status(location_id: int) -> PublicStatusResponse:
    """Latest citizen-safe status snapshot; live updates continue over WS."""
    return core_service.get_public_status(location_id=location_id)


@router.post("/evacuation-route", response_model=EvacuationRouteResponse)
@limiter.limit("20/minute")
async def get_public_evacuation_route(
    request: Request,
    payload: EvacuationRouteRequest,
    db: AsyncSession = Depends(get_db),
) -> EvacuationRouteResponse:
    """Return a server-side walking route to a real, open evacuation center."""
    center = await evacuation_center_crud.get(db=db, id=payload.center_id)
    if center is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Evacuation center not found.",
        )

    site = await location_crud.get(db=db, id=center.location_id)
    if site is None or not site.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Evacuation center not found.",
        )

    center_status = getattr(center.status, "value", center.status)
    if center_status != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The selected evacuation center is not open.",
        )

    return await evacuation_route_service.get_walking_route(
        origin=payload.origin,
        destination=center,
    )
