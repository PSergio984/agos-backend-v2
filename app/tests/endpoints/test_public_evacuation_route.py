from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from app.api.v1.endpoints import public
from app.models import EvacuationCenterStatus
from app.schemas import EvacuationRouteResponse, RouteCoordinate


def make_center(*, status: EvacuationCenterStatus = EvacuationCenterStatus.OPEN):
    return SimpleNamespace(
        id=7,
        name="Maysan 3S",
        latitude=14.706,
        longitude=120.965,
        status=status,
        location_id=1,
    )


def patch_active_site(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        public.location_crud,
        "get",
        AsyncMock(return_value=SimpleNamespace(id=1, is_active=True)),
    )


@pytest.mark.asyncio
async def test_public_evacuation_route_returns_server_generated_route(
    async_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    center = make_center()
    get_center = AsyncMock(return_value=center)
    get_route = AsyncMock(
        return_value=EvacuationRouteResponse(
            geometry=[
                RouteCoordinate(latitude=14.71, longitude=120.97),
                RouteCoordinate(latitude=14.706, longitude=120.965),
            ],
            distance_meters=850,
            duration_seconds=620,
        )
    )
    monkeypatch.setattr(public.evacuation_center_crud, "get", get_center)
    patch_active_site(monkeypatch)
    monkeypatch.setattr(
        public.evacuation_route_service,
        "get_walking_route",
        get_route,
    )

    response = await async_client.post(
        "/api/v1/public/evacuation-route",
        json={
            "center_id": center.id,
            "origin": {"latitude": 14.71, "longitude": 120.97},
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "geometry": [
            {"latitude": 14.71, "longitude": 120.97},
            {"latitude": 14.706, "longitude": 120.965},
        ],
        "distance_meters": 850.0,
        "duration_seconds": 620.0,
        "provider": "openrouteservice",
    }
    get_route.assert_awaited_once()


@pytest.mark.asyncio
async def test_public_evacuation_route_rejects_unknown_center(
    async_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        public.evacuation_center_crud,
        "get",
        AsyncMock(return_value=None),
    )

    response = await async_client.post(
        "/api/v1/public/evacuation-route",
        json={
            "center_id": 999,
            "origin": {"latitude": 14.71, "longitude": 120.97},
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Evacuation center not found."


@pytest.mark.asyncio
async def test_public_evacuation_route_rejects_closed_center(
    async_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        public.evacuation_center_crud,
        "get",
        AsyncMock(return_value=make_center(status=EvacuationCenterStatus.CLOSED)),
    )
    patch_active_site(monkeypatch)

    response = await async_client.post(
        "/api/v1/public/evacuation-route",
        json={
            "center_id": 7,
            "origin": {"latitude": 14.71, "longitude": 120.97},
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "The selected evacuation center is not open."
    )
