"""Tests for #33 — Expo push path (Phase A of the responder app's audible
alarm layer).

Same seams as test_alarm_fanout.py / test_site_scoped_fanout.py:
- endpoint contract: TestClient WITHOUT a context manager, dependency
  overrides for get_db + responder auth, service methods patched with
  AsyncMock.
- service logic: CRUD singletons patched at the service module scope;
  httpx.AsyncClient patched at app.services.notification_service.

Invariants under test (#33):
- POST /push/subscribe accepts the Expo shape ({token, platform,
  responderId}) next to the unchanged VAPID shape, keeps the 403
  responder-id check, and dedupes on (responder_id, token).
- send_expo_push maps Expo tickets: ok → SENT, DeviceNotRegistered → GONE,
  any other error/non-200/transport failure → FAILED, never aborting the
  fan-out.
- ALARM payloads carry channelId "alarm" + sound "siren.wav" +
  interruptionLevel "timeSensitive"; other types stay on the default
  channel. data.type is always populated.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.v1.dependencies import CurrentResponder, require_responder_auth
from app.core.database import get_db
from app.crud import push_token_crud
from app.main import app
from app.models.notification_template import NotificationType
from app.models.responder_related.notification_delivery import DeliveryStatus
from app.schemas.subscription import (
    CustomNotificationPayload,
    ExpoSubscriptionSchema,
    SendNotificationSchema,
)
from app.services import push_subscription_service
from app.services.notification_service import (
    EXPO_PUSH_URL,
    NotificationService,
    PushResult,
    notification_service,
)

RESPONDER_ID = uuid.uuid4()

NS_MODULE = "app.services.notification_service"


# ---------------------------------------------------------------------------
# Endpoint contract — POST /push/subscribe accepts both shapes
# ---------------------------------------------------------------------------


@pytest.fixture()
def responder_client():
    async def _fake_db():
        yield MagicMock()

    app.dependency_overrides[require_responder_auth] = lambda: CurrentResponder(
        id=str(RESPONDER_ID)
    )
    app.dependency_overrides[get_db] = _fake_db
    # No context manager — `with TestClient` runs the app lifespan, which
    # opens a real DB connection. These tests only exercise the endpoint seam.
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_subscribe_accepts_expo_token_shape(responder_client):
    with (
        patch.object(push_subscription_service, "subscribe_expo", new=AsyncMock()) as expo_mock,
        patch.object(push_subscription_service, "subscribe", new=AsyncMock()) as web_mock,
    ):
        res = responder_client.post(
            "/api/v1/push/subscribe",
            json={
                "token": "ExponentPushToken[abc]",
                "platform": "android",
                "responderId": str(RESPONDER_ID),
            },
        )

    assert res.status_code == 204
    expo_mock.assert_awaited_once()
    web_mock.assert_not_awaited()
    data = expo_mock.call_args.kwargs["data"]
    assert data.responder_id == RESPONDER_ID
    assert data.token == "ExponentPushToken[abc]"
    assert data.platform == "android"


def test_subscribe_expo_shape_for_another_responder_is_403(responder_client):
    with patch.object(push_subscription_service, "subscribe_expo", new=AsyncMock()) as expo_mock:
        res = responder_client.post(
            "/api/v1/push/subscribe",
            json={
                "token": "ExponentPushToken[abc]",
                "platform": "ios",
                "responderId": str(uuid.uuid4()),
            },
        )

    assert res.status_code == 403
    expo_mock.assert_not_awaited()


def test_subscribe_webpush_shape_still_works(responder_client):
    with (
        patch.object(push_subscription_service, "subscribe", new=AsyncMock()) as web_mock,
        patch.object(push_subscription_service, "subscribe_expo", new=AsyncMock()) as expo_mock,
    ):
        res = responder_client.post(
            "/api/v1/push/subscribe",
            json={
                "endpoint": "https://push.example/sub",
                "keys": {"p256dh": "k", "auth": "a"},
                "responder_id": str(RESPONDER_ID),
            },
        )

    assert res.status_code == 204
    web_mock.assert_awaited_once()
    expo_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Service — subscribe_expo dedupes on (responder_id, token)
# ---------------------------------------------------------------------------


def _expo_sub(token="ExponentPushToken[x]", responder_id=None):
    return ExpoSubscriptionSchema.model_validate(
        {
            "token": token,
            "platform": "expo",
            "responderId": str(responder_id or uuid.uuid4()),
        }
    )


async def test_subscribe_expo_dedupes_existing_token():
    data = _expo_sub()
    existing = MagicMock()
    existing.responder_id = data.responder_id
    # patch.object on the singleton: "create" is a Mock constructor kwarg, so
    # patch(target, create=...) would swallow it instead of setting the attr.
    with (
        patch.object(
            push_token_crud,
            "get_by_token",
            new=AsyncMock(return_value=existing),
        ) as get_mock,
        patch.object(push_token_crud, "create", new=AsyncMock()) as create_mock,
        patch.object(
            push_token_crud, "reassign", new=AsyncMock()
        ) as reassign_mock,
    ):
        await push_subscription_service.subscribe_expo(data=data, db=AsyncMock())

    assert get_mock.call_args.kwargs["token"] == data.token
    create_mock.assert_not_awaited()
    reassign_mock.assert_not_awaited()


async def test_subscribe_expo_reassigns_token_to_new_responder():
    """A token identifies the install, not the responder — on a shared
    device, subscribing under a different responder must move the row, or
    the previous owner's alarms keep ringing this phone."""
    data = _expo_sub()
    existing = MagicMock()
    existing.responder_id = uuid.uuid4()
    with (
        patch.object(
            push_token_crud,
            "get_by_token",
            new=AsyncMock(return_value=existing),
        ),
        patch.object(push_token_crud, "create", new=AsyncMock()) as create_mock,
        patch.object(
            push_token_crud, "reassign", new=AsyncMock()
        ) as reassign_mock,
    ):
        await push_subscription_service.subscribe_expo(data=data, db=AsyncMock())

    create_mock.assert_not_awaited()
    reassign_mock.assert_awaited_once()
    assert reassign_mock.call_args.kwargs["responder_id"] == data.responder_id


async def test_subscribe_expo_creates_when_token_is_new():
    data = _expo_sub()
    with (
        patch.object(
            push_token_crud,
            "get_by_token",
            new=AsyncMock(return_value=None),
        ),
        patch.object(push_token_crud, "create", new=AsyncMock()) as create_mock,
    ):
        await push_subscription_service.subscribe_expo(data=data, db=AsyncMock())

    create_mock.assert_awaited_once()
    assert create_mock.call_args.kwargs["data"] is data


# ---------------------------------------------------------------------------
# Service — send_expo_push ticket → PushResult mapping
# ---------------------------------------------------------------------------


def _expo_response(status_code=200, data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data or {}
    return resp


def _httpx_patch(post_mock):
    """Patch the httpx module ref at the service seam; returns (patcher,
    client) where client is what `async with httpx.AsyncClient()` yields."""
    httpx_mock = MagicMock()
    client = httpx_mock.AsyncClient.return_value.__aenter__.return_value
    client.post = post_mock
    return patch(f"{NS_MODULE}.httpx", httpx_mock), client


def _token_row(token="ExponentPushToken[abc]"):
    return MagicMock(token=token)


async def test_expo_send_ok_ticket_is_sent():
    post = AsyncMock(
        return_value=_expo_response(data={"data": {"status": "ok", "id": "t1"}})
    )
    patcher, client = _httpx_patch(post)

    with patcher:
        result = await notification_service.send_expo_push(
            client=client,
            push_token=_token_row(),
            notif_title="t",
            notif_message="m",
            notif_type=NotificationType.CRITICAL,
        )

    assert result == PushResult.SENT
    assert post.call_args.args[0] == EXPO_PUSH_URL
    body = post.call_args.kwargs["json"]
    assert body["to"] == "ExponentPushToken[abc]"
    assert body["title"] == "t"
    assert body["body"] == "m"
    assert body["data"] == {"type": "critical"}


async def test_expo_device_not_registered_is_gone():
    post = AsyncMock(
        return_value=_expo_response(
            data={
                "data": {
                    "status": "error",
                    "message": "gone",
                    "details": {"error": "DeviceNotRegistered"},
                }
            }
        )
    )
    patcher, client = _httpx_patch(post)

    with patcher:
        result = await notification_service.send_expo_push(
            client=client,
            push_token=_token_row(),
            notif_title="t",
            notif_message="m",
            notif_type=NotificationType.CRITICAL,
        )

    assert result == PushResult.GONE


async def test_expo_other_ticket_error_is_failed():
    post = AsyncMock(
        return_value=_expo_response(
            data={
                "data": {
                    "status": "error",
                    "message": "too big",
                    "details": {"error": "MessageTooBig"},
                }
            }
        )
    )
    patcher, client = _httpx_patch(post)

    with patcher:
        result = await notification_service.send_expo_push(
            client=client,
            push_token=_token_row(),
            notif_title="t",
            notif_message="m",
            notif_type=NotificationType.CRITICAL,
        )

    assert result == PushResult.FAILED


async def test_expo_non_200_is_failed():
    post = AsyncMock(return_value=_expo_response(status_code=500))
    patcher, client = _httpx_patch(post)

    with patcher:
        result = await notification_service.send_expo_push(
            client=client,
            push_token=_token_row(),
            notif_title="t",
            notif_message="m",
            notif_type=NotificationType.CRITICAL,
        )

    assert result == PushResult.FAILED


async def test_expo_transport_error_is_failed_and_logged():
    """Same per-device boundary as send_push: a transport/parse failure fails
    this delivery row, never aborts the fan-out — and is logged so a systemic
    failure isn't indistinguishable from stale tokens."""
    post = AsyncMock(side_effect=ValueError("boom"))
    patcher, client = _httpx_patch(post)

    with patcher, patch(f"{NS_MODULE}.logger") as log_mock:
        result = await notification_service.send_expo_push(
            client=client,
            push_token=_token_row(),
            notif_title="t",
            notif_message="m",
            notif_type=NotificationType.CRITICAL,
        )

    assert result == PushResult.FAILED
    log_mock.exception.assert_called_once()


# ---------------------------------------------------------------------------
# Service — alarm-tier payload vs default
# ---------------------------------------------------------------------------


async def test_expo_alarm_payload_carries_siren_channel():
    """The takeover UI's audible layer: ALARM rides the app's "alarm" channel
    with the bundled siren at timeSensitive interruption."""
    post = AsyncMock(return_value=_expo_response(data={"data": {"status": "ok"}}))
    patcher, client = _httpx_patch(post)

    with patcher:
        result = await notification_service.send_expo_push(
            client=client,
            push_token=_token_row(),
            notif_title="FLOOD ALARM",
            notif_message="Evacuate now",
            notif_type=NotificationType.ALARM,
        )

    assert result == PushResult.SENT
    body = post.call_args.kwargs["json"]
    assert body["channelId"] == "alarm"
    assert body["sound"] == "siren.wav"
    assert body["interruptionLevel"] == "timeSensitive"
    assert body["priority"] == "high"
    assert body["ttl"] == 3600
    assert body["data"] == {"type": "alarm"}


async def test_expo_non_alarm_payload_stays_on_default_channel():
    post = AsyncMock(return_value=_expo_response(data={"data": {"status": "ok"}}))
    patcher, client = _httpx_patch(post)

    with patcher:
        result = await notification_service.send_expo_push(
            client=client,
            push_token=_token_row(),
            notif_title="t",
            notif_message="m",
            notif_type=NotificationType.WARNING,
        )

    assert result == PushResult.SENT
    body = post.call_args.kwargs["json"]
    assert body["channelId"] == "default"
    assert body["sound"] == "default"
    assert "interruptionLevel" not in body
    assert body["data"] == {"type": "warning"}


# ---------------------------------------------------------------------------
# Fan-out — push_tokens ride the same delivery-set as push_subscriptions
# ---------------------------------------------------------------------------


def _payload(responder_ids):
    return SendNotificationSchema(
        responder_ids=responder_ids,
        custom_notification=CustomNotificationPayload(
            type=NotificationType.CRITICAL, title="Alert", message="Message"
        ),
    )


async def test_fanout_sends_expo_tokens_and_prunes_gone():
    """send_notification_to_subscribers gathers push_tokens next to
    push_subscriptions: each token gets an Expo send, a GONE token is pruned,
    and a SENT on any device marks the responder's delivery row SENT."""
    rid = uuid.uuid4()
    live = MagicMock(responder_id=rid, token="ExponentPushToken[live]")
    dead = MagicMock(responder_id=rid, token="ExponentPushToken[dead]")
    # A token for a responder outside the audience must not be sent to.
    other = MagicMock(responder_id=uuid.uuid4(), token="ExponentPushToken[other]")
    dispatch = MagicMock()
    dispatch.id = 99
    db = AsyncMock()

    send_expo = AsyncMock(
        side_effect=lambda push_token, **kw: (
            PushResult.GONE if push_token is dead else PushResult.SENT
        )
    )

    with (
        patch(
            f"{NS_MODULE}.responder_crud",
            **{
                "get_reachable_ids": AsyncMock(
                    side_effect=lambda db, responder_ids: responder_ids
                )
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
            **{"get_by_responder_ids": AsyncMock(return_value=[live, dead, other])},
        ),
        patch(
            f"{NS_MODULE}.notification_delivery_crud",
            **{"upsert_many_results": AsyncMock()},
        ) as del_mock,
        patch.object(NotificationService, "send_expo_push", send_expo),
    ):
        sent = await notification_service.send_notification_to_subscribers(
            payload=_payload([rid]),
            db=db,
            # CRITICAL custom payloads need the trusted-internal flag — same
            # as the fusion/alarm callers.
            system_initiated=True,
        )

    # live SENT, dead pruned — responder counts as reached once.
    assert sent == 1
    assert send_expo.await_count == 2
    db.delete.assert_awaited_once_with(dead)
    final_rows = del_mock.upsert_many_results.call_args.kwargs["delivery_rows"]
    assert [row["responder_id"] for row in final_rows] == [rid]
    assert final_rows[0]["status"] == DeliveryStatus.SENT
