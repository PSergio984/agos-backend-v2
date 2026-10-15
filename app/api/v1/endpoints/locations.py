from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, optional_auth, require_auth
from app.core.database import get_db
from app.schemas import LocationCreate, LocationResponse, LocationUpdate
from app.services import location_service

router = APIRouter(prefix="/locations", tags=["locations"])


@router.get("", response_model=list[LocationResponse])
async def list_sites(
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser | None = Depends(optional_auth),
) -> list[LocationResponse]:
    """Public: the site pickers (admin view scope + responder presence) need
    the registry without admin credentials — same exposure as sensor config.
    Defaults to active sites only; include_inactive enumerates retired sites
    and is admin-only."""
    if include_inactive and current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin auth required to list inactive sites.",
        )
    return await location_service.list_sites(db=db, include_inactive=include_inactive)


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
