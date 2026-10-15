"""Tests for the T6 device registry + camera↔site binding.

Same seam as test_locations.py / test_claim_alert.py: TestClient (no context
manager) with get_db + require_auth (and require_iot_api_key for the ingest
path) overridden, services patched with AsyncMock. The service-level tests
exercise the validation rules directly against mocked CRUD objects.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, WebSocketDisconnect

from app.api.v1.dependencies import CurrentUser
from app.models import SensorConfig
from app.schemas import CameraDeviceItem, SensorDeviceItem
from app.services.camera_device_service import camera_device_service
from app.services.sensor_device_service import sensor_device_service

ADMIN_ID = uuid.uuid4()

SENSOR_CFG = {
    "installation_height": 100,
    "warning_threshold": 50,
    "critical_threshold": 80,
}
SENSOR_ITEM = SensorDeviceItem(
    id=1, device_name="Sensor A", location_id=1,
    sensor_config=SensorConfig(**SENSOR_CFG), is_active=True,
)
CAMERA_ITEM = CameraDeviceItem(id=1, device_name="Cam A", location_id=1, stream_url=None, is_active=True)


# ---------------------------------------------------------------------------
# Sensor device registry — endpoint contract
# ---------------------------------------------------------------------------


def test_register_sensor_device_success(admin_client):
    with patch.object(
        sensor_device_service, "register_device", new=AsyncMock(return_value=SENSOR_ITEM)
    ):
        res = admin_client.post(
            "/api/v1/sensor-devices",
            json={"device_name": "Sensor A", "location_id": 1, "sensor_config": SENSOR_CFG},
        )

    assert res.status_code == 201
    assert res.json()["device_name"] == "Sensor A"


def test_register_sensor_device_requires_auth(anon_client):
    res = anon_client.post(
        "/api/v1/sensor-devices",
        json={"device_name": "Sensor A", "location_id": 1, "sensor_config": SENSOR_CFG},
    )
    assert res.status_code in (401, 403)


def test_register_sensor_device_unknown_location_returns_404(admin_client):
    with patch.object(
        sensor_device_service,
        "register_device",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="Site not found.")),
    ):
        res = admin_client.post(
            "/api/v1/sensor-devices",
            json={"device_name": "Sensor A", "location_id": 99, "sensor_config": SENSOR_CFG},
        )
    assert res.status_code == 404


def test_register_sensor_device_inactive_location_returns_400(admin_client):
    with patch.object(
        sensor_device_service,
        "register_device",
        new=AsyncMock(side_effect=HTTPException(status_code=400, detail="Site is inactive.")),
    ):
        res = admin_client.post(
            "/api/v1/sensor-devices",
            json={"device_name": "Sensor A", "location_id": 2, "sensor_config": SENSOR_CFG},
        )
    assert res.status_code == 400


def test_register_sensor_device_duplicate_name_same_site_returns_409(admin_client):
    with patch.object(
        sensor_device_service,
        "register_device",
        new=AsyncMock(side_effect=HTTPException(status_code=409, detail="dup")),
    ):
        res = admin_client.post(
            "/api/v1/sensor-devices",
            json={"device_name": "Sensor A", "location_id": 1, "sensor_config": SENSOR_CFG},
        )
    assert res.status_code == 409


def test_list_sensor_devices_filters_by_location(admin_client):
    mock = AsyncMock(return_value=[SENSOR_ITEM])
    with patch.object(sensor_device_service, "list_devices", new=mock):
        res = admin_client.get("/api/v1/sensor-devices?location_id=1")

    assert res.status_code == 200
    assert res.json()[0]["device_name"] == "Sensor A"
    assert mock.await_args.kwargs["location_id"] == 1


def test_deactivate_sensor_device(admin_client):
    deactivated = SENSOR_ITEM.model_copy(update={"is_active": False})
    with patch.object(
        sensor_device_service, "update_device", new=AsyncMock(return_value=deactivated)
    ):
        res = admin_client.put("/api/v1/sensor-devices/1", json={"is_active": False})

    assert res.status_code == 200
    assert res.json()["is_active"] is False


def test_update_sensor_device_not_found_returns_404(admin_client):
    with patch.object(
        sensor_device_service,
        "update_device",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="Sensor device not found.")),
    ):
        res = admin_client.put("/api/v1/sensor-devices/99", json={"is_active": False})
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Camera device registry — endpoint contract
# ---------------------------------------------------------------------------


def test_register_camera_device_success(admin_client):
    with patch.object(
        camera_device_service, "register_device", new=AsyncMock(return_value=CAMERA_ITEM)
    ):
        res = admin_client.post(
            "/api/v1/camera-devices",
            json={"device_name": "Cam A", "location_id": 1},
        )

    assert res.status_code == 201
    assert res.json()["location_id"] == 1


def test_second_camera_same_site_ok(admin_client):
    second = CAMERA_ITEM.model_copy(update={"id": 2, "device_name": "Cam B"})
    with patch.object(
        camera_device_service, "register_device", new=AsyncMock(return_value=second)
    ):
        res = admin_client.post(
            "/api/v1/camera-devices",
            json={"device_name": "Cam B", "location_id": 1},
        )
    assert res.status_code == 201


def test_register_camera_device_requires_auth(anon_client):
    res = anon_client.post(
        "/api/v1/camera-devices", json={"device_name": "Cam A", "location_id": 1}
    )
    assert res.status_code in (401, 403)


def test_register_camera_device_unknown_location_returns_404(admin_client):
    with patch.object(
        camera_device_service,
        "register_device",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="Site not found.")),
    ):
        res = admin_client.post(
            "/api/v1/camera-devices", json={"device_name": "Cam A", "location_id": 99}
        )
    assert res.status_code == 404


def test_register_camera_device_inactive_location_returns_400(admin_client):
    with patch.object(
        camera_device_service,
        "register_device",
        new=AsyncMock(side_effect=HTTPException(status_code=400, detail="Site is inactive.")),
    ):
        res = admin_client.post(
            "/api/v1/camera-devices", json={"device_name": "Cam A", "location_id": 2}
        )
    assert res.status_code == 400


def test_register_camera_device_duplicate_name_same_site_returns_409(admin_client):
    with patch.object(
        camera_device_service,
        "register_device",
        new=AsyncMock(side_effect=HTTPException(status_code=409, detail="dup")),
    ):
        res = admin_client.post(
            "/api/v1/camera-devices", json={"device_name": "Cam A", "location_id": 1}
        )
    assert res.status_code == 409


def test_list_camera_devices_filters_by_location(admin_client):
    mock = AsyncMock(return_value=[CAMERA_ITEM])
    with patch.object(camera_device_service, "list_devices", new=mock):
        res = admin_client.get("/api/v1/camera-devices?location_id=1")

    assert res.status_code == 200
    assert res.json()[0]["device_name"] == "Cam A"
    assert mock.await_args.kwargs["location_id"] == 1


def test_deactivate_camera_device(admin_client):
    deactivated = CAMERA_ITEM.model_copy(update={"is_active": False})
    with patch.object(
        camera_device_service, "update_device", new=AsyncMock(return_value=deactivated)
    ):
        res = admin_client.put("/api/v1/camera-devices/1", json={"is_active": False})

    assert res.status_code == 200
    assert res.json()["is_active"] is False


def test_update_camera_device_not_found_returns_404(admin_client):
    with patch.object(
        camera_device_service,
        "update_device",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="Camera device not found.")),
    ):
        res = admin_client.put("/api/v1/camera-devices/99", json={"device_name": "X"})
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Camera device service — registry rules (mocked CRUD layer)
# ---------------------------------------------------------------------------

MODULE = "app.services.camera_device_service"


def _site(**overrides):
    site = MagicMock()
    site.id = 1
    site.name = "Site A"
    site.is_active = True
    for k, v in overrides.items():
        setattr(site, k, v)
    return site


def _device(**overrides):
    d = MagicMock()
    d.id = 1
    d.device_name = "Cam A"
    d.location_id = 1
    d.stream_url = None
    d.is_active = True
    for k, v in overrides.items():
        setattr(d, k, v)
    return d


def _service_patches(location=None, clash=None, created=None, device=None):
    return (
        patch(f"{MODULE}.location_crud", **{"get": AsyncMock(return_value=location)}),
        patch(
            f"{MODULE}.camera_device_crud",
            **{
                "get_by_name_and_location": AsyncMock(return_value=clash),
                "create_and_return_no_commit": AsyncMock(return_value=created),
                "get": AsyncMock(return_value=device),
                "update_no_commit": AsyncMock(return_value=device),
            },
        ),
        patch(
            f"{MODULE}.admin_audit_log_crud",
            **{"create_only_no_commit": AsyncMock()},
        ),
    )


async def test_service_register_camera_success():
    from app.schemas import CameraDeviceCreate

    created = _device(id=7)
    loc_p, cam_p, audit_p = _service_patches(location=_site(), created=created)
    with loc_p, cam_p, audit_p as audit_mock:
        result = await camera_device_service.register_device(
            db=AsyncMock(),
            payload=CameraDeviceCreate(device_name="Cam A", location_id=1),
            current_user=CurrentUser(
                id=str(ADMIN_ID), is_superuser=True, is_enabled=True,
                force_password_change=False,
            ),
        )
    assert result.id == 7
    audit_mock.create_only_no_commit.assert_awaited_once()


async def test_service_register_camera_unknown_site_404():
    from app.schemas import CameraDeviceCreate

    loc_p, cam_p, audit_p = _service_patches(location=None)
    with loc_p, cam_p, audit_p:
        with pytest.raises(HTTPException) as exc:
            await camera_device_service.register_device(
                db=AsyncMock(),
                payload=CameraDeviceCreate(device_name="Cam A", location_id=99),
                current_user=CurrentUser(
                    id=str(ADMIN_ID), is_superuser=True, is_enabled=True,
                    force_password_change=False,
                ),
            )
    assert exc.value.status_code == 404


async def test_service_register_camera_inactive_site_400():
    from app.schemas import CameraDeviceCreate

    loc_p, cam_p, audit_p = _service_patches(location=_site(is_active=False))
    with loc_p, cam_p, audit_p:
        with pytest.raises(HTTPException) as exc:
            await camera_device_service.register_device(
                db=AsyncMock(),
                payload=CameraDeviceCreate(device_name="Cam A", location_id=1),
                current_user=CurrentUser(
                    id=str(ADMIN_ID), is_superuser=True, is_enabled=True,
                    force_password_change=False,
                ),
            )
    assert exc.value.status_code == 400


async def test_service_register_camera_dup_name_same_site_409():
    from app.schemas import CameraDeviceCreate

    loc_p, cam_p, audit_p = _service_patches(
        location=_site(), clash=_device(id=2)
    )
    with loc_p, cam_p, audit_p:
        with pytest.raises(HTTPException) as exc:
            await camera_device_service.register_device(
                db=AsyncMock(),
                payload=CameraDeviceCreate(device_name="Cam A", location_id=1),
                current_user=CurrentUser(
                    id=str(ADMIN_ID), is_superuser=True, is_enabled=True,
                    force_password_change=False,
                ),
            )
    assert exc.value.status_code == 409


async def test_service_register_camera_same_name_other_site_ok():
    from app.schemas import CameraDeviceCreate

    created = _device(id=8, location_id=2)
    loc_p, cam_p, audit_p = _service_patches(
        location=_site(id=2), clash=None, created=created
    )
    with loc_p, cam_p, audit_p:
        result = await camera_device_service.register_device(
            db=AsyncMock(),
            payload=CameraDeviceCreate(device_name="Cam A", location_id=2),
            current_user=CurrentUser(
                id=str(ADMIN_ID), is_superuser=True, is_enabled=True,
                force_password_change=False,
            ),
        )
    assert result.location_id == 2


async def test_service_update_camera_not_found_404():
    from app.schemas import CameraDeviceUpdate

    loc_p, cam_p, audit_p = _service_patches(device=None)
    with loc_p, cam_p, audit_p:
        with pytest.raises(HTTPException) as exc:
            await camera_device_service.update_device(
                db=AsyncMock(),
                device_id=99,
                payload=CameraDeviceUpdate(is_active=False),
                current_user=CurrentUser(
                    id=str(ADMIN_ID), is_superuser=True, is_enabled=True,
                    force_password_change=False,
                ),
            )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Camera↔site binding — service-level verification
# ---------------------------------------------------------------------------


async def test_get_for_ingest_returns_device_when_bound():
    device = _device(location_id=1)
    site = MagicMock(id=1, is_active=True)
    with (
        patch(f"{MODULE}.camera_device_crud", **{"get": AsyncMock(return_value=device)}),
        patch(f"{MODULE}.location_crud", **{"get": AsyncMock(return_value=site)}),
    ):
        result = await camera_device_service.get_for_ingest(
            db=AsyncMock(), camera_device_id=1, location_id=1
        )
    assert result is device


async def test_get_for_ingest_rejects_inactive_site():
    device = _device(location_id=1)
    site = MagicMock(id=1, is_active=False)
    with (
        patch(f"{MODULE}.camera_device_crud", **{"get": AsyncMock(return_value=device)}),
        patch(f"{MODULE}.location_crud", **{"get": AsyncMock(return_value=site)}),
    ):
        result = await camera_device_service.get_for_ingest(
            db=AsyncMock(), camera_device_id=1, location_id=1
        )
    assert result is None


async def test_get_for_ingest_rejects_site_mismatch():
    device = _device(location_id=1)
    with patch(f"{MODULE}.camera_device_crud", **{"get": AsyncMock(return_value=device)}):
        result = await camera_device_service.get_for_ingest(
            db=AsyncMock(), camera_device_id=1, location_id=2
        )
    assert result is None


async def test_get_for_ingest_rejects_unknown_device():
    with patch(f"{MODULE}.camera_device_crud", **{"get": AsyncMock(return_value=None)}):
        result = await camera_device_service.get_for_ingest(
            db=AsyncMock(), camera_device_id=99, location_id=1
        )
    assert result is None


async def test_get_for_ingest_rejects_inactive_device():
    device = _device(is_active=False)
    with patch(f"{MODULE}.camera_device_crud", **{"get": AsyncMock(return_value=device)}):
        result = await camera_device_service.get_for_ingest(
            db=AsyncMock(), camera_device_id=1, location_id=1
        )
    assert result is None


async def test_get_for_ingest_legacy_resolves_default_camera():
    # Legacy firmware sends only location_id — the site's default active
    # camera is resolved and then bound the same way.
    device = _device(id=7, location_id=1)
    site = MagicMock(id=1, is_active=True)
    with patch(
        f"{MODULE}.camera_device_crud",
        **{
            "get_id_by_location": AsyncMock(return_value=7),
            "get": AsyncMock(return_value=device),
        },
    ), patch(f"{MODULE}.location_crud", **{"get": AsyncMock(return_value=site)}):
        result = await camera_device_service.get_for_ingest(
            db=AsyncMock(), camera_device_id=None, location_id=1
        )
    assert result is device


async def test_get_for_ingest_legacy_no_camera_on_site():
    with patch(
        f"{MODULE}.camera_device_crud",
        **{"get_id_by_location": AsyncMock(return_value=None)},
    ):
        result = await camera_device_service.get_for_ingest(
            db=AsyncMock(), camera_device_id=None, location_id=9
        )
    assert result is None


# ---------------------------------------------------------------------------
# Ingest path — upload-image HTTP binding + /ws/rpi websocket binding
# ---------------------------------------------------------------------------


def test_upload_image_binding_mismatch_returns_400(iot_client):
    with patch.object(
        camera_device_service, "get_for_ingest", new=AsyncMock(return_value=None)
    ):
        res = iot_client.post(
            "/api/v1/stream/upload-image?camera_device_id=1&location_id=2",
            files={"file": ("f.jpg", b"\xff\xd8\xff", "image/jpeg")},
        )
    assert res.status_code == 400


def test_upload_image_success_uses_registered_site(iot_client):
    device = _device(id=1, location_id=2)
    with (
        patch.object(
            camera_device_service, "get_for_ingest", new=AsyncMock(return_value=device)
        ),
        patch("app.api.v1.endpoints.stream.camera_status_service") as status_mock,
        patch("app.api.v1.endpoints.stream.ws_manager") as ws_mock,
        patch("app.api.v1.endpoints.stream.ml_service") as ml_mock,
    ):
        ws_mock.broadcast_to_location = AsyncMock()
        ml_mock.process_frame_bytes = AsyncMock()
        res = iot_client.post(
            "/api/v1/stream/upload-image?camera_device_id=1&location_id=2",
            files={"file": ("f.jpg", b"\xff\xd8\xff", "image/jpeg")},
        )

    assert res.status_code == 200
    ml_mock.process_frame_bytes.assert_awaited_once()
    assert ml_mock.process_frame_bytes.await_args.kwargs["location_id"] == 2
    status_mock.record_frame.assert_called_once()


def _fake_session_factory():
    session = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def test_ws_rpi_binding_mismatch_closes_1008(anon_client):
    with (
        patch(
            "app.api.v1.endpoints.websocket.AsyncSessionLocal",
            new=_fake_session_factory(),
        ),
        patch.object(
            camera_device_service, "get_for_ingest", new=AsyncMock(return_value=None)
        ),
    ):
        with pytest.raises(WebSocketDisconnect) as exc:
            with anon_client.websocket_connect(
                "/ws/rpi?camera_device_id=1&location_id=2"
            ) as ws:
                ws.receive_json()
    assert exc.value.code == 1008


def test_ws_rpi_bound_device_connects(anon_client):
    device = _device(id=1, location_id=2)
    with (
        patch(
            "app.api.v1.endpoints.websocket.AsyncSessionLocal",
            new=_fake_session_factory(),
        ),
        patch.object(
            camera_device_service, "get_for_ingest", new=AsyncMock(return_value=device)
        ),
    ):
        with anon_client.websocket_connect(
            "/ws/rpi?camera_device_id=1&location_id=2"
        ) as ws:
            msg = ws.receive_json()
            assert msg == {"type": "connected", "camera_device_id": 1, "location_id": 2}
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}


def test_ws_rpi_legacy_no_camera_device_id_connects(anon_client):
    # Legacy firmware: location_id only — the service resolves the default.
    device = _device(id=5, location_id=2)
    with (
        patch(
            "app.api.v1.endpoints.websocket.AsyncSessionLocal",
            new=_fake_session_factory(),
        ),
        patch.object(
            camera_device_service, "get_for_ingest", new=AsyncMock(return_value=device)
        ) as ingest,
    ):
        with anon_client.websocket_connect("/ws/rpi?location_id=2") as ws:
            msg = ws.receive_json()
            assert msg == {"type": "connected", "camera_device_id": 5, "location_id": 2}
    assert ingest.await_args.kwargs["camera_device_id"] is None


def test_upload_image_legacy_no_camera_device_id(iot_client):
    device = _device(id=3, location_id=2)
    with (
        patch.object(
            camera_device_service, "get_for_ingest", new=AsyncMock(return_value=device)
        ) as ingest,
        patch("app.api.v1.endpoints.stream.camera_status_service"),
        patch("app.api.v1.endpoints.stream.ws_manager") as ws_mock,
        patch("app.api.v1.endpoints.stream.ml_service") as ml_mock,
    ):
        ws_mock.broadcast_to_location = AsyncMock()
        ml_mock.process_frame_bytes = AsyncMock()
        res = iot_client.post(
            "/api/v1/stream/upload-image?location_id=2",
            files={"file": ("f.jpg", b"\xff\xd8\xff", "image/jpeg")},
        )

    assert res.status_code == 200
    assert ingest.await_args.kwargs["camera_device_id"] is None
    # ML runs against the resolved device id, not the missing param.
    assert ml_mock.process_frame_bytes.await_args.kwargs["camera_device_id"] == 3


# ---------------------------------------------------------------------------
# /ws presence derivation — a responder token pins the channel to their
# effective site; unauthenticated clients keep the client-chosen channel.
# ---------------------------------------------------------------------------


def _responder_jwt(responder_id: uuid.UUID) -> str:
    from jose import jwt
    from app.core.config import settings

    return jwt.encode(
        {"sub": str(responder_id), "type": "responder"},
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )


def test_ws_token_derives_channel_from_active_site(anon_client):
    responder = MagicMock()
    responder.active_location_id = 3
    responder.location_id = 1
    from app.models.responder_related.responders import ResponderStatus

    responder.status = ResponderStatus.ACTIVE

    session = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)

    with (
        patch("app.api.v1.endpoints.websocket.AsyncSessionLocal", new=factory),
        patch(
            "app.api.v1.endpoints.websocket.websocket_service.send_initial_data",
            new=AsyncMock(),
        ) as send_init,
        patch("app.crud.responder_crud.get", new=AsyncMock(return_value=responder)),
        patch(
            "app.crud.location_crud.get",
            new=AsyncMock(return_value=MagicMock(id=3, is_active=True)),
        ),
    ):
        with anon_client.websocket_connect(
            f"/ws?location_id=99&token={_responder_jwt(uuid.uuid4())}"
        ):
            pass

    # responder_crud.get is patched at its definition site; the function under
    # test imports it inside _resolve_responder_location_id.
    assert send_init.await_args.kwargs["location_id"] == 3


def test_ws_invalid_token_closes_1008(anon_client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with anon_client.websocket_connect("/ws?location_id=1&token=bad") as ws:
            ws.receive_json()
    assert exc.value.code == 1008


def test_ws_no_token_keeps_client_chosen_channel(anon_client):
    session = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.api.v1.endpoints.websocket.AsyncSessionLocal", new=MagicMock(return_value=cm)),
        patch(
            "app.api.v1.endpoints.websocket.websocket_service.send_initial_data",
            new=AsyncMock(),
        ) as send_init,
        patch(
            "app.crud.location_crud.get",
            new=AsyncMock(return_value=MagicMock(id=7, is_active=True)),
        ),
    ):
        with anon_client.websocket_connect("/ws?location_id=7"):
            pass

    assert send_init.await_args.kwargs["location_id"] == 7


def test_ws_inactive_site_closes_1008(anon_client):
    session = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.api.v1.endpoints.websocket.AsyncSessionLocal", new=MagicMock(return_value=cm)),
        patch(
            "app.crud.location_crud.get",
            new=AsyncMock(return_value=MagicMock(id=7, is_active=False)),
        ),
    ):
        with pytest.raises(WebSocketDisconnect) as exc:
            with anon_client.websocket_connect("/ws?location_id=7") as ws:
                ws.receive_json()
    assert exc.value.code == 1008
