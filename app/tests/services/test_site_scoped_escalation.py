"""Tests for T4 — escalation re-sends scoped per site.

Same seam style as test_site_scoped_fanout.py: escalation_check_job defers its
imports into the function body, so the patch targets are the singleton objects
those imports resolve to (patched via patch.object, like T3's monkeypatch
style). AsyncSessionLocal lives at module scope in app.core.escalation.

Site rule: overdue deliveries are grouped by their dispatch's location_id; each
group re-notifies only that site's responders (location_id passed to
send_notification_to_subscribers, which applies the effective-site filter). A
NULL-site dispatch escalates to everyone, as before.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.core.escalation as escalation_module
from app.core.escalation import escalation_check_job
from app.crud import (
    location_crud,
    notification_delivery_crud,
    system_settings_crud,
)
from app.models.notification_template import NotificationType
from app.services import notification_service

SITE_A = 1
SITE_B = 2


def _delivery(responder_id, location_id, notif_type=NotificationType.CRITICAL):
    d = MagicMock()
    d.id = uuid.uuid4()
    d.responder_id = responder_id
    d.dispatch.location_id = location_id
    d.dispatch.type = notif_type
    return d


def _settings_get_value(db, key):
    return {
        "escalation_timeout_minutes": "10",
        "max_escalation_count": "3",
    }[key]


def _location_get(db, id):
    return SimpleNamespace(id=id, name=f"Site {id}")


def _patches(overdue):
    """Patch the seams around escalation_check_job's deferred imports."""
    fake_db = AsyncMock()
    fake_session = MagicMock()
    fake_session.__aenter__.return_value = fake_db
    return (
        patch.object(
            escalation_module, "AsyncSessionLocal", new=lambda: fake_session
        ),
        patch.object(
            system_settings_crud,
            "get_value",
            new=AsyncMock(side_effect=_settings_get_value),
        ),
        patch.object(
            notification_delivery_crud,
            "get_unacknowledged_critical_past_threshold",
            new=AsyncMock(return_value=overdue),
        ),
        patch.object(
            notification_delivery_crud,
            "increment_escalation_count",
            new=AsyncMock(),
        ),
        patch.object(
            location_crud,
            "get",
            new=AsyncMock(side_effect=_location_get),
        ),
        patch.object(
            notification_service,
            "send_notification_to_subscribers",
            new=AsyncMock(),
        ),
    )


async def test_escalation_sends_one_notification_per_site():
    r1, r2, r3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    overdue = [
        _delivery(r1, SITE_A),
        _delivery(r2, SITE_A),
        _delivery(r3, SITE_B),
    ]
    session, settings_p, unacked, increment, loc_get, send = _patches(overdue)

    with (
        session,
        settings_p,
        unacked,
        increment as increment_mock,
        loc_get,
        send as send_mock,
    ):
        await escalation_check_job()

    assert send_mock.await_count == 2
    sends_by_site = {
        call.kwargs["location_id"]: call for call in send_mock.await_args_list
    }
    assert set(sends_by_site) == {SITE_A, SITE_B}

    # Each send stays scoped to its site and names the site in the message.
    site_a_call = sends_by_site[SITE_A]
    assert set(site_a_call.kwargs["payload"].responder_ids) == {r1, r2}
    assert site_a_call.kwargs["system_initiated"] is True
    assert "Site 1" in site_a_call.kwargs["payload"].custom_notification.message

    site_b_call = sends_by_site[SITE_B]
    assert site_b_call.kwargs["payload"].responder_ids == [r3]
    assert "Site 2" in site_b_call.kwargs["payload"].custom_notification.message

    # Every overdue delivery's escalation count is bumped, regardless of site.
    assert increment_mock.await_count == 3


async def test_escalation_dedups_responder_within_site_not_across_sites():
    # Same responder owes two alerts at site A and one at site B: one ping in
    # each site group — different sites are different alerts, so the dedup key
    # is (site, responder), not responder alone.
    r1 = uuid.uuid4()
    overdue = [
        _delivery(r1, SITE_A),
        _delivery(r1, SITE_A),
        _delivery(r1, SITE_B),
    ]
    session, settings_p, unacked, increment, loc_get, send = _patches(overdue)

    with session, settings_p, unacked, increment, loc_get, send as send_mock:
        await escalation_check_job()

    assert send_mock.await_count == 2
    sends_by_site = {
        call.kwargs["location_id"]: call for call in send_mock.await_args_list
    }
    assert sends_by_site[SITE_A].kwargs["payload"].responder_ids == [r1]
    assert sends_by_site[SITE_B].kwargs["payload"].responder_ids == [r1]


async def test_escalation_null_site_dispatch_escalates_to_everyone():
    # NULL-site dispatches (announcements) keep the old behaviour: one send
    # with location_id=None, which skips the effective-site filter entirely.
    r1, r2 = uuid.uuid4(), uuid.uuid4()
    overdue = [_delivery(r1, None), _delivery(r2, None)]
    session, settings_p, unacked, increment, loc_get, send = _patches(overdue)

    with (
        session,
        settings_p,
        unacked,
        increment,
        loc_get as loc_get_mock,
        send as send_mock,
    ):
        await escalation_check_job()

    send_mock.assert_awaited_once()
    call = send_mock.await_args
    assert call.kwargs["location_id"] is None
    assert set(call.kwargs["payload"].responder_ids) == {r1, r2}
    # No site to name — no location lookup, and the legacy message is kept.
    loc_get_mock.assert_not_called()
    assert (
        call.kwargs["payload"].custom_notification.message
        == "You have unacknowledged critical alerts. Please review and respond immediately."
    )
