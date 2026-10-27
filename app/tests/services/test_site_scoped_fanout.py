"""Tests for T3 — site-scoped alert fan-out.

Same seam as test_claim_alert.py / test_locations.py:
- endpoint contract: TestClient WITHOUT a context manager, dependency_overrides
  for get_db + auth, service patched with AsyncMock.
- service logic: CRUD seams patched with AsyncMock.

Site rule: a dispatch with location_id=L delivers only to responders whose
effective site (active_location_id ?? location_id) is L; a NULL location_id is
a global announcement delivered to the given ids regardless of site.
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app.core.state as state_module
from app.api.v1.dependencies import CurrentUser, require_auth
from app.core.database import get_db
from app.crud import (
    notification_dispatch_crud,
    notification_template_crud,
    responder_crud,
    responder_group_crud,
)
from app.main import app
from app.models.notification_template import NotificationType
from app.models.responder_related.group import DEFAULT_ACTIVE_RESPONDERS_GROUP_NAME
from app.models.responder_related.notification_delivery import DeliveryStatus
from app.models.responder_related.responders import ResponderStatus
from app.schemas.responder import ResponderOTPVerifyRequest
from app.schemas.subscription import CustomNotificationPayload, SendNotificationSchema
from app.services.notification_log_service import notification_log_service
from app.services.notification_service import NotificationService, notification_service
from app.services.responder.responder_app_service import responder_app_service

ADMIN_ID = uuid.uuid4()
SITE_A = 1
SITE_B = 2


# ---------------------------------------------------------------------------
# Endpoint contract — manual send stays a NULL-site announcement
# ---------------------------------------------------------------------------


@pytest.fixture()
def authed_client():
    async def _fake_db():
        yield MagicMock()

    app.dependency_overrides[require_auth] = lambda: CurrentUser(
        id=str(ADMIN_ID), is_superuser=True, is_enabled=True, force_password_change=False
    )
    app.dependency_overrides[get_db] = _fake_db
    # No context manager — `with TestClient` runs the app lifespan, which
    # opens a real DB connection. These tests only exercise the endpoint seam.
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_manual_send_notification_is_announcement_scoped(authed_client):
    responder_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    with patch.object(
        notification_service, "send_notification_to_subscribers", new=AsyncMock()
    ) as send_mock:
        res = authed_client.post(
            "/api/v1/push/send-notification",
            json={
                "responder_ids": responder_ids,
                "custom_notification": {
                    "title": "Drill",
                    "message": "Test broadcast",
                    "type": "announcement",
                },
            },
        )

    assert res.status_code == 204
    send_mock.assert_awaited_once()
    kwargs = send_mock.call_args.kwargs
    assert kwargs.get("location_id") is None
    assert {str(rid) for rid in kwargs["payload"].responder_ids} == set(responder_ids)


# ---------------------------------------------------------------------------
# Service — send_notification_to_subscribers delivery-set filtering
# ---------------------------------------------------------------------------

NS_MODULE = "app.services.notification_service"


def _payload(responder_ids, notif_type=NotificationType.CRITICAL):
    return SendNotificationSchema(
        responder_ids=responder_ids,
        custom_notification=CustomNotificationPayload(
            type=notif_type, title="Alert", message="Message"
        ),
    )


def _send_patches(site_ids=None):
    """Patch the CRUD seams around send_notification_to_subscribers.

    site_ids is what the effective-site query returns — the subset of payload
    ids whose COALESCE(active_location_id, location_id) matches the dispatch
    site (a responder deployed to site B has effective site B even when their
    home site is A).
    """
    dispatch = MagicMock()
    dispatch.id = 99
    return (
        patch(
            f"{NS_MODULE}.responder_crud",
            **{
                "get_ids_in_effective_location": AsyncMock(return_value=site_ids),
                "get_reachable_ids": AsyncMock(
                    side_effect=lambda db, responder_ids: responder_ids
                ),
            },
        ),
        patch(
            f"{NS_MODULE}.notification_dispatch_crud",
            **{"create_for_send": AsyncMock(return_value=dispatch)},
        ),
        patch(
            f"{NS_MODULE}.push_subscription_crud",
            **{"get_by_responder_ids": AsyncMock(return_value=[])},
        ),
        patch(
            f"{NS_MODULE}.push_token_crud",
            **{"get_by_responder_ids": AsyncMock(return_value=[])},
        ),
        patch(
            f"{NS_MODULE}.notification_delivery_crud",
            **{"upsert_many_results": AsyncMock()},
        ),
    )


async def test_site_scoped_dispatch_delivers_only_to_matching_site():
    home_a = uuid.uuid4()          # home site A, no deployment -> effective A
    home_b = uuid.uuid4()          # effective site B -> excluded
    deployed_b = uuid.uuid4()      # home A but active_location_id B -> excluded
    crud_r, crud_d, crud_sub, crud_tok, crud_del = _send_patches(site_ids=[home_a])

    with crud_r as r_mock, crud_d as d_mock, crud_sub as sub_mock, crud_tok, crud_del as del_mock:
        await notification_service.send_notification_to_subscribers(
            payload=_payload([home_a, home_b, deployed_b]),
            db=AsyncMock(),
            system_initiated=True,
            location_id=SITE_A,
        )

    # All candidate ids go through the effective-site filter...
    r_mock.get_ids_in_effective_location.assert_awaited_once()
    site_call = r_mock.get_ids_in_effective_location.call_args.kwargs
    assert site_call["location_id"] == SITE_A
    assert set(site_call["responder_ids"]) == {home_a, home_b, deployed_b}

    # ...the dispatch is stamped with the site...
    assert d_mock.create_for_send.call_args.kwargs["location_id"] == SITE_A

    # ...and only site-filtered responders get subscription lookups/deliveries.
    assert sub_mock.get_by_responder_ids.call_args.kwargs["responder_ids"] == [home_a]
    rows = del_mock.upsert_many_results.call_args.kwargs["delivery_rows"]
    assert [row["responder_id"] for row in rows] == [home_a]


async def test_null_site_dispatch_delivers_to_all_given_ids():
    ids = [uuid.uuid4(), uuid.uuid4()]
    crud_r, crud_d, crud_sub, crud_tok, crud_del = _send_patches()

    with crud_r as r_mock, crud_d as d_mock, crud_sub, crud_tok, crud_del as del_mock:
        await notification_service.send_notification_to_subscribers(
            payload=_payload(ids, notif_type=NotificationType.ANNOUNCEMENT),
            db=AsyncMock(),
        )

    r_mock.get_ids_in_effective_location.assert_not_called()
    # Barangay-wide skips the site filter but keeps the reachable-only gate.
    r_mock.get_reachable_ids.assert_awaited_once()
    assert set(r_mock.get_reachable_ids.call_args.kwargs["responder_ids"]) == set(ids)
    assert d_mock.create_for_send.call_args.kwargs["location_id"] is None
    rows = del_mock.upsert_many_results.call_args.kwargs["delivery_rows"]
    assert {row["responder_id"] for row in rows} == set(ids)


async def test_site_scoped_dispatch_with_no_matching_responders_skips_dispatch():
    crud_r, crud_d, crud_sub, crud_tok, crud_del = _send_patches(site_ids=[])

    with crud_r, crud_d as d_mock, crud_sub as sub_mock, crud_tok, crud_del as del_mock:
        await notification_service.send_notification_to_subscribers(
            payload=_payload([uuid.uuid4()]),
            db=AsyncMock(),
            system_initiated=True,
            location_id=SITE_B,
        )

    d_mock.create_for_send.assert_not_called()
    sub_mock.get_by_responder_ids.assert_not_called()
    del_mock.upsert_many_results.assert_not_called()


async def test_create_for_send_stamps_location_id():
    db = MagicMock()
    db.flush = AsyncMock()
    dispatch = await notification_dispatch_crud.create_for_send(
        db=db,
        notif_type=NotificationType.BLOCKAGE,
        title="t",
        message="m",
        location_id=SITE_A,
    )
    assert dispatch.location_id == SITE_A
    db.add.assert_called_once_with(dispatch)
    db.flush.assert_awaited_once()


async def test_effective_site_query_uses_active_over_home():
    """COALESCE(active_location_id, location_id) — active deployment wins."""
    rid = uuid.uuid4()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [rid]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    out = await responder_crud.get_ids_in_effective_location(
        db=db, responder_ids=[rid], location_id=SITE_A
    )

    assert out == [rid]
    stmt = db.execute.call_args.args[0]
    sql = str(stmt.compile()).lower()
    assert "coalesce(responders.active_location_id, responders.location_id)" in sql


async def test_effective_site_query_empty_ids_short_circuits():
    db = MagicMock()
    db.execute = AsyncMock()
    out = await responder_crud.get_ids_in_effective_location(
        db=db, responder_ids=[], location_id=SITE_A
    )
    assert out == []
    db.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Detection path — FusionAnalysisState plumbs its site into the dispatch
# ---------------------------------------------------------------------------


def _patch_state_send(monkeypatch, ids):
    fake_db = AsyncMock()
    fake_session = MagicMock()
    fake_session.__aenter__.return_value = fake_db
    monkeypatch.setattr(state_module, "AsyncSessionLocal", lambda: fake_session)

    template = SimpleNamespace(
        id=5, type=NotificationType.CRITICAL, title="t", message="m"
    )
    monkeypatch.setattr(
        notification_template_crud, "get_by_type", AsyncMock(return_value=template)
    )
    get_ids = AsyncMock(return_value=ids)
    monkeypatch.setattr(
        responder_crud, "get_responder_ids_with_push_subscription", get_ids
    )
    # The real send returns the SENT delivery count — the fusion cooldown
    # arms on sent > 0. Patch the class, not the singleton instance:
    # monkeypatch would restore a bound method as an instance attribute,
    # permanently shadowing class-level patches in later tests.
    send = AsyncMock(return_value=1)
    monkeypatch.setattr(
        NotificationService, "send_notification_to_subscribers", send
    )
    return get_ids, send


async def test_detection_dispatch_is_stamped_with_fusion_site(monkeypatch):
    fusion = state_module.FusionAnalysisState(location_id=SITE_B)
    get_ids, send = _patch_state_send(monkeypatch, ids={uuid.uuid4()})

    ok = await fusion._send_system_template(NotificationType.CRITICAL, "[CRITICAL]")

    assert ok is True
    get_ids.assert_awaited_once()
    assert get_ids.call_args.kwargs["location_id"] == SITE_B
    send.assert_awaited_once()
    assert send.call_args.kwargs["location_id"] == SITE_B
    assert send.call_args.kwargs["system_initiated"] is True


async def test_detection_send_returns_false_when_no_site_recipients(monkeypatch):
    fusion = state_module.FusionAnalysisState(location_id=SITE_B)
    get_ids, send = _patch_state_send(monkeypatch, ids=set())

    ok = await fusion._send_system_template(NotificationType.CRITICAL, "[CRITICAL]")

    assert ok is False
    send.assert_not_called()


async def test_detection_send_returns_none_on_zero_deliveries(monkeypatch):
    """PR #30 review: a send that reached nobody still wrote dispatch + FAILED
    delivery rows. It must report the tri-state 'attempted, zero landed' — not
    False (nothing sent) — so the caller can back off instead of either
    re-stamping every tick or silencing for the full cooldown."""
    fusion = state_module.FusionAnalysisState(location_id=SITE_B)
    get_ids, send = _patch_state_send(monkeypatch, ids={uuid.uuid4()})
    send.return_value = 0

    ok = await fusion._send_system_template(NotificationType.CRITICAL, "[CRITICAL]")

    assert ok is None


async def test_auto_notify_arms_retry_backoff_on_zero_delivery(monkeypatch):
    """A push outage must not stamp a fresh dispatch batch on every fusion
    tick (the pre-fix behavior would) nor silence the alert for the whole
    cooldown. Zero-delivery arms the short retry backoff: a second call inside
    the window skips; once it elapses the send retries."""
    fusion = state_module.FusionAnalysisState(location_id=SITE_B)
    get_ids, send = _patch_state_send(monkeypatch, ids={uuid.uuid4()})
    send.return_value = 0
    # monotonic() counts since boot on Linux — the 0 initial stamp suppresses
    # the first send on a fresh CI runner. Backdate it past the cooldown so
    # the first call always sends regardless of uptime.
    fusion._last_critical_notify_time = -state_module.CRITICAL_NOTIFY_COOLDOWN_SECONDS

    await fusion._auto_notify_critical()
    assert send.await_count == 1

    # Inside the backoff window — no new send.
    await fusion._auto_notify_critical()
    assert send.await_count == 1

    # Backoff elapsed — the alert retries rather than waiting out the full
    # CRITICAL cooldown.
    fusion._last_critical_notify_time -= state_module.NOTIFY_RETRY_SECONDS
    await fusion._auto_notify_critical()
    assert send.await_count == 2


# ---------------------------------------------------------------------------
# Activation — auto-join the site's own "All Active Responders" group
# ---------------------------------------------------------------------------

APP_MODULE = "app.services.responder.responder_app_service"


def _verify_patches(responder):
    record = MagicMock(
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        otp_hash="hash",
        attempt_count=0,
    )
    group = MagicMock()
    group.id = 42
    return (
        patch(
            f"{APP_MODULE}.responder_otp_verification_crud",
            **{
                "get_by_responder_id": AsyncMock(return_value=record),
                "delete_by_responder_id": AsyncMock(),
            },
        ),
        patch(f"{APP_MODULE}.verify_otp_hash", MagicMock(return_value=True)),
        patch(f"{APP_MODULE}.create_responder_token", MagicMock(return_value="tok")),
        patch(
            f"{APP_MODULE}.responder_crud",
            **{
                "get": AsyncMock(return_value=responder),
                "activate": AsyncMock(),
            },
        ),
        patch(
            f"{APP_MODULE}.responder_group_crud",
            **{
                "ensure_exists": AsyncMock(return_value=group),
                "add_member": AsyncMock(),
            },
        ),
        patch(
            f"{APP_MODULE}.location_crud",
            **{
                "get": AsyncMock(return_value=MagicMock(is_active=True)),
                "get_for_share": AsyncMock(return_value=MagicMock(is_active=True)),
            },
        ),
    )


async def test_pending_responder_activation_joins_home_site_group():
    responder_id = uuid.uuid4()
    responder = MagicMock(status=ResponderStatus.PENDING, location_id=SITE_B)
    otp_crud, otp_hash, token, r_crud, g_crud, _loc_crud = _verify_patches(responder)

    with otp_crud, otp_hash, token, r_crud as r_mock, g_crud as g_mock, _loc_crud:
        result = await responder_app_service.verify_otp(
            verify_request=ResponderOTPVerifyRequest(
                responder_id=responder_id, otp="123456"
            ),
            db=AsyncMock(),
        )

    assert result.success is True
    g_mock.ensure_exists.assert_awaited_once()
    ensure_kwargs = g_mock.ensure_exists.call_args.kwargs
    assert ensure_kwargs["name"] == DEFAULT_ACTIVE_RESPONDERS_GROUP_NAME
    # Home site, not NULL — the legacy global group is never auto-joined.
    assert ensure_kwargs["location_id"] == SITE_B
    g_mock.add_member.assert_awaited_once()
    member_kwargs = g_mock.add_member.call_args.kwargs
    assert member_kwargs["group_id"] == 42
    assert member_kwargs["responder_id"] == responder_id
    r_mock.activate.assert_awaited_once()


async def test_active_responder_relogin_skips_group_join():
    responder = MagicMock(status=ResponderStatus.ACTIVE, location_id=SITE_B)
    otp_crud, otp_hash, token, r_crud, g_crud, _loc_crud = _verify_patches(responder)

    with otp_crud, otp_hash, token, r_crud as r_mock, g_crud as g_mock, _loc_crud:
        result = await responder_app_service.verify_otp(
            verify_request=ResponderOTPVerifyRequest(
                responder_id=uuid.uuid4(), otp="123456"
            ),
            db=AsyncMock(),
        )

    assert result.success is True
    g_mock.ensure_exists.assert_not_called()
    g_mock.add_member.assert_not_called()
    r_mock.activate.assert_not_called()


async def test_ensure_exists_resolves_and_creates_per_site_group():
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    with patch.object(
        responder_group_crud,
        "get_by_name_and_location",
        new=AsyncMock(return_value=None),
    ) as lookup:
        group = await responder_group_crud.ensure_exists(
            db=db, name=DEFAULT_ACTIVE_RESPONDERS_GROUP_NAME, location_id=SITE_B
        )

    assert lookup.call_args.kwargs["location_id"] == SITE_B
    assert group.location_id == SITE_B
    db.add.assert_called_once_with(group)
    db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Payloads — dispatch-bearing responses carry location_id
# ---------------------------------------------------------------------------


async def test_responder_alert_items_carry_dispatch_location():
    now = datetime.now(timezone.utc)
    delivery = MagicMock()
    delivery.id = uuid.uuid4()
    delivery.sent_at = now
    delivery.acknowledgement = None
    delivery.dispatch.id = 55
    delivery.dispatch.type = NotificationType.BLOCKAGE
    delivery.dispatch.title = "t"
    delivery.dispatch.message = "m"
    delivery.dispatch.location_id = SITE_B
    delivery.dispatch.claimed_by = None
    delivery.dispatch.claimed_by_responder = None
    delivery.dispatch.claimed_at = None

    responder = MagicMock()
    responder.active_location_id = SITE_B
    responder.location_id = SITE_A
    with (
        patch(
            f"{APP_MODULE}.notification_delivery_crud",
            **{
                "get_alerts_per_responder": AsyncMock(return_value=([delivery], False))
            },
        ),
        patch(
            f"{APP_MODULE}.responder_crud",
            **{"get": AsyncMock(return_value=responder)},
        ),
    ):
        res = await responder_app_service.get_responder_alerts(
            responder_id=uuid.uuid4(), db=AsyncMock()
        )

    assert res.items[0].location_id == SITE_B


async def test_responder_alerts_scoped_to_effective_site():
    # Active-site wins over home — the CRUD filter gets the deployment site.
    responder = MagicMock()
    responder.active_location_id = SITE_B
    responder.location_id = SITE_A
    crud_del = AsyncMock(return_value=([], False))
    with (
        patch(
            f"{APP_MODULE}.notification_delivery_crud",
            **{"get_alerts_per_responder": crud_del},
        ),
        patch(
            f"{APP_MODULE}.responder_crud",
            **{"get": AsyncMock(return_value=responder)},
        ),
    ):
        await responder_app_service.get_responder_alerts(
            responder_id=uuid.uuid4(), db=AsyncMock()
        )

    assert crud_del.await_args.kwargs["effective_location_id"] == SITE_B


async def test_responder_alerts_home_site_when_no_active():
    responder = MagicMock()
    responder.active_location_id = None
    responder.location_id = SITE_A
    crud_del = AsyncMock(return_value=([], False))
    with (
        patch(
            f"{APP_MODULE}.notification_delivery_crud",
            **{"get_alerts_per_responder": crud_del},
        ),
        patch(
            f"{APP_MODULE}.responder_crud",
            **{"get": AsyncMock(return_value=responder)},
        ),
    ):
        await responder_app_service.get_responder_alerts(
            responder_id=uuid.uuid4(), db=AsyncMock()
        )

    assert crud_del.await_args.kwargs["effective_location_id"] == SITE_A


async def test_delivery_log_items_carry_dispatch_location():
    now = datetime.now(timezone.utc)
    delivery = MagicMock()
    delivery.id = uuid.uuid4()
    delivery.status = DeliveryStatus.SENT
    delivery.sent_at = now
    delivery.error_message = None
    delivery.created_at = now
    delivery.acknowledgement = None
    delivery.dispatch.type = NotificationType.CRITICAL
    delivery.dispatch.title = "t"
    delivery.dispatch.message = "m"
    delivery.dispatch.created_at = now
    delivery.dispatch.location_id = SITE_A

    with patch(
        "app.services.notification_log_service.notification_log_crud",
        **{
            "get_deliveries_for_responder": AsyncMock(
                return_value=([delivery], False)
            )
        },
    ):
        res = await notification_log_service.get_responder_deliveries(
            db=AsyncMock(), responder_id=uuid.uuid4()
        )

    assert res.items[0].location_id == SITE_A


async def test_verify_otp_rejects_activation_into_inactive_home_site():
    """OTP for a PENDING responder whose home site was deactivated → 409, no
    group join, no activation."""
    import pytest
    from fastapi import HTTPException

    responder = MagicMock(status=ResponderStatus.PENDING, location_id=SITE_B)
    otp_crud, otp_hash, token, r_crud, g_crud, loc_crud = _verify_patches(responder)

    with otp_crud, otp_hash, token, r_crud as r_mock, g_crud as g_mock, loc_crud as loc_mock:
        loc_mock.get.return_value = MagicMock(is_active=False)
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.verify_otp(
                verify_request=ResponderOTPVerifyRequest(
                    responder_id=uuid.uuid4(), otp="123456"
                ),
                db=AsyncMock(),
            )

    assert exc.value.status_code == 409
    g_mock.ensure_exists.assert_not_called()
    r_mock.activate.assert_not_awaited()


async def test_responder_details_lists_open_deployments_only():
    """The presence picker reads `deployments` — open ones only."""
    responder_id = uuid.uuid4()
    responder = MagicMock(
        id=responder_id,
        first_name="A",
        last_name="B",
        status=ResponderStatus.ACTIVE,
        phone_number="0917",
        location_id=SITE_A,
        active_location_id=None,
        active_location=None,
        created_at=datetime.now(timezone.utc),
        activated_at=None,
    )
    responder.location.name = "Site A"
    open_dep = MagicMock(
        id=7, location_id=SITE_B, starts_at=datetime.now(timezone.utc),
        ends_at=None, planned_ends_at=None, created_by=uuid.uuid4(),
    )
    open_dep.location.name = "Site B"
    closed_dep = MagicMock(
        id=8, location_id=SITE_B, starts_at=datetime.now(timezone.utc),
        ends_at=datetime.now(timezone.utc), planned_ends_at=None, created_by=uuid.uuid4(),
    )

    with (
        patch(
            f"{APP_MODULE}.responder_crud",
            **{"get_responder_details_for_app": AsyncMock(return_value=responder)},
        ),
        patch(
            f"{APP_MODULE}.responder_deployment_crud",
            **{"list_for_responder": AsyncMock(return_value=[open_dep, closed_dep])},
        ),
    ):
        details = await responder_app_service.get_responder_details_for_app(
            responder_id=responder_id, db=AsyncMock()
        )

    assert [d.id for d in details.deployments] == [7]
    assert details.deployments[0].location_name == "Site B"
