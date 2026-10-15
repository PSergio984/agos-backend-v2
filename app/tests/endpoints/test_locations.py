"""Tests for the /locations site registry — the multi-site foundation.

Same seam as the claim tests: endpoint contract via TestClient (no context
manager) with get_db + require_auth overridden, and service mocked/patched.
"""

import uuid

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.schemas import LocationResponse
from app.services.location_service import location_service

SITE_A = LocationResponse(id=1, name="Site A", latitude=14.6, longitude=121.0, is_active=True)
SITE_B = LocationResponse(id=2, name="Site B", latitude=14.7, longitude=121.1, is_active=True)


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


def test_create_site_success(admin_client):
    with patch.object(
        location_service, "create_site", new=AsyncMock(return_value=SITE_B)
    ):
        res = admin_client.post(
            "/api/v1/locations",
            json={"name": "Site B", "latitude": 14.7, "longitude": 121.1},
        )

    assert res.status_code == 201
    assert res.json()["name"] == "Site B"


def test_create_site_duplicate_name_returns_409(admin_client):
    with patch.object(
        location_service,
        "create_site",
        new=AsyncMock(
            side_effect=HTTPException(status_code=409, detail="A site with that name already exists.")
        ),
    ):
        res = admin_client.post(
            "/api/v1/locations",
            json={"name": "Site A", "latitude": 14.6, "longitude": 121.0},
        )

    assert res.status_code == 409


def test_update_site_success(admin_client):
    updated = LocationResponse(id=1, name="Site A Renamed", latitude=14.6, longitude=121.0, is_active=True)
    with patch.object(
        location_service, "update_site", new=AsyncMock(return_value=updated)
    ):
        res = admin_client.put("/api/v1/locations/1", json={"name": "Site A Renamed"})

    assert res.status_code == 200
    assert res.json()["name"] == "Site A Renamed"


def test_update_site_not_found_returns_404(admin_client):
    with patch.object(
        location_service,
        "update_site",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="Site not found.")),
    ):
        res = admin_client.put("/api/v1/locations/99", json={"name": "Nowhere"})

    assert res.status_code == 404


def test_deactivate_site_via_update(admin_client):
    deactivated = LocationResponse(id=1, name="Site A", latitude=14.6, longitude=121.0, is_active=False)
    with patch.object(
        location_service, "update_site", new=AsyncMock(return_value=deactivated)
    ):
        res = admin_client.put("/api/v1/locations/1", json={"is_active": False})

    assert res.status_code == 200
    assert res.json()["is_active"] is False


def test_create_site_empty_name_rejected(admin_client):
    res = admin_client.post(
        "/api/v1/locations",
        json={"name": "   ", "latitude": 14.6, "longitude": 121.0},
    )
    assert res.status_code == 422


def test_list_sites_defaults_to_active_only(anon_client):
    inactive = LocationResponse(id=3, name="Old Site", latitude=14.5, longitude=120.9, is_active=False)
    with patch.object(
        location_service, "list_sites", new=AsyncMock(return_value=[SITE_A, SITE_B, inactive])
    ) as svc:
        anon_client.get("/api/v1/locations")

    assert svc.await_args.kwargs["include_inactive"] is False


def test_list_sites_include_inactive_param_passed(anon_client):
    with patch.object(
        location_service, "list_sites", new=AsyncMock(return_value=[SITE_A])
    ) as svc:
        anon_client.get("/api/v1/locations?include_inactive=true")

    assert svc.await_args.kwargs["include_inactive"] is True


def test_update_site_name_race_returns_409(admin_client):
    with patch.object(
        location_service,
        "update_site",
        new=AsyncMock(
            side_effect=HTTPException(status_code=409, detail="A site with that name already exists.")
        ),
    ):
        res = admin_client.put("/api/v1/locations/1", json={"name": "Taken"})

    assert res.status_code == 409


# ---------------------------------------------------------------------------
# Service-level: deactivation cleanup
# ---------------------------------------------------------------------------


def _site_row(is_active=True):
    row = MagicMock(id=1, latitude=14.6, longitude=121.0, is_active=is_active)
    row.name = "Site A"  # MagicMock `name` kwarg sets the repr name, not the attr
    return row


def _update_site_patches(updated, existing):
    from app.services.location_service import (
        admin_audit_log_crud,
        cache_service,
        fusion_state_manager,
        location_crud,
        responder_crud,
        responder_deployment_crud,
    )
    return (
        patch.object(location_crud, "get", new=AsyncMock(return_value=existing)),
        patch.object(location_crud, "get_by_name", new=AsyncMock(return_value=None)),
        patch.object(location_crud, "update_no_commit", new=AsyncMock(return_value=updated)),
        patch.object(responder_crud, "clear_active_site", new=AsyncMock()),
        patch.object(responder_deployment_crud, "close_open_for_location", new=AsyncMock()),
        patch.object(admin_audit_log_crud, "create_only_no_commit", new=AsyncMock()),
        patch.object(cache_service, "invalidate_location_caches"),
        patch.object(fusion_state_manager, "start_fusion_analysis_state"),
        patch.object(fusion_state_manager, "stop_fusion_analysis_state"),
    )


async def test_update_site_deactivation_clears_presence_and_deployments():
    from app.schemas import LocationUpdate

    get_p, name_p, upd_p, clear_p, close_p, audit_p, cache_p, fusion_p, stop_p = _update_site_patches(
        updated=_site_row(is_active=False), existing=_site_row()
    )
    with get_p, name_p, upd_p, clear_p as clear, close_p as close, audit_p, cache_p, fusion_p, stop_p as stop:
        result = await location_service.update_site(
            db=AsyncMock(), location_id=1, payload=LocationUpdate(is_active=False),
            current_user=MagicMock(id=uuid.uuid4()),
        )

    clear.assert_awaited_once()
    close.assert_awaited_once()
    stop.assert_called_once()  # dead site stops serving its frozen snapshot
    assert result.is_active is False


async def test_update_site_other_changes_skip_cleanup():
    from app.schemas import LocationUpdate

    get_p, name_p, upd_p, clear_p, close_p, audit_p, cache_p, fusion_p, stop_p = _update_site_patches(
        updated=_site_row(is_active=True), existing=_site_row()
    )
    with get_p, name_p, upd_p, clear_p as clear, close_p as close, audit_p, cache_p, fusion_p, stop_p as stop:
        await location_service.update_site(
            db=AsyncMock(), location_id=1, payload=LocationUpdate(name="Renamed"),
            current_user=MagicMock(id=uuid.uuid4()),
        )

    clear.assert_not_awaited()
    close.assert_not_awaited()
    stop.assert_not_called()
