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

import asyncio
import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests.exceptions
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


def _endpoint_patch(sent=5, side_effect=None):
    """The endpoint is thin: the site gate + audience live in the service."""
    return patch(
        f"{PUSH_MOD}.notification_service",
        **{"send_alarm": AsyncMock(return_value=sent, side_effect=side_effect)},
    )


def test_send_alarm_endpoint_dispatches_to_site(admin_client):
    with _endpoint_patch(sent=3) as svc_p:
        res = admin_client.post(
            "/api/v1/push/send-alarm",
            json={"location_id": SITE_A, "title": "FLOOD ALARM", "message": "Evacuate now"},
        )

    assert res.status_code == 200
    assert res.json() == {"sent_count": 3}
    svc_mock = svc_p.send_alarm
    svc_mock.assert_awaited_once()
    kwargs = svc_mock.call_args.kwargs
    assert kwargs["location_id"] == SITE_A
    assert kwargs["title"] == "FLOOD ALARM"
    assert kwargs["message"] == "Evacuate now"


def test_send_alarm_null_site_is_barangay_wide(admin_client):
    with _endpoint_patch() as svc_p:
        res = admin_client.post(
            "/api/v1/push/send-alarm",
            json={"title": "PROVINCE ALARM", "message": "All sites"},
        )

    assert res.status_code == 200
    assert svc_p.send_alarm.call_args.kwargs["location_id"] is None


def test_send_alarm_service_gate_errors_propagate(admin_client):
    """The 404/400 site gate lives in the service — the endpoint just
    surfaces its errors."""
    with _endpoint_patch(side_effect=HTTPException(status_code=404, detail="Site not found.")) as svc_p:
        res = admin_client.post(
            "/api/v1/push/send-alarm",
            json={"location_id": 999, "title": "t", "message": "m"},
        )

    assert res.status_code == 404
    svc_p.send_alarm.assert_awaited_once()


def test_send_alarm_zero_audience_is_400(admin_client):
    """An alarm fired at nobody must surface — the service raises the 400,
    the endpoint just propagates it."""
    with _endpoint_patch(
        side_effect=HTTPException(
            status_code=400, detail="No push-subscribed responders for this site"
        )
    ):
        res = admin_client.post(
            "/api/v1/push/send-alarm",
            json={"location_id": SITE_A, "title": "t", "message": "m"},
        )

    assert res.status_code == 400


def test_send_alarm_zero_delivered_is_200(admin_client):
    """Audience resolved but every push failed → an honest sent_count of 0,
    not a phantom count — the delivery rows carry the per-responder failures."""
    with _endpoint_patch(sent=0):
        res = admin_client.post(
            "/api/v1/push/send-alarm",
            json={"location_id": SITE_A, "title": "t", "message": "m"},
        )

    assert res.status_code == 200
    assert res.json() == {"sent_count": 0}


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
                "get_reachable_ids": AsyncMock(
                    side_effect=lambda db, responder_ids: [
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
        patch(
            "app.services.location_service.require_active_site",
            AsyncMock(),
        ),
    )


async def test_send_alarm_creates_alarm_tier_dispatch_scoped_to_site():
    at_site = uuid.uuid4()
    crud_r, crud_d, crud_sub, crud_del, gate = _alarm_patches({at_site})

    with crud_r as r_mock, crud_d as d_mock, crud_sub, crud_del as del_mock, gate as gate_mock:
        sent = await notification_service.send_alarm(
            db=AsyncMock(),
            location_id=SITE_A,
            title="FLOOD ALARM",
            message="Evacuate now",
        )

    # The active-site gate is the service's own — a future autonomous
    # trigger inherits it.
    gate_mock.assert_awaited_once()
    assert gate_mock.call_args.kwargs["location_id"] == SITE_A
    # Audience resolved to 1, but this fixture has no subscription rows —
    # zero responders actually reached.
    assert sent == 0

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
    crud_r, crud_d, crud_sub, crud_del, gate = _alarm_patches(ids)

    with crud_r as r_mock, crud_d as d_mock, crud_sub, crud_del as del_mock, gate as gate_mock:
        sent = await notification_service.send_alarm(
            db=AsyncMock(), location_id=None, title="t", message="m"
        )

    # NULL site: no gate call — barangay-wide needs no site check.
    gate_mock.assert_not_called()
    # Same as above — resolved audience 2, zero subscriptions → 0 reached.
    assert sent == 0
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
    crud_r, crud_d, crud_sub, crud_del, gate = _alarm_patches(set())

    with crud_r, crud_d as d_mock, crud_sub as sub_mock, crud_del as del_mock, gate:
        with pytest.raises(HTTPException) as exc:
            await notification_service.send_alarm(
                db=AsyncMock(), location_id=SITE_B, title="t", message="m"
            )

    assert exc.value.status_code == 400
    d_mock.create_for_send.assert_not_called()
    sub_mock.get_by_responder_ids.assert_not_called()
    del_mock.upsert_many_results.assert_not_called()


async def test_send_alarm_inactive_site_raises_before_resolving():
    """The service-level gate is the guard for every caller, including the
    deferred autonomous trigger — an alarm to an inactive site never
    reaches the resolver."""
    crud_r, crud_d, crud_sub, crud_del, gate = _alarm_patches({uuid.uuid4()})

    with crud_r as r_mock, crud_d, crud_sub, crud_del, patch(
        "app.services.location_service.require_active_site",
        AsyncMock(side_effect=HTTPException(status_code=400, detail="Site is inactive.")),
    ):
        with pytest.raises(HTTPException) as exc:
            await notification_service.send_alarm(
                db=AsyncMock(), location_id=SITE_B, title="t", message="m"
            )

    assert exc.value.status_code == 400
    r_mock.get_responder_ids_with_push_subscription.assert_not_called()


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
    assert webpush_mock.call_args.kwargs["timeout"] == 10
    data = json.loads(webpush_mock.call_args.kwargs["data"])
    assert data["type"] == NotificationType.ALARM.value


async def test_send_push_transport_error_returns_failed():
    """pywebpush lets requests transport errors propagate unwrapped — and a
    corrupt stored key raises during payload encryption before any POST. Both
    must fail their delivery row, not abort fan-out."""
    subscription = MagicMock(endpoint="https://push.example/x", p256dh="k", auth="a")

    for exc in (
        requests.exceptions.ConnectTimeout("stale endpoint"),
        ValueError("malformed p256dh"),
    ):
        with patch(f"{NS_MODULE}.webpush", side_effect=exc):
            result = await notification_service.send_push(
                subscription=subscription,
                notif_title="t",
                notif_message="m",
                notif_type=NotificationType.ALARM,
            )

        assert result == "failed", exc


async def test_send_push_does_not_block_event_loop():
    """webpush is a sync requests.post — inside send_alarm it runs while the
    FOR SHARE site lock is held. It must run off the event loop so other
    requests aren't stalled behind N x up-to-10s push calls."""
    subscription = MagicMock(endpoint="https://push.example/x", p256dh="k", auth="a")

    def slow_webpush(**kwargs):
        time.sleep(0.2)
        return MagicMock(status_code=201)

    with patch(f"{NS_MODULE}.webpush", side_effect=slow_webpush):
        task = asyncio.create_task(
            notification_service.send_push(
                subscription=subscription,
                notif_title="t",
                notif_message="m",
                notif_type=NotificationType.ALARM,
            )
        )
        ticks = 0
        while not task.done():
            await asyncio.sleep(0.01)
            ticks += 1

    assert await task == "sent"
    # ~20 ticks expected off-loop; a blocking call yields ~0-1.
    assert ticks > 3


# ---------------------------------------------------------------------------
# Review-round fixes — escalation keeps the tier, audience filters apply
# ---------------------------------------------------------------------------


async def test_escalation_keeps_alarm_tier():
    """An unacknowledged ALARM escalates as an ALARM — downgrading the ping
    to CRITICAL strips the takeover render in the responder app."""
    from app.core.escalation import escalation_check_job

    delivery = MagicMock()
    delivery.dispatch.location_id = SITE_A
    delivery.dispatch.type = NotificationType.ALARM
    delivery.responder_id = uuid.uuid4()
    delivery.id = 1

    db = AsyncMock()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=db)
    session.__aexit__ = AsyncMock(return_value=False)

    settings_vals = {"escalation_timeout_minutes": "10", "max_escalation_count": "3"}

    with (
        patch("app.core.escalation.AsyncSessionLocal", return_value=session),
        patch(
            "app.crud.system_settings.system_settings_crud",
            **{"get_value": AsyncMock(side_effect=lambda db, key: settings_vals[key])},
        ),
        patch(
            "app.crud.notification_delivery.notification_delivery_crud",
            **{
                "get_unacknowledged_critical_past_threshold": AsyncMock(
                    return_value=[delivery]
                ),
                "increment_escalation_count": AsyncMock(),
            },
        ),
        patch(
            "app.crud.location.location_crud",
            **{"get": AsyncMock(return_value=MagicMock(name="Site A"))},
        ),
        patch(
            "app.services.notification_service.NotificationService.send_notification_to_subscribers",
            AsyncMock(),
        ) as send_mock,
    ):
        await escalation_check_job()

    payload = send_mock.call_args.kwargs["payload"]
    assert payload.custom_notification.type == NotificationType.ALARM
    assert "Alarm" in payload.custom_notification.title
    assert send_mock.call_args.kwargs["location_id"] == SITE_A


async def test_fanout_resolver_excludes_inactive_and_suspended():
    """The shared audience resolver must join Responder + Location and filter
    on responder status and site is_active — otherwise a barangay-wide alarm
    reaches suspended responders and responders at deactivated sites."""
    from app.crud import responder_crud

    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(all=MagicMock(return_value=[]))
    )

    await responder_crud.get_responder_ids_with_push_subscription(db=db)

    sql = str(db.execute.call_args.args[0])
    assert "JOIN" in sql.upper()
    assert "status" in sql
    assert "is_active" in sql


async def test_send_alarm_returns_delivered_count():
    """sent_count counts responders a push actually reached — the resolver's
    audience only bounds it."""
    at_site = uuid.uuid4()
    crud_r, crud_d, crud_sub, crud_del, gate = _alarm_patches({at_site})
    subscription = MagicMock(
        responder_id=at_site, id=uuid.uuid4(),
        endpoint="https://push.example/x", p256dh="k", auth="a",
    )

    with (
        crud_r, crud_d, crud_sub as sub_mock, crud_del, gate,
        patch(f"{NS_MODULE}.webpush"),
    ):
        sub_mock.get_by_responder_ids.return_value = [subscription]
        sent = await notification_service.send_alarm(
            db=AsyncMock(), location_id=SITE_A, title="t", message="m"
        )

    assert sent == 1


async def test_site_refilter_keeps_audience_hygiene():
    """The send path's re-filters — site-scoped and barangay-wide — are what
    escalation re-sends go through, so both must apply the same status +
    active-site filters as the resolver or a suspended responder keeps
    getting takeover pings."""
    from app.crud import responder_crud

    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    await responder_crud.get_ids_in_effective_location(
        db=db, responder_ids=[uuid.uuid4()], location_id=SITE_A
    )
    await responder_crud.get_reachable_ids(db=db, responder_ids=[uuid.uuid4()])

    for call in db.execute.call_args_list:
        sql = str(call.args[0])
        assert "JOIN" in sql.upper()
        assert "status" in sql
        assert "is_active" in sql


async def test_admin_list_badge_uses_unfiltered_lookup():
    """has_push_subscription reports a stored subscription, not current
    reachability — the badge must not go dark just because a site was
    deactivated."""
    from app.services.responder.responder_service import responder_service

    with patch(
        "app.services.responder.responder_service.responder_crud",
        **{
            "get_all": AsyncMock(return_value=[]),
            "get_ids_with_push_subscription": AsyncMock(return_value=set()),
            "get_responder_ids_with_push_subscription": AsyncMock(return_value=set()),
        },
    ) as r_mock:
        await responder_service.get_all_responders(db=AsyncMock())

    r_mock.get_ids_with_push_subscription.assert_awaited_once()
    r_mock.get_responder_ids_with_push_subscription.assert_not_called()


def test_alarm_cannot_be_created_as_template():
    """Until the autonomous trigger path exists, an ALARM template would be
    undeletable and would silently become the system template."""
    from pydantic import ValidationError

    from app.schemas.notification_template import CreateNotificationTemplateRequest

    with pytest.raises(ValidationError):
        CreateNotificationTemplateRequest(
            type=NotificationType.ALARM, title="t", message="m"
        )
