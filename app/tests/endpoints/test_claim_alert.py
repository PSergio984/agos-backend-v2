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
    d.location_id = 2
    for k, v in overrides.items():
        setattr(d, k, v)
    return d


def _responder(**overrides):
    r = MagicMock()
    r.id = RESPONDER_ID
    r.first_name = "Danny"
    r.last_name = "Cleaner"
    # Present at site 2 — the dispatch fixture's site — so a claim is legal.
    r.location_id = 2
    r.active_location_id = None
    for k, v in overrides.items():
        setattr(r, k, v)
    return r


def _patches(dispatch=None, has_ack=False, responder=None, has_delivery=True):
    if responder is None and dispatch is not None and has_delivery:
        # The site gate reads the responder row before the type/claim checks —
        # tests that reach past the delivery check need a real responder.
        responder = _responder()
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
        patch(
            f"{MODULE}.require_active_site",
            new=AsyncMock(),
        ),
    )


async def test_service_claim_sets_fields_and_broadcasts():
    dispatch = _dispatch()
    db = AsyncMock()
    crud_d, crud_del, crud_r, ws, gate = _patches(
        dispatch=dispatch, responder=_responder()
    )
    with crud_d, crud_del, crud_r, ws as ws_mock, gate:
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
    # Broadcast goes to the dispatch's site channel.
    assert kwargs["location_id"] == 2
    assert result.claimed_by_name == "Danny Cleaner"


async def test_service_claim_null_site_dispatch_broadcasts_to_all_sites():
    # Announcement dispatches (location_id=None) reached every site, so the
    # claim broadcast fans out to every site's channel.
    dispatch = _dispatch(location_id=None)
    db = AsyncMock()
    crud_d, crud_del, crud_r, ws, gate = _patches(
        dispatch=dispatch, responder=_responder()
    )
    with (
        crud_d,
        crud_del,
        crud_r,
        ws as ws_mock,
        patch(
            f"{MODULE}.location_crud",
            **{"get_all_ids": AsyncMock(return_value=[1, 2, 3])},
        ),
    ):
        await responder_app_service.claim_alert(
            dispatch_id=DISPATCH_ID,
            current_responder=CurrentResponder(id=RESPONDER_ID),
            db=db,
        )

    assert ws_mock.broadcast_update.await_count == 3
    sites = {call.kwargs["location_id"] for call in ws_mock.broadcast_update.await_args_list}
    assert sites == {1, 2, 3}
    for call in ws_mock.broadcast_update.await_args_list:
        assert call.args[0] == "alert_claimed"


async def test_service_claim_already_claimed_409():
    dispatch = _dispatch(claimed_by=uuid.uuid4())
    crud_d, crud_del, crud_r, ws, gate = _patches(dispatch=dispatch)
    with crud_d, crud_del, crud_r, ws, gate:
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.claim_alert(
                dispatch_id=DISPATCH_ID,
                current_responder=CurrentResponder(id=RESPONDER_ID),
                db=AsyncMock(),
            )
    assert exc.value.status_code == 409


async def test_service_claim_acknowledged_409():
    crud_d, crud_del, crud_r, ws, gate = _patches(dispatch=_dispatch(), has_ack=True)
    with crud_d, crud_del, crud_r, ws, gate:
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.claim_alert(
                dispatch_id=DISPATCH_ID,
                current_responder=CurrentResponder(id=RESPONDER_ID),
                db=AsyncMock(),
            )
    assert exc.value.status_code == 409


async def test_service_claim_wrong_type_403():
    crud_d, crud_del, crud_r, ws, gate = _patches(
        dispatch=_dispatch(type=NotificationType.CRITICAL)
    )
    with crud_d, crud_del, crud_r, ws, gate:
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.claim_alert(
                dispatch_id=DISPATCH_ID,
                current_responder=CurrentResponder(id=RESPONDER_ID),
                db=AsyncMock(),
            )
    assert exc.value.status_code == 403


async def test_service_claim_missing_404():
    crud_d, crud_del, crud_r, ws, gate = _patches(dispatch=None)
    with crud_d, crud_del, crud_r, ws, gate:
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
    crud_d, crud_del, crud_r, ws, gate = _patches(
        dispatch=_dispatch(), has_delivery=False
    )
    with crud_d, crud_del, crud_r, ws, gate:
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.claim_alert(
                dispatch_id=DISPATCH_ID,
                current_responder=CurrentResponder(id=RESPONDER_ID),
                db=AsyncMock(),
            )
    assert exc.value.status_code == 404


async def test_service_claim_other_site_dispatch_404():
    # Presence-scoped claims: the responder WAS sent this dispatch but has
    # since moved — site 3 can't claim a site-2 alert.
    crud_d, crud_del, crud_r, ws, gate = _patches(
        dispatch=_dispatch(),
        responder=_responder(location_id=3),
    )
    with crud_d, crud_del, crud_r, ws, gate:
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.claim_alert(
                dispatch_id=DISPATCH_ID,
                current_responder=CurrentResponder(id=RESPONDER_ID),
                db=AsyncMock(),
            )
    assert exc.value.status_code == 404


async def test_service_claim_deployed_site_allowed():
    # Deployed presence counts: home site is 3 but active_location_id=2 puts
    # the responder on the dispatch's site.
    dispatch = _dispatch()
    crud_d, crud_del, crud_r, ws, gate = _patches(
        dispatch=dispatch,
        responder=_responder(location_id=3, active_location_id=2),
    )
    with crud_d, crud_del, crud_r, ws, gate:
        result = await responder_app_service.claim_alert(
            dispatch_id=DISPATCH_ID,
            current_responder=CurrentResponder(id=RESPONDER_ID),
            db=AsyncMock(),
        )
    assert result.dispatch_id == DISPATCH_ID
    assert dispatch.claimed_by == RESPONDER_ID


async def test_service_claim_foreign_site_dispatch_still_404():
    # T4: site-B dispatch + site-A responder. The delivery gate (T3's fan-out
    # already kept the alert off their list) rejects the claim — 404 and no
    # broadcast, same as a dispatch that doesn't exist.
    dispatch = _dispatch(location_id=2)
    responder = _responder(location_id=1)
    crud_d, crud_del, crud_r, ws, gate = _patches(
        dispatch=dispatch, responder=responder, has_delivery=False
    )
    with crud_d, crud_del, crud_r, ws as ws_mock, gate:
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.claim_alert(
                dispatch_id=DISPATCH_ID,
                current_responder=CurrentResponder(id=RESPONDER_ID),
                db=AsyncMock(),
            )
    assert exc.value.status_code == 404
    ws_mock.broadcast_update.assert_not_called()


# ---------------------------------------------------------------------------
# #29 — claims are site-scoped writes: gate on the dispatch site's is_active
# ---------------------------------------------------------------------------


async def test_service_claim_inactive_dispatch_site_rejects():
    # The dispatch's site was deactivated after the alert went out — a claim
    # writes dispatch state, so it must serialize against deactivation like
    # every other gated writer. The shared gate raises 400 "Site is inactive."
    dispatch = _dispatch()
    crud_d, crud_del, crud_r, ws, gate = _patches(
        dispatch=dispatch, responder=_responder()
    )
    with crud_d, crud_del, crud_r, ws as ws_mock, gate as gate_mock:
        gate_mock.side_effect = HTTPException(
            status_code=400, detail="Site is inactive."
        )
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.claim_alert(
                dispatch_id=DISPATCH_ID,
                current_responder=CurrentResponder(id=RESPONDER_ID),
                db=AsyncMock(),
            )

    assert exc.value.status_code == 400
    gate_mock.assert_awaited_once()
    assert gate_mock.await_args.kwargs["location_id"] == 2
    ws_mock.broadcast_update.assert_not_called()


async def test_service_claim_null_site_skips_site_gate():
    # NULL-site dispatches are announcements — not bound to any site, so no
    # site gate applies and they stay claimable.
    dispatch = _dispatch(location_id=None)
    crud_d, crud_del, crud_r, ws, gate = _patches(
        dispatch=dispatch, responder=_responder()
    )
    with (
        crud_d,
        crud_del,
        crud_r,
        ws,
        gate as gate_mock,
        patch(
            f"{MODULE}.location_crud",
            **{"get_all_ids": AsyncMock(return_value=[1, 2])},
        ),
    ):
        await responder_app_service.claim_alert(
            dispatch_id=DISPATCH_ID,
            current_responder=CurrentResponder(id=RESPONDER_ID),
            db=AsyncMock(),
        )

    gate_mock.assert_not_called()
    assert dispatch.claimed_by == RESPONDER_ID
