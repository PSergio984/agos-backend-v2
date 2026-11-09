"""Endpoint contract for /system-settings.

Same seam as test_locations: TestClient without a context manager, get_db +
require_auth overridden by the client fixtures, CRUD layer mocked. The CRUD
None signal is the boundary's job to map to 404.
"""

from unittest.mock import AsyncMock, patch

from app.crud.system_settings import system_settings_crud
from app.models.system_settings import SystemSettings


def test_get_setting_returns_row(admin_client):
    setting = SystemSettings(key="data_retention_days", json_value=30)
    with patch.object(system_settings_crud, "get", new=AsyncMock(return_value=setting)):
        res = admin_client.get("/api/v1/system-settings/data_retention_days")

    assert res.status_code == 200
    assert res.json() == {"key": "data_retention_days", "json_value": 30}


def test_get_setting_missing_key_returns_404(admin_client):
    with patch.object(system_settings_crud, "get", new=AsyncMock(return_value=None)):
        res = admin_client.get("/api/v1/system-settings/missing")

    assert res.status_code == 404


def test_get_setting_value_returns_json_value(admin_client):
    setting = SystemSettings(key="alert_thresholds", json_value={"tier_1_max": 30})
    with patch.object(system_settings_crud, "get", new=AsyncMock(return_value=setting)):
        res = admin_client.get("/api/v1/system-settings/alert_thresholds/value")

    assert res.status_code == 200
    assert res.json() == {"tier_1_max": 30}


def test_get_setting_value_missing_key_returns_404(admin_client):
    with patch.object(system_settings_crud, "get", new=AsyncMock(return_value=None)):
        res = admin_client.get("/api/v1/system-settings/missing/value")

    assert res.status_code == 404


def test_get_setting_value_falsy_value_is_not_404(admin_client):
    # A present key whose json_value is falsy (e.g. 0) is not a missing key.
    setting = SystemSettings(key="data_retention_days", json_value=0)
    with patch.object(system_settings_crud, "get", new=AsyncMock(return_value=setting)):
        res = admin_client.get("/api/v1/system-settings/data_retention_days/value")

    assert res.status_code == 200
    assert res.json() == 0


def test_update_setting_missing_key_returns_404(admin_client):
    with patch.object(system_settings_crud, "get", new=AsyncMock(return_value=None)):
        res = admin_client.put(
            "/api/v1/system-settings/missing",
            json={"key": "missing", "json_value": 1},
        )

    assert res.status_code == 404
