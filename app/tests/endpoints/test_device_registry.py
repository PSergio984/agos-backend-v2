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
from fastapi.testclient import TestClient

from app.main import app
from app.api.v1.dependencies import CurrentUser, require_auth, require_iot_api_key
from app.core.database import get_db
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
CAMERA_ITEM = CameraDeviceItem(id=1, device_name="Cam A", location_id=1, is_active=True)


@pytest.fixture()
def authed_client():
    async def _fake_db():
        yield MagicMock()

    app.dependency_overrides[require_auth] = lambda: CurrentUser(
        id=str(ADMIN_ID), is_superuser=True, is_enabled=True, force_password_change=False
    )
    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def anon_client():
    async def _fake_db():
        yield MagicMock()

    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def iot_client():
    async def _fake_db():
        yield MagicMock()

    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[require_iot_api_key] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Sensor device registry — endpoint contract
# ---------------------------------------------------------------------------


def test_register_sensor_device_success(authed_client):
    with patch.object(
        sensor_device_service, "register_device", new=AsyncMock(return_value=SENSOR_ITEM)
    ):
        res = authed_client.post(
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


def test_register_sensor_device_unknown_location_returns_404(authed_client):
    with patch.object(
        sensor_device_service,
        "register_device",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="Site not found.")),
    ):
        res = authed_client.post(
            "/api/v1/sensor-devices",
            json={"device_name": "Sensor A", "location_id": 99, "sensor_config": SENSOR_CFG},
        )
    assert res.status_code == 404


def test_register_sensor_device_inactive_location_returns_400(authed_client):
    with patch.object(
        sensor_device_service,
        "register_device",
        new=AsyncMock(side_effect=HTTPException(status_code=400, detail="Site is inactive.")),
    ):
        res = authed_client.post(
            "/api/v1/sensor-devices",
            json={"device_name": "Sensor A", "location_id": 2, "sensor_config": SENSOR_CFG},
        )
    assert res.status_code == 400


def test_register_sensor_device_duplicate_name_same_site_returns_409(authed_client):
    with patch.object(
        sensor_device_service,
        "register_device",
        new=AsyncMock(side_effect=HTTPException(status_code=409, detail="dup")),
    ):
        res = authed_client.post(
            "/api/v1/sensor-devices",
            json={"device_name": "Sensor A", "location_id": 1, "sensor_config": SENSOR_CFG},
        )
    assert res.status_code == 409


def test_list_sensor_devices_filters_by_location(authed_client):
    mock = AsyncMock(return_value=[SENSOR_ITEM])
    with patch.object(sensor_device_service, "list_devices", new=mock):
        res = authed_client.get("/api/v1/sensor-devices?location_id=1")

    assert res.status_code == 200
    assert res.json()[0]["device_name"] == "Sensor A"
    assert mock.await_args.kwargs["location_id"] == 1


def test_deactivate_sensor_device(authed_client):
    deactivated = SENSOR_ITEM.model_copy(update={"is_active": False})
    with patch.object(
        sensor_device_service, "update_device", new=AsyncMock(return_value=deactivated)
    ):
        res = authed_client.put("/api/v1/sensor-devices/1", json={"is_active": False})

    assert res.status_code == 200
    assert res.json()["is_active"] is False


def test_update_sensor_device_not_found_returns_404(authed_client):
    with patch.object(
        sensor_device_service,
        "update_device",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="Sensor device not found.")),
    ):
        res = authed_client.put("/api/v1/sensor-devices/99", json={"is_active": False})
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Camera device registry — endpoint contract
# ---------------------------------------------------------------------------


def test_register_camera_device_success(authed_client):
    with patch.object(
        camera_device_service, "register_device", new=AsyncMock(return_value=CAMERA_ITEM)
    ):
        res = authed_client.post(
            "/api/v1/camera-devices",
            json={"device_name": "Cam A", "location_id": 1},
        )

    assert res.status_code == 201
    assert res.json()["location_id"] == 1


def test_second_camera_same_site_ok(authed_client):
    second = CAMERA_ITEM.model_copy(update={"id": 2, "device_name": "Cam B"})
    with patch.object(
        camera_device_service, "register_device", new=AsyncMock(return_value=second)
    ):
        res = authed_client.post(
            "/api/v1/camera-devices",
            json={"device_name": "Cam B", "location_id": 1},
        )
    assert res.status_code == 201


def test_register_camera_device_requires_auth(anon_client):
    res = anon_client.post(
        "/api/v1/camera-devices", json={"device_name": "Cam A", "location_id": 1}
    )
    assert res.status_code in (401, 403)


def test_register_camera_device_unknown_location_returns_404(authed_client):
    with patch.object(
        camera_device_service,
        "register_device",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="Site not found.")),
    ):
        res = authed_client.post(
            "/api/v1/camera-devices", json={"device_name": "Cam A", "location_id": 99}
        )
    assert res.status_code == 404


def test_register_camera_device_inactive_location_returns_400(authed_client):
    with patch.object(
        camera_device_service,
        "register_device",
        new=AsyncMock(side_effect=HTTPException(status_code=400, detail="Site is inactive.")),
    ):
        res = authed_client.post(
            "/api/v1/camera-devices", json={"device_name": "Cam A", "location_id": 2}
        )
    assert res.status_code == 400


def test_register_camera_device_duplicate_name_same_site_returns_409(authed_client):
    with patch.object(
        camera_device_service,
        "register_device",
        new=AsyncMock(side_effect=HTTPException(status_code=409, detail="dup")),
    ):
        res = authed_client.post(
            "/api/v1/camera-devices", json={"device_name": "Cam A", "location_id": 1}
        )
    assert res.status_code == 409


def test_list_camera_devices_filters_by_location(authed_client):
    mock = AsyncMock(return_value=[CAMERA_ITEM])
    with patch.object(camera_device_service, "list_devices", new=mock):
        res = authed_client.get("/api/v1/camera-devices?location_id=1")

    assert res.status_code == 200
    assert res.json()[0]["device_name"] == "Cam A"
    assert mock.await_args.kwargs["location_id"] == 1


def test_deactivate_camera_device(authed_client):
    deactivated = CAMERA_ITEM.model_copy(update={"is_active": False})
    with patch.object(
        camera_device_service, "update_device", new=AsyncMock(return_value=deactivated)
    ):
        res = authed_client.put("/api/v1/camera-devices/1", json={"is_active": False})

    assert res.status_code == 200
    assert res.json()["is_active"] is False


def test_update_camera_device_not_found_returns_404(authed_client):
    with patch.object(
        camera_device_service,
        "update_device",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="Camera device not found.")),
    ):
        res = authed_client.put("/api/v1/camera-devices/99", json={"device_name": "X"})
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
                "create_and_return": AsyncMock(return_value=created),
                "get": AsyncMock(return_value=device),
                "update": AsyncMock(return_value=device),
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
    with patch(f"{MODULE}.camera_device_crud", **{"get": AsyncMock(return_value=device)}):
        result = await camera_device_service.get_for_ingest(
            db=AsyncMock(), camera_device_id=1, location_id=1
        )
    assert result is device


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
