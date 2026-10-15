from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, require_auth
from app.core.database import get_db
from app.schemas import LocationCreate, LocationResponse, LocationUpdate
from app.services import location_service

router = APIRouter(prefix="/locations", tags=["locations"])


@router.get("", response_model=list[LocationResponse])
async def list_sites(
    db: AsyncSession = Depends(get_db),
) -> list[LocationResponse]:
    """Public: the site pickers (admin view scope + responder presence) need
    the registry without admin credentials — same exposure as sensor config."""
    return await location_service.list_sites(db=db)


@router.post("", response_model=LocationResponse, status_code=201)
async def create_site(
    payload: LocationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_auth),
) -> LocationResponse:
    return await location_service.create_site(
        db=db, payload=payload, current_user=current_user
    )


@router.put("/{location_id}", response_model=LocationResponse)
async def update_site(
    location_id: int,
    payload: LocationUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_auth),
) -> LocationResponse:
    return await location_service.update_site(
        db=db, location_id=location_id, payload=payload, current_user=current_user
    )
