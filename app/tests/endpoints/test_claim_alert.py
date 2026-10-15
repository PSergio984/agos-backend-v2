"""Tests for POST /responder/claim — the "On it" claim on shared dispatches.

Two layers, both mocked at the seams (the suite's conftest provides no DB
isolation):
- endpoint contract: auth override + service patched → status codes & payloads
- service logic: CRUD/broadcast patched → the 404/403/409/success rules
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.api.v1.dependencies import CurrentResponder, require_responder_auth
from app.core.database import get_db
from app.models.notification_template import NotificationType
from app.schemas.responder import ClaimResponse
from app.services.responder.responder_app_service import responder_app_service

RESPONDER_ID = uuid.uuid4()
DISPATCH_ID = 7


@pytest.fixture()
def client():
    async def _fake_db():
        yield MagicMock()

    app.dependency_overrides[require_responder_auth] = lambda: CurrentResponder(
        id=RESPONDER_ID
    )
    app.dependency_overrides[get_db] = _fake_db
    # No context manager — `with TestClient` runs the app lifespan, which
    # opens a real DB connection. These tests only exercise the endpoint seam.
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Endpoint contract
# ---------------------------------------------------------------------------


def test_claim_success_returns_200(client):
    claim = ClaimResponse(
        dispatch_id=DISPATCH_ID,
        claimed_by=RESPONDER_ID,
        claimed_by_name="Danny Cleaner",
        claimed_at=datetime.now(timezone.utc),
    )
    with patch.object(
        responder_app_service, "claim_alert", new=AsyncMock(return_value=claim)
    ):
        res = client.post("/api/v1/responder/claim", json={"dispatch_id": DISPATCH_ID})

    assert res.status_code == 200
    body = res.json()
    assert body["dispatch_id"] == DISPATCH_ID
    assert body["claimed_by_name"] == "Danny Cleaner"


def test_claim_conflict_returns_409(client):
    with patch.object(
        responder_app_service,
        "claim_alert",
        new=AsyncMock(
            side_effect=HTTPException(status_code=409, detail="already claimed")
        ),
    ):
        res = client.post("/api/v1/responder/claim", json={"dispatch_id": DISPATCH_ID})

    assert res.status_code == 409


def test_claim_missing_returns_404(client):
    with patch.object(
        responder_app_service,
        "claim_alert",
        new=AsyncMock(
            side_effect=HTTPException(status_code=404, detail="not found")
        ),
    ):
        res = client.post("/api/v1/responder/claim", json={"dispatch_id": DISPATCH_ID})

    assert res.status_code == 404


def test_claim_unauthenticated_returns_401():
    # No auth override, no Authorization header, no lifespan.
    res = TestClient(app).post(
        "/api/v1/responder/claim", json={"dispatch_id": DISPATCH_ID}
    )
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# Service logic
# ---------------------------------------------------------------------------

MODULE = "app.services.responder.responder_app_service"


def _dispatch(**overrides):
    d = MagicMock()
    d.id = DISPATCH_ID
    d.type = NotificationType.BLOCKAGE
    d.claimed_by = None
    d.claimed_at = None
    for k, v in overrides.items():
        setattr(d, k, v)
    return d


def _responder(**overrides):
    r = MagicMock()
    r.id = RESPONDER_ID
    r.first_name = "Danny"
    r.last_name = "Cleaner"
    r.location_id = 3
    for k, v in overrides.items():
        setattr(r, k, v)
    return r


def _patches(dispatch=None, has_ack=False, responder=None, has_delivery=True):
    return (
        patch(
            f"{MODULE}.notification_dispatch_crud",
            **{"get_with_lock": AsyncMock(return_value=dispatch)},
        ),
        patch(
            f"{MODULE}.notification_delivery_crud",
            **{
                "dispatch_has_acknowledgement": AsyncMock(return_value=has_ack),
                "get_for_responder_dispatch": AsyncMock(
                    return_value=MagicMock() if has_delivery else None
                ),
            },
        ),
        patch(
            f"{MODULE}.responder_crud",
            **{"get": AsyncMock(return_value=responder)},
        ),
        patch(
            f"{MODULE}.websocket_service",
            **{"broadcast_update": AsyncMock()},
        ),
    )



async def test_service_claim_sets_fields_and_broadcasts():
    dispatch = _dispatch()
    db = AsyncMock()
    crud_d, crud_del, crud_r, ws = _patches(
        dispatch=dispatch, responder=_responder()
    )
    with crud_d, crud_del, crud_r, ws as ws_mock:
        result = await responder_app_service.claim_alert(
            dispatch_id=DISPATCH_ID,
            current_responder=CurrentResponder(id=RESPONDER_ID),
            db=db,
        )

    assert dispatch.claimed_by == RESPONDER_ID
    assert dispatch.claimed_at is not None
    db.commit.assert_awaited_once()
    ws_mock.broadcast_update.assert_awaited_once()
    args, kwargs = ws_mock.broadcast_update.call_args
    assert args[0] == "alert_claimed"
    assert kwargs["location_id"] == 3
    assert result.claimed_by_name == "Danny Cleaner"



async def test_service_claim_already_claimed_409():
    dispatch = _dispatch(claimed_by=uuid.uuid4())
    crud_d, crud_del, crud_r, ws = _patches(dispatch=dispatch)
    with crud_d, crud_del, crud_r, ws:
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.claim_alert(
                dispatch_id=DISPATCH_ID,
                current_responder=CurrentResponder(id=RESPONDER_ID),
                db=AsyncMock(),
            )
    assert exc.value.status_code == 409



async def test_service_claim_acknowledged_409():
    crud_d, crud_del, crud_r, ws = _patches(dispatch=_dispatch(), has_ack=True)
    with crud_d, crud_del, crud_r, ws:
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.claim_alert(
                dispatch_id=DISPATCH_ID,
                current_responder=CurrentResponder(id=RESPONDER_ID),
                db=AsyncMock(),
            )
    assert exc.value.status_code == 409



async def test_service_claim_wrong_type_403():
    crud_d, crud_del, crud_r, ws = _patches(
        dispatch=_dispatch(type=NotificationType.CRITICAL)
    )
    with crud_d, crud_del, crud_r, ws:
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.claim_alert(
                dispatch_id=DISPATCH_ID,
                current_responder=CurrentResponder(id=RESPONDER_ID),
                db=AsyncMock(),
            )
    assert exc.value.status_code == 403



async def test_service_claim_missing_404():
    crud_d, crud_del, crud_r, ws = _patches(dispatch=None)
    with crud_d, crud_del, crud_r, ws:
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.claim_alert(
                dispatch_id=DISPATCH_ID,
                current_responder=CurrentResponder(id=RESPONDER_ID),
                db=AsyncMock(),
            )
    assert exc.value.status_code == 404


async def test_service_claim_not_dispatched_to_responder_404():
    # A responder holding no delivery for this dispatch gets the same 404 as
    # a nonexistent dispatch — foreign alerts aren't claimable (or leakable).
    crud_d, crud_del, crud_r, ws = _patches(
        dispatch=_dispatch(), has_delivery=False
    )
    with crud_d, crud_del, crud_r, ws:
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.claim_alert(
                dispatch_id=DISPATCH_ID,
                current_responder=CurrentResponder(id=RESPONDER_ID),
                db=AsyncMock(),
            )
    assert exc.value.status_code == 404
