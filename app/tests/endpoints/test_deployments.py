"""Tests for responder deployments (admin membership) + active-site presence.

Same seam as the claim/location tests: endpoint contract via TestClient (no
context manager) with get_db + auth deps overridden and services patched;
service logic with CRUD patched by AsyncMock.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.v1.dependencies import CurrentUser
from app.schemas.responder import (
    ResponderActiveSiteResponse,
    ResponderDeploymentCreate,
    ResponderDeploymentItem,
    ResponderDeploymentsResponse,
)
from app.services.responder.responder_app_service import responder_app_service
from app.services.responder.responder_deployment_service import responder_deployment_service

ADMIN_ID = uuid.uuid4()
RESPONDER_ID = uuid.uuid4()
HOME_SITE_ID = 1
SITE_B_ID = 2
SITE_C_ID = 3
DEPLOYMENT_ID = 5

DEPLOYMENT_ITEM = ResponderDeploymentItem(
    id=DEPLOYMENT_ID,
    location_id=SITE_B_ID,
    location_name="Site B",
    starts_at=datetime.now(timezone.utc),
    ends_at=None,
    planned_ends_at=None,
    created_by=ADMIN_ID,
)


# ---------------------------------------------------------------------------
# Endpoint contract — admin deployment routes
# ---------------------------------------------------------------------------


def test_open_deployment_requires_auth(anon_client):
    res = anon_client.post(
        f"/api/v1/responders/{RESPONDER_ID}/deployments",
        json={"location_id": SITE_B_ID},
    )
    assert res.status_code in (401, 403)


def test_close_deployment_requires_auth(anon_client):
    res = anon_client.post(
        f"/api/v1/responders/{RESPONDER_ID}/deployments/{DEPLOYMENT_ID}/close"
    )
    assert res.status_code in (401, 403)


def test_list_deployments_requires_auth(anon_client):
    res = anon_client.get(f"/api/v1/responders/{RESPONDER_ID}/deployments")
    assert res.status_code in (401, 403)


def test_open_deployment_created(admin_client):
    with patch.object(
        responder_deployment_service,
        "open_deployment",
        new=AsyncMock(return_value=DEPLOYMENT_ITEM),
    ):
        res = admin_client.post(
            f"/api/v1/responders/{RESPONDER_ID}/deployments",
            json={"location_id": SITE_B_ID},
        )

    assert res.status_code == 201
    body = res.json()
    assert body["id"] == DEPLOYMENT_ID
    assert body["location_id"] == SITE_B_ID
    assert body["location_name"] == "Site B"
    assert body["ends_at"] is None
    assert body["created_by"] == str(ADMIN_ID)


def test_open_deployment_unknown_responder_returns_404(admin_client):
    with patch.object(
        responder_deployment_service,
        "open_deployment",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="Responder not found.")),
    ):
        res = admin_client.post(
            f"/api/v1/responders/{uuid.uuid4()}/deployments",
            json={"location_id": SITE_B_ID},
        )
    assert res.status_code == 404


def test_open_deployment_unknown_site_returns_404(admin_client):
    with patch.object(
        responder_deployment_service,
        "open_deployment",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="Site not found.")),
    ):
        res = admin_client.post(
            f"/api/v1/responders/{RESPONDER_ID}/deployments",
            json={"location_id": 999},
        )
    assert res.status_code == 404


def test_open_deployment_to_home_site_returns_409(admin_client):
    with patch.object(
        responder_deployment_service,
        "open_deployment",
        new=AsyncMock(
            side_effect=HTTPException(
                status_code=409, detail="A responder's home site is not a deployment."
            )
        ),
    ):
        res = admin_client.post(
            f"/api/v1/responders/{RESPONDER_ID}/deployments",
            json={"location_id": HOME_SITE_ID},
        )
    assert res.status_code == 409


def test_open_deployment_duplicate_open_returns_409(admin_client):
    with patch.object(
        responder_deployment_service,
        "open_deployment",
        new=AsyncMock(
            side_effect=HTTPException(
                status_code=409, detail="An open deployment to this site already exists."
            )
        ),
    ):
        res = admin_client.post(
            f"/api/v1/responders/{RESPONDER_ID}/deployments",
            json={"location_id": SITE_B_ID},
        )
    assert res.status_code == 409


def test_close_deployment_success(admin_client):
    closed = DEPLOYMENT_ITEM.model_copy(
        update={"ends_at": datetime.now(timezone.utc)}
    )
    with patch.object(
        responder_deployment_service,
        "close_deployment",
        new=AsyncMock(return_value=closed),
    ):
        res = admin_client.post(
            f"/api/v1/responders/{RESPONDER_ID}/deployments/{DEPLOYMENT_ID}/close"
        )

    assert res.status_code == 200
    assert res.json()["ends_at"] is not None


def test_close_deployment_wrong_ids_returns_404(admin_client):
    with patch.object(
        responder_deployment_service,
        "close_deployment",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="Deployment not found.")),
    ):
        res = admin_client.post(
            f"/api/v1/responders/{RESPONDER_ID}/deployments/999/close"
        )
    assert res.status_code == 404


def test_close_deployment_already_closed_returns_409(admin_client):
    with patch.object(
        responder_deployment_service,
        "close_deployment",
        new=AsyncMock(
            side_effect=HTTPException(status_code=409, detail="Deployment is already closed.")
        ),
    ):
        res = admin_client.post(
            f"/api/v1/responders/{RESPONDER_ID}/deployments/{DEPLOYMENT_ID}/close"
        )
    assert res.status_code == 409


def test_list_deployments(admin_client):
    payload = ResponderDeploymentsResponse(
        home_location_id=HOME_SITE_ID,
        home_location_name="Site A",
        active_location_id=SITE_B_ID,
        deployments=[DEPLOYMENT_ITEM],
    )
    with patch.object(
        responder_deployment_service,
        "list_deployments",
        new=AsyncMock(return_value=payload),
    ):
        res = admin_client.get(f"/api/v1/responders/{RESPONDER_ID}/deployments")

    assert res.status_code == 200
    body = res.json()
    assert body["home_location_id"] == HOME_SITE_ID
    assert body["home_location_name"] == "Site A"
    assert body["active_location_id"] == SITE_B_ID
    assert len(body["deployments"]) == 1
    assert body["deployments"][0]["location_name"] == "Site B"
    assert body["deployments"][0]["starts_at"] is not None
    assert body["deployments"][0]["created_by"] == str(ADMIN_ID)


def test_list_deployments_unknown_responder_returns_404(admin_client):
    with patch.object(
        responder_deployment_service,
        "list_deployments",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="Responder not found.")),
    ):
        res = admin_client.get(f"/api/v1/responders/{uuid.uuid4()}/deployments")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Endpoint contract — POST /responder/active-site
# ---------------------------------------------------------------------------


def test_set_active_site_success(responder_client):
    response = ResponderActiveSiteResponse(
        location_id=HOME_SITE_ID, active_location_id=SITE_B_ID
    )
    with patch.object(
        responder_app_service, "set_active_site", new=AsyncMock(return_value=response)
    ):
        res = responder_client.post(
            "/api/v1/responder/active-site", json={"location_id": SITE_B_ID}
        )

    assert res.status_code == 200
    assert res.json() == {
        "location_id": HOME_SITE_ID,
        "active_location_id": SITE_B_ID,
    }


def test_set_active_site_non_member_returns_400(responder_client):
    with patch.object(
        responder_app_service,
        "set_active_site",
        new=AsyncMock(
            side_effect=HTTPException(
                status_code=400, detail="Responder is not deployed to that site."
            )
        ),
    ):
        res = responder_client.post(
            "/api/v1/responder/active-site", json={"location_id": SITE_C_ID}
        )
    assert res.status_code == 400


def test_set_active_site_requires_auth(anon_client):
    res = anon_client.post(
        "/api/v1/responder/active-site", json={"location_id": SITE_B_ID}
    )
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# Service logic — deployments
# ---------------------------------------------------------------------------

MODULE = "app.services.responder.responder_deployment_service"
APP_MODULE = "app.services.responder.responder_app_service"


def _admin():
    return CurrentUser(
        id=str(ADMIN_ID), is_superuser=True, is_enabled=True, force_password_change=False
    )


def _db():
    db = AsyncMock()
    db.add = MagicMock()  # sync session method — AsyncMock attrs would warn
    return db


def _responder(**overrides):
    r = MagicMock()
    r.id = RESPONDER_ID
    r.first_name = "Danny"
    r.last_name = "Cleaner"
    r.location_id = HOME_SITE_ID
    r.active_location_id = None
    for k, v in overrides.items():
        setattr(r, k, v)
    return r


def _location(**overrides):
    loc = MagicMock()
    loc.id = SITE_B_ID
    loc.name = "Site B"
    for k, v in overrides.items():
        setattr(loc, k, v)
    return loc


def _deployment(**overrides):
    d = MagicMock()
    d.id = DEPLOYMENT_ID
    d.responder_id = RESPONDER_ID
    d.location_id = SITE_B_ID
    d.location = _location()
    d.starts_at = datetime.now(timezone.utc)
    d.ends_at = None
    d.created_by = ADMIN_ID
    for k, v in overrides.items():
        setattr(d, k, v)
    return d


def _deployment_patches(
    responder=None,
    location=None,
    open_deployment=None,
    deployment=None,
    deployments=None,
):
    async def _close(db, deployment):
        deployment.ends_at = datetime.now(timezone.utc)

    # Built by hand: "create" is a reserved patch() kwarg, so it can't be set
    # via the **attrs form.
    dep_crud = MagicMock()
    dep_crud.get_open_for_responder = AsyncMock(return_value=open_deployment)
    dep_crud.get_for_responder = AsyncMock(return_value=deployment)
    dep_crud.list_for_responder = AsyncMock(return_value=deployments or [])
    dep_crud.create = AsyncMock(return_value=_deployment(id=DEPLOYMENT_ID + 1))
    dep_crud.close = AsyncMock(side_effect=_close)

    return (
        patch(
            f"{MODULE}.responder_crud",
            **{
                "get": AsyncMock(return_value=responder),
                "get_with_lock": AsyncMock(return_value=responder),
            },
        ),
        patch(
            f"{MODULE}.location_crud",
            **{"get": AsyncMock(return_value=location)},
        ),
        patch(f"{MODULE}.responder_deployment_crud", dep_crud),
        patch(
            f"{MODULE}.admin_audit_log_crud",
            **{"create_only_no_commit": AsyncMock()},
        ),
    )


async def test_service_open_deployment_creates_and_audits():
    cruds = _deployment_patches(
        responder=_responder(), location=_location()
    )
    db = _db()
    with cruds[0], cruds[1], cruds[2] as dep_crud, cruds[3] as audit:
        item = await responder_deployment_service.open_deployment(
            db=db,
            responder_id=RESPONDER_ID,
            payload=ResponderDeploymentCreate(location_id=SITE_B_ID),
            current_user=_admin(),
        )

    dep_crud.create.assert_awaited_once()
    assert dep_crud.create.call_args.kwargs["location_id"] == SITE_B_ID
    assert dep_crud.create.call_args.kwargs["responder_id"] == RESPONDER_ID
    dep_crud.close.assert_not_awaited()
    audit.create_only_no_commit.assert_awaited_once()
    db.commit.assert_awaited_once()
    assert item.location_name == "Site B"
    assert item.ends_at is None


async def test_service_open_deployment_unknown_responder_404():
    cruds = _deployment_patches(responder=None, location=_location())
    with cruds[0], cruds[1], cruds[2], cruds[3]:
        with pytest.raises(HTTPException) as exc:
            await responder_deployment_service.open_deployment(
                db=_db(),
                responder_id=uuid.uuid4(),
                payload=MagicMock(location_id=SITE_B_ID),
                current_user=_admin(),
            )
    assert exc.value.status_code == 404


async def test_service_open_deployment_unknown_site_404():
    cruds = _deployment_patches(responder=_responder(), location=None)
    with cruds[0], cruds[1], cruds[2], cruds[3]:
        with pytest.raises(HTTPException) as exc:
            await responder_deployment_service.open_deployment(
                db=_db(),
                responder_id=RESPONDER_ID,
                payload=MagicMock(location_id=999),
                current_user=_admin(),
            )
    assert exc.value.status_code == 404


async def test_service_open_deployment_to_home_site_409():
    cruds = _deployment_patches(responder=_responder(), location=_location(id=HOME_SITE_ID))
    with cruds[0], cruds[1], cruds[2] as dep_crud, cruds[3]:
        with pytest.raises(HTTPException) as exc:
            await responder_deployment_service.open_deployment(
                db=_db(),
                responder_id=RESPONDER_ID,
                payload=MagicMock(location_id=HOME_SITE_ID),
                current_user=_admin(),
            )
    assert exc.value.status_code == 409
    dep_crud.create.assert_not_awaited()


async def test_service_open_deployment_same_site_already_open_409():
    open_dep = _deployment(location_id=SITE_B_ID)
    cruds = _deployment_patches(
        responder=_responder(), location=_location(), open_deployment=open_dep
    )
    with cruds[0], cruds[1], cruds[2] as dep_crud, cruds[3]:
        with pytest.raises(HTTPException) as exc:
            await responder_deployment_service.open_deployment(
                db=_db(),
                responder_id=RESPONDER_ID,
                payload=MagicMock(location_id=SITE_B_ID),
                current_user=_admin(),
            )
    assert exc.value.status_code == 409
    dep_crud.close.assert_not_awaited()
    dep_crud.create.assert_not_awaited()


async def test_service_open_deployment_auto_closes_previous():
    responder = _responder(active_location_id=None)
    previous = _deployment(id=4, location_id=SITE_C_ID)
    cruds = _deployment_patches(
        responder=responder,
        location=_location(),
        open_deployment=previous,
        deployment=_deployment(id=6, location_id=SITE_B_ID),
    )
    with cruds[0], cruds[1], cruds[2] as dep_crud, cruds[3]:
        item = await responder_deployment_service.open_deployment(
            db=_db(),
            responder_id=RESPONDER_ID,
            payload=MagicMock(location_id=SITE_B_ID),
            current_user=_admin(),
        )

    dep_crud.close.assert_awaited_once()
    assert dep_crud.close.call_args.kwargs["deployment"] is previous
    assert previous.ends_at is not None
    dep_crud.create.assert_awaited_once()
    assert item.location_id == SITE_B_ID


async def test_service_open_deployment_auto_close_clears_active_site():
    # Responder was "on" the superseded site — presence falls back to home.
    responder = _responder(active_location_id=SITE_C_ID)
    previous = _deployment(id=4, location_id=SITE_C_ID)
    cruds = _deployment_patches(
        responder=responder,
        location=_location(),
        open_deployment=previous,
        deployment=_deployment(id=6, location_id=SITE_B_ID),
    )
    with cruds[0], cruds[1], cruds[2], cruds[3]:
        await responder_deployment_service.open_deployment(
            db=_db(),
            responder_id=RESPONDER_ID,
            payload=MagicMock(location_id=SITE_B_ID),
            current_user=_admin(),
        )

    assert responder.active_location_id is None


async def test_service_close_deployment_sets_ends_at_and_clears_active_site():
    responder = _responder(active_location_id=SITE_B_ID)
    deployment = _deployment(location_id=SITE_B_ID)
    cruds = _deployment_patches(responder=responder, deployment=deployment)
    db = _db()
    with cruds[0], cruds[1], cruds[2] as dep_crud, cruds[3] as audit:
        item = await responder_deployment_service.close_deployment(
            db=db,
            responder_id=RESPONDER_ID,
            deployment_id=DEPLOYMENT_ID,
            current_user=_admin(),
        )

    dep_crud.close.assert_awaited_once()
    assert deployment.ends_at is not None
    assert responder.active_location_id is None
    audit.create_only_no_commit.assert_awaited_once()
    db.commit.assert_awaited_once()
    assert item.ends_at == deployment.ends_at
    assert item.location_name == "Site B"


async def test_service_close_deployment_keeps_unrelated_active_site():
    # Active site pointed somewhere else (home) — closing this deployment
    # must not disturb it.
    responder = _responder(active_location_id=HOME_SITE_ID)
    deployment = _deployment(location_id=SITE_B_ID)
    cruds = _deployment_patches(responder=responder, deployment=deployment)
    with cruds[0], cruds[1], cruds[2], cruds[3]:
        await responder_deployment_service.close_deployment(
            db=_db(),
            responder_id=RESPONDER_ID,
            deployment_id=DEPLOYMENT_ID,
            current_user=_admin(),
        )

    assert responder.active_location_id == HOME_SITE_ID


async def test_service_close_deployment_wrong_ids_404():
    cruds = _deployment_patches(responder=_responder(), deployment=None)
    with cruds[0], cruds[1], cruds[2], cruds[3]:
        with pytest.raises(HTTPException) as exc:
            await responder_deployment_service.close_deployment(
                db=_db(),
                responder_id=RESPONDER_ID,
                deployment_id=999,
                current_user=_admin(),
            )
    assert exc.value.status_code == 404


async def test_service_close_deployment_already_closed_409():
    deployment = _deployment(ends_at=datetime.now(timezone.utc))
    cruds = _deployment_patches(responder=_responder(), deployment=deployment)
    with cruds[0], cruds[1], cruds[2] as dep_crud, cruds[3]:
        with pytest.raises(HTTPException) as exc:
            await responder_deployment_service.close_deployment(
                db=_db(),
                responder_id=RESPONDER_ID,
                deployment_id=DEPLOYMENT_ID,
                current_user=_admin(),
            )
    assert exc.value.status_code == 409
    dep_crud.close.assert_not_awaited()


async def test_service_list_deployments_returns_site_names():
    deployments = [
        _deployment(id=6, location_id=SITE_B_ID),
        _deployment(id=4, location_id=SITE_C_ID, location=_location(id=SITE_C_ID, name="Site C")),
    ]
    home = _location(id=HOME_SITE_ID, name="Home Base")
    cruds = _deployment_patches(responder=_responder(), location=home, deployments=deployments)
    with cruds[0], cruds[1], cruds[2], cruds[3]:
        res = await responder_deployment_service.list_deployments(
            db=_db(), responder_id=RESPONDER_ID
        )

    assert res.home_location_id == HOME_SITE_ID
    assert res.home_location_name == "Home Base"
    assert [i.location_name for i in res.deployments] == ["Site B", "Site C"]
    assert [i.id for i in res.deployments] == [6, 4]


async def test_service_list_deployments_unknown_responder_404():
    cruds = _deployment_patches(responder=None)
    with cruds[0], cruds[1], cruds[2], cruds[3]:
        with pytest.raises(HTTPException) as exc:
            await responder_deployment_service.list_deployments(
                db=_db(), responder_id=uuid.uuid4()
            )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Service logic — POST /responder/active-site
# ---------------------------------------------------------------------------


def _app_patches(responder=None, open_deployment=None, location=None):
    if location is None:
        location = _location(is_active=True)
    return (
        patch(
            f"{APP_MODULE}.responder_crud",
            **{"get_with_lock": AsyncMock(return_value=responder)},
        ),
        patch(
            f"{APP_MODULE}.responder_deployment_crud",
            **{"get_open_for_responder": AsyncMock(return_value=open_deployment)},
        ),
        patch(
            f"{APP_MODULE}.location_crud",
            **{"get": AsyncMock(return_value=location)},
        ),
    )


async def test_service_set_active_site_home():
    responder = _responder()
    cruds = _app_patches(responder=responder)
    db = _db()
    with cruds[0], cruds[1] as dep_crud, cruds[2]:
        result = await responder_app_service.set_active_site(
            responder_id=str(RESPONDER_ID), location_id=HOME_SITE_ID, db=db
        )

    dep_crud.get_open_for_responder.assert_not_awaited()
    assert responder.active_location_id == HOME_SITE_ID
    db.commit.assert_awaited_once()
    assert result.location_id == HOME_SITE_ID
    assert result.active_location_id == HOME_SITE_ID


async def test_service_set_active_site_deployed():
    responder = _responder()
    open_dep = _deployment(location_id=SITE_B_ID)
    cruds = _app_patches(responder=responder, open_deployment=open_dep)
    with cruds[0], cruds[1], cruds[2]:
        result = await responder_app_service.set_active_site(
            responder_id=str(RESPONDER_ID), location_id=SITE_B_ID, db=_db()
        )

    assert responder.active_location_id == SITE_B_ID
    assert result.active_location_id == SITE_B_ID


async def test_service_set_active_site_future_deployment_400():
    # A posting scheduled for the future isn't a valid presence target yet.
    responder = _responder()
    future_dep = _deployment(
        location_id=SITE_B_ID,
        starts_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    cruds = _app_patches(responder=responder, open_deployment=future_dep)
    with cruds[0], cruds[1], cruds[2]:
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.set_active_site(
                responder_id=str(RESPONDER_ID), location_id=SITE_B_ID, db=_db()
            )
    assert exc.value.status_code == 400


async def test_service_set_active_site_non_member_400():
    responder = _responder()
    open_dep = _deployment(location_id=SITE_B_ID)
    cruds = _app_patches(responder=responder, open_deployment=open_dep)
    with cruds[0], cruds[1], cruds[2]:
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.set_active_site(
                responder_id=str(RESPONDER_ID), location_id=SITE_C_ID, db=_db()
            )
    assert exc.value.status_code == 400
    assert responder.active_location_id is None


async def test_service_set_active_site_no_open_deployment_400():
    responder = _responder()
    cruds = _app_patches(responder=responder, open_deployment=None)
    with cruds[0], cruds[1], cruds[2]:
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.set_active_site(
                responder_id=str(RESPONDER_ID), location_id=SITE_B_ID, db=_db()
            )
    assert exc.value.status_code == 400


async def test_service_set_active_site_unknown_responder_404():
    cruds = _app_patches(responder=None)
    with cruds[0], cruds[1], cruds[2]:
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.set_active_site(
                responder_id=str(uuid.uuid4()), location_id=SITE_B_ID, db=_db()
            )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Review fixes: admin presence override, inactive-site deploy, planned window
# ---------------------------------------------------------------------------


def test_admin_set_active_site_success(admin_client):
    resp = ResponderActiveSiteResponse(location_id=HOME_SITE_ID, active_location_id=SITE_B_ID)
    with patch.object(
        responder_app_service, "admin_set_active_site", new=AsyncMock(return_value=resp)
    ) as svc:
        res = admin_client.put(
            f"/responders/{RESPONDER_ID}/active-site".replace("/responders/", "/api/v1/responders/"),
            json={"location_id": SITE_B_ID},
        )
    assert res.status_code == 200
    assert res.json()["active_location_id"] == SITE_B_ID
    svc.assert_awaited_once()


def test_admin_clear_active_site(admin_client):
    resp = ResponderActiveSiteResponse(location_id=HOME_SITE_ID, active_location_id=None)
    with patch.object(
        responder_app_service, "admin_set_active_site", new=AsyncMock(return_value=resp)
    ):
        res = admin_client.put(
            f"/api/v1/responders/{RESPONDER_ID}/active-site",
            json={"location_id": None},
        )
    assert res.status_code == 200
    assert res.json()["active_location_id"] is None


def test_admin_set_active_site_requires_auth(anon_client):
    res = anon_client.put(
        f"/api/v1/responders/{RESPONDER_ID}/active-site", json={"location_id": SITE_B_ID}
    )
    assert res.status_code in (401, 403)


async def test_service_admin_clear_active_site():
    responder = _responder()
    responder.active_location_id = SITE_B_ID
    cruds = _app_patches(responder=responder, open_deployment=None)
    with cruds[0], cruds[1], cruds[2]:
        result = await responder_app_service.admin_set_active_site(
            responder_id=str(RESPONDER_ID), location_id=None, db=_db()
        )
    assert responder.active_location_id is None
    assert result.active_location_id is None


async def test_service_open_deployment_inactive_site_400():
    cruds = _deployment_patches(
        responder=_responder(), location=_location(is_active=False)
    )
    with cruds[0], cruds[1], cruds[2], cruds[3]:
        with pytest.raises(HTTPException) as exc:
            await responder_deployment_service.open_deployment(
                db=_db(),
                responder_id=RESPONDER_ID,
                payload=ResponderDeploymentCreate(location_id=SITE_B_ID),
                current_user=_admin(),
            )
    assert exc.value.status_code == 400


async def test_service_open_deployment_passes_planned_ends_at():
    planned = datetime.now(timezone.utc) + timedelta(days=7)
    cruds = _deployment_patches(responder=_responder(), location=_location())
    with cruds[0], cruds[1], cruds[2] as dep_crud, cruds[3]:
        await responder_deployment_service.open_deployment(
            db=_db(),
            responder_id=RESPONDER_ID,
            payload=ResponderDeploymentCreate(
                location_id=SITE_B_ID, planned_ends_at=planned
            ),
            current_user=_admin(),
        )
    assert dep_crud.create.call_args.kwargs["planned_ends_at"] == planned


async def test_service_set_active_site_inactive_target_400():
    responder = _responder()
    cruds = _app_patches(responder=responder, location=_location(is_active=False))
    with cruds[0], cruds[1], cruds[2]:
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.set_active_site(
                responder_id=str(RESPONDER_ID), location_id=HOME_SITE_ID, db=_db()
            )
    assert exc.value.status_code == 400


async def test_service_open_deployment_flushes_close_before_insert():
    """R1 regression: within one flush SQLAlchemy emits INSERTs before UPDATEs,
    which would trip uq_responder_deployments_one_open while the superseded
    row still looks open. The close must flush first."""
    responder = _responder()
    prior = _deployment(location_id=SITE_C_ID)
    cruds = _deployment_patches(
        responder=responder, location=_location(), open_deployment=prior
    )
    db = _db()
    parent = MagicMock()
    with cruds[0], cruds[1], cruds[2] as dep_crud, cruds[3]:
        parent.attach_mock(dep_crud.close, "close")
        parent.attach_mock(dep_crud.create, "create")
        parent.attach_mock(db.flush, "flush")
        await responder_deployment_service.open_deployment(
            db=db,
            responder_id=RESPONDER_ID,
            payload=ResponderDeploymentCreate(location_id=SITE_B_ID),
            current_user=_admin(),
        )
    order = [c[0] for c in parent.mock_calls]
    assert order.index("close") < order.index("flush") < order.index("create")
