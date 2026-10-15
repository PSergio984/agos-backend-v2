"""Tests for the /locations site registry — the multi-site foundation.

Same seam as the claim tests: endpoint contract via TestClient (no context
manager) with get_db + require_auth overridden, and service mocked/patched.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.api.v1.dependencies import CurrentUser, require_auth
from app.core.database import get_db
from app.schemas import LocationResponse
from app.services.location_service import location_service

ADMIN_ID = uuid.uuid4()

SITE_A = LocationResponse(id=1, name="Site A", latitude=14.6, longitude=121.0, is_active=True)
SITE_B = LocationResponse(id=2, name="Site B", latitude=14.7, longitude=121.1, is_active=True)


@pytest.fixture()
def authed_client():
    async def _fake_db():
        yield MagicMock()

    app.dependency_overrides[require_auth] = lambda: CurrentUser(
        id=str(ADMIN_ID), is_superuser=True, is_enabled=True, force_password_change=False
    )
    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def anon_client():
    async def _fake_db():
        yield MagicMock()

    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Endpoint contract
# ---------------------------------------------------------------------------


def test_list_sites_is_public(anon_client):
    with patch.object(
        location_service, "list_sites", new=AsyncMock(return_value=[SITE_A, SITE_B])
    ):
        res = anon_client.get("/api/v1/locations")

    assert res.status_code == 200
    body = res.json()
    assert [s["name"] for s in body] == ["Site A", "Site B"]


def test_create_site_requires_auth(anon_client):
    res = anon_client.post(
        "/api/v1/locations",
        json={"name": "Site C", "latitude": 14.8, "longitude": 121.2},
    )
    assert res.status_code in (401, 403)


def test_create_site_success(authed_client):
    with patch.object(
        location_service, "create_site", new=AsyncMock(return_value=SITE_B)
    ):
        res = authed_client.post(
            "/api/v1/locations",
            json={"name": "Site B", "latitude": 14.7, "longitude": 121.1},
        )

    assert res.status_code == 201
    assert res.json()["name"] == "Site B"


def test_create_site_duplicate_name_returns_409(authed_client):
    with patch.object(
        location_service,
        "create_site",
        new=AsyncMock(
            side_effect=HTTPException(status_code=409, detail="A site with that name already exists.")
        ),
    ):
        res = authed_client.post(
            "/api/v1/locations",
            json={"name": "Site A", "latitude": 14.6, "longitude": 121.0},
        )

    assert res.status_code == 409


def test_update_site_success(authed_client):
    updated = LocationResponse(id=1, name="Site A Renamed", latitude=14.6, longitude=121.0, is_active=True)
    with patch.object(
        location_service, "update_site", new=AsyncMock(return_value=updated)
    ):
        res = authed_client.put("/api/v1/locations/1", json={"name": "Site A Renamed"})

    assert res.status_code == 200
    assert res.json()["name"] == "Site A Renamed"


def test_update_site_not_found_returns_404(authed_client):
    with patch.object(
        location_service,
        "update_site",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="Site not found.")),
    ):
        res = authed_client.put("/api/v1/locations/99", json={"name": "Nowhere"})

    assert res.status_code == 404


def test_deactivate_site_via_update(authed_client):
    deactivated = LocationResponse(id=1, name="Site A", latitude=14.6, longitude=121.0, is_active=False)
    with patch.object(
        location_service, "update_site", new=AsyncMock(return_value=deactivated)
    ):
        res = authed_client.put("/api/v1/locations/1", json={"is_active": False})

    assert res.status_code == 200
    assert res.json()["is_active"] is False


def test_create_site_empty_name_rejected(authed_client):
    res = authed_client.post(
        "/api/v1/locations",
        json={"name": "   ", "latitude": 14.6, "longitude": 121.0},
    )
    assert res.status_code == 422
