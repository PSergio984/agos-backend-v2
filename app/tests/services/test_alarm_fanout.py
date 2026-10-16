"""Tests for T8 — alarm tier on the dispatch pipeline (decision record #21).

Same seams as test_site_scoped_fanout.py / test_site_scoped_escalation.py:
TestClient WITHOUT a context manager for the endpoint contract, CRUD
singletons patched at app.services.notification_service / endpoint module
scope for the service path.

Alarm invariants under test (#21):
- An alarm materializes as a dispatch carrying location_id (NULL =
  barangay-wide) — the existing pipeline, never a parallel channel.
- Fan-out = the site's responders via active presence — the same resolver the
  fusion path uses (push-subscribed responders at COALESCE(active, home)).
- Deliveries/escalation treat ALARM as critical-or-above.
- The push payload carries the tier so the app can render the takeover UI.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.crud import notification_delivery_crud
from app.models.notification_template import NotificationType
from app.schemas.subscription import CustomNotificationPayload, SendNotificationSchema
from app.services.notification_service import notification_service

SITE_A = 1
SITE_B = 2

PUSH_MOD = "app.api.v1.endpoints.push"
NS_MODULE = "app.services.notification_service"


# ---------------------------------------------------------------------------
# Endpoint contract — POST /push/send-alarm
# ---------------------------------------------------------------------------


def _endpoint_patches(site):
    return (
        patch(
            f"{PUSH_MOD}.location_crud",
            **{"get_for_share": AsyncMock(return_value=site)},
        ),
        patch(
            f"{PUSH_MOD}.notification_service",
            **{"send_alarm": AsyncMock()},
        ),
    )


def test_send_alarm_endpoint_dispatches_to_site(admin_client):
    site = MagicMock(is_active=True)
    loc_p, svc_p = _endpoint_patches(site)

    with loc_p, svc_p as svc_mock:
        res = admin_client.post(
            "/api/v1/push/send-alarm",
            json={"location_id": SITE_A, "title": "FLOOD ALARM", "message": "Evacuate now"},
        )

    assert res.status_code == 204
    svc_mock.send_alarm.assert_awaited_once()
    kwargs = svc_mock.send_alarm.call_args.kwargs
    assert kwargs["location_id"] == SITE_A
    assert kwargs["title"] == "FLOOD ALARM"
    assert kwargs["message"] == "Evacuate now"


def test_send_alarm_null_site_is_barangay_wide(admin_client):
    loc_p, svc_p = _endpoint_patches(MagicMock(is_active=True))

    with loc_p as loc_mock, svc_p as svc_mock:
        res = admin_client.post(
            "/api/v1/push/send-alarm",
            json={"title": "PROVINCE ALARM", "message": "All sites"},
        )

    assert res.status_code == 204
    loc_mock.get_for_share.assert_not_called()
    assert svc_mock.send_alarm.call_args.kwargs["location_id"] is None


def test_send_alarm_unknown_site_404(admin_client):
    loc_p, svc_p = _endpoint_patches(None)

    with loc_p, svc_p as svc_mock:
        res = admin_client.post(
            "/api/v1/push/send-alarm",
            json={"location_id": 999, "title": "t", "message": "m"},
        )

    assert res.status_code == 404
    svc_mock.send_alarm.assert_not_called()


def test_send_alarm_inactive_site_400(admin_client):
    loc_p, svc_p = _endpoint_patches(MagicMock(is_active=False))

    with loc_p, svc_p as svc_mock:
        res = admin_client.post(
            "/api/v1/push/send-alarm",
            json={"location_id": SITE_A, "title": "t", "message": "m"},
        )

    assert res.status_code == 400
    svc_mock.send_alarm.assert_not_called()


# ---------------------------------------------------------------------------
# Service — send_alarm rides the dispatch fan-out path with the ALARM tier
# ---------------------------------------------------------------------------


def _alarm_patches(site_ids):
    """Patch the CRUD seams around send_alarm → send_notification_to_subscribers.

    site_ids is what get_responder_ids_with_push_subscription returns — the
    push-subscribed responders whose effective site is the alarm site. The
    inner effective-site filter is made honest: it returns only candidates
    that were in site_ids.
    """
    dispatch = MagicMock()
    dispatch.id = 99
    return (
        patch(
            f"{NS_MODULE}.responder_crud",
            **{
                "get_responder_ids_with_push_subscription": AsyncMock(
                    return_value=site_ids
                ),
                "get_ids_in_effective_location": AsyncMock(
                    side_effect=lambda db, responder_ids, location_id: [
                        i for i in responder_ids if i in site_ids
                    ]
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
            f"{NS_MODULE}.notification_delivery_crud",
            **{"upsert_many_results": AsyncMock()},
        ),
    )


async def test_send_alarm_creates_alarm_tier_dispatch_scoped_to_site():
    at_site = uuid.uuid4()
    crud_r, crud_d, crud_sub, crud_del = _alarm_patches({at_site})

    with crud_r as r_mock, crud_d as d_mock, crud_sub, crud_del as del_mock:
        await notification_service.send_alarm(
            db=AsyncMock(),
            location_id=SITE_A,
            title="FLOOD ALARM",
            message="Evacuate now",
        )

    # Audience resolves via active presence at the alarm site — the same
    # resolver the fusion/auto-notify path uses.
    resolver = r_mock.get_responder_ids_with_push_subscription
    resolver.assert_awaited_once()
    assert resolver.call_args.kwargs["location_id"] == SITE_A

    # The dispatch is the alarm — same pipeline, tier stamped on the row.
    create_kwargs = d_mock.create_for_send.call_args.kwargs
    assert create_kwargs["notif_type"] == NotificationType.ALARM
    assert create_kwargs["location_id"] == SITE_A
    assert create_kwargs["title"] == "FLOOD ALARM"

    # Presence filter still applies inside the send (a responder whose
    # presence moved between resolve and send drops out).
    assert r_mock.get_ids_in_effective_location.call_args.kwargs["location_id"] == SITE_A
    rows = del_mock.upsert_many_results.call_args.kwargs["delivery_rows"]
    assert [row["responder_id"] for row in rows] == [at_site]


async def test_send_alarm_null_site_fans_out_barangay_wide():
    ids = {uuid.uuid4(), uuid.uuid4()}
    crud_r, crud_d, crud_sub, crud_del = _alarm_patches(ids)

    with crud_r as r_mock, crud_d as d_mock, crud_sub, crud_del as del_mock:
        await notification_service.send_alarm(
            db=AsyncMock(), location_id=None, title="t", message="m"
        )

    assert (
        r_mock.get_responder_ids_with_push_subscription.call_args.kwargs["location_id"]
        is None
    )
    # NULL site: no effective-site filter — the barangay-wide path.
    r_mock.get_ids_in_effective_location.assert_not_called()
    assert d_mock.create_for_send.call_args.kwargs["location_id"] is None
    rows = del_mock.upsert_many_results.call_args.kwargs["delivery_rows"]
    assert {row["responder_id"] for row in rows} == ids


async def test_send_alarm_no_site_responders_creates_no_dispatch():
    crud_r, crud_d, crud_sub, crud_del = _alarm_patches(set())

    with crud_r, crud_d as d_mock, crud_sub as sub_mock, crud_del as del_mock:
        await notification_service.send_alarm(
            db=AsyncMock(), location_id=SITE_B, title="t", message="m"
        )

    d_mock.create_for_send.assert_not_called()
    sub_mock.get_by_responder_ids.assert_not_called()
    del_mock.upsert_many_results.assert_not_called()


async def test_manual_send_cannot_claim_alarm_tier():
    """The generic send endpoint stays announcement-only — an alarm enters
    only through the dedicated path, never by smuggling the type into a
    manual send."""
    payload = SendNotificationSchema(
        responder_ids=[uuid.uuid4()],
        custom_notification=CustomNotificationPayload(
            type=NotificationType.ALARM, title="t", message="m"
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await notification_service._resolve_notification_content(
            payload=payload, db=AsyncMock(), system_initiated=False
        )

    assert exc.value.status_code == 400


async def test_alarm_counts_as_critical_or_above_for_escalation():
    """The escalation query must pick up unacknowledged ALARM deliveries,
    not only CRITICAL."""
    result = MagicMock()
    result.scalars.return_value.unique.return_value.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    await notification_delivery_crud.get_unacknowledged_critical_past_threshold(
        db=db, timeout_minutes=10, max_escalation=3
    )

    stmt = db.execute.call_args.args[0]
    params = set()
    for value in stmt.compile().params.values():
        # in_() compiles to one expanding param holding the list.
        params.update(value if isinstance(value, list) else [value])
    assert NotificationType.CRITICAL in params
    assert NotificationType.ALARM in params


async def test_push_payload_marks_alarm_tier():
    """The app renders the takeover UI off the push itself — the data payload
    must carry the tier, not just title/message."""
    subscription = MagicMock(endpoint="https://push.example/x", p256dh="k", auth="a")

    with patch(f"{NS_MODULE}.webpush") as webpush_mock:
        result = await notification_service.send_push(
            subscription=subscription,
            notif_title="FLOOD ALARM",
            notif_message="Evacuate now",
            notif_type=NotificationType.ALARM,
        )

    assert result == "sent"
    data = json.loads(webpush_mock.call_args.kwargs["data"])
    assert data["type"] == NotificationType.ALARM.value
