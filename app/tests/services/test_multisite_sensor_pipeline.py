"""T5 — per-site sensor config cache + device-scoped readings.

Same seam as test_daily_summary.py: service level with the CRUD/cache
boundary mocked — no internals patched.
"""

import importlib
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.api.v1.dependencies import CurrentUser
from app.crud import (
    admin_audit_log_crud,
    camera_device_crud,
    location_crud,
    sensor_device_crud,
    sensor_reading_crud,
)
from app.models import SensorConfig
from app.schemas import LocationCreate
from app.services import sensor_reading_service
from app.services.cache_service import CacheService, cache_service

location_service_module = importlib.import_module("app.services.location_service")

CONFIG_A = SensorConfig(installation_height=200, warning_threshold=50, critical_threshold=100)
CONFIG_B = SensorConfig(installation_height=300, warning_threshold=60, critical_threshold=120)


async def test_sensor_config_cache_is_keyed_per_device(monkeypatch):
    """Two devices in two locations must resolve their own configs — a site-2
    device must never receive site-1's cached config."""
    service = CacheService()
    configs = {10: CONFIG_A, 20: CONFIG_B}
    get_device_config = AsyncMock(
        side_effect=lambda db, sensor_device_id: configs[sensor_device_id]
    )
    monkeypatch.setattr(sensor_device_crud, "get_device_config", get_device_config)
    db = MagicMock()

    config_a = await service.get_sensor_config(db=db, sensor_device_id=10)
    config_b = await service.get_sensor_config(db=db, sensor_device_id=20)

    assert config_a == CONFIG_A
    assert config_b == CONFIG_B
    assert config_b != config_a
    assert get_device_config.await_count == 2

    # Second reads hit the per-device cache — no further CRUD calls.
    assert await service.get_sensor_config(db=db, sensor_device_id=10) == CONFIG_A
    assert await service.get_sensor_config(db=db, sensor_device_id=20) == CONFIG_B
    assert get_device_config.await_count == 2


async def test_update_sensor_config_cache_refreshes_only_that_device(monkeypatch):
    """update_device_config must invalidate only the updated device's entry."""
    service = CacheService()
    configs = {10: CONFIG_A, 20: CONFIG_B}
    get_device_config = AsyncMock(
        side_effect=lambda db, sensor_device_id: configs[sensor_device_id]
    )
    monkeypatch.setattr(sensor_device_crud, "get_device_config", get_device_config)
    db = MagicMock()
    await service.get_sensor_config(db=db, sensor_device_id=10)
    await service.get_sensor_config(db=db, sensor_device_id=20)

    updated = SensorConfig(installation_height=250, warning_threshold=70, critical_threshold=110)
    configs[10] = updated
    await service.update_sensor_config_cache(db=db, sensor_device_id=10)

    assert await service.get_sensor_config(db=db, sensor_device_id=10) == updated
    assert await service.get_sensor_config(db=db, sensor_device_id=20) == CONFIG_B


async def test_sensor_config_falls_back_to_first_device_without_device_context(monkeypatch):
    """Callers without a device id keep the legacy fallback: first cached
    sensor device (location 1, else the first known location)."""
    service = CacheService()
    monkeypatch.setattr(location_crud, "get_all_ids", AsyncMock(return_value=[2]))
    monkeypatch.setattr(camera_device_crud, "get_id_by_location", AsyncMock(return_value=99))
    monkeypatch.setattr(sensor_device_crud, "get_id_by_location", AsyncMock(return_value=10))
    get_device_config = AsyncMock(return_value=CONFIG_A)
    monkeypatch.setattr(sensor_device_crud, "get_device_config", get_device_config)

    config = await service.get_sensor_config(db=MagicMock())

    assert config == CONFIG_A
    get_device_config.assert_awaited_once()
    assert get_device_config.await_args.kwargs["sensor_device_id"] == 10


async def test_record_summary_scopes_previous_reading_and_config_to_device(monkeypatch):
    """A reading's trend and alert summary must be computed against the same
    device's history and config — never another site's."""
    reading = SimpleNamespace(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        water_level_cm=50.0,
        sensor_device_id=20,
    )
    get_previous = AsyncMock(return_value=None)
    monkeypatch.setattr(sensor_reading_crud, "get_previous_reading", get_previous)
    get_config = AsyncMock(return_value=CONFIG_B)
    monkeypatch.setattr(cache_service, "get_sensor_config", get_config)

    summary = await sensor_reading_service.calculate_record_summary(
        db=MagicMock(), reading=reading
    )

    assert get_previous.await_args.kwargs["sensor_device_id"] == 20
    assert get_config.await_args.kwargs["sensor_device_id"] == 20
    assert summary.water_level.trend == "stable"


async def test_create_site_initializes_fusion_state_for_new_location(monkeypatch):
    """A site created at runtime must get fusion state, or its readings can
    never score until the next restart."""
    created = SimpleNamespace(
        id=7, name="Site C", latitude=14.8, longitude=121.2, is_active=True
    )
    monkeypatch.setattr(location_crud, "get_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(
        location_crud, "create_and_return_no_commit", AsyncMock(return_value=created)
    )
    monkeypatch.setattr(
        admin_audit_log_crud, "create_only_no_commit", AsyncMock()
    )
    start_state = MagicMock()
    monkeypatch.setattr(
        location_service_module.fusion_state_manager,
        "start_fusion_analysis_state",
        start_state,
    )

    db = AsyncMock()
    user = CurrentUser(
        id=str(uuid.uuid4()),
        is_superuser=True,
        is_enabled=True,
        force_password_change=False,
    )
    result = await location_service_module.location_service.create_site(
        db=db,
        payload=LocationCreate(name="Site C", latitude=14.8, longitude=121.2),
        current_user=user,
    )

    assert result.id == 7
    start_state.assert_called_once()
    assert start_state.call_args.kwargs["location_id"] == 7


async def test_device_ids_cache_tolerates_deviceless_site(monkeypatch):
    """A newly-registered site has no devices — the cache rebuild must store
    None ids instead of crashing the whole rebuild (which would take down
    send_initial_data and startup fusion-state init for every site)."""
    service = CacheService()
    monkeypatch.setattr(location_crud, "get_all_ids", AsyncMock(return_value=[1, 3]))
    monkeypatch.setattr(
        sensor_device_crud,
        "get_id_by_location",
        AsyncMock(side_effect=lambda db, location_id: {1: 10, 3: None}[location_id]),
    )
    monkeypatch.setattr(
        camera_device_crud,
        "get_id_by_location",
        AsyncMock(side_effect=lambda db, location_id: {1: 55, 3: None}[location_id]),
    )
    db = MagicMock()

    await service.update_device_ids_cache(db=db)

    assert service._device_ids_cache[1].sensor_device_id == 10
    assert service._device_ids_cache[3].sensor_device_id is None
    assert service._device_ids_cache[3].camera_device_id is None

    # Same for the reverse map — a deviceless site contributes no entry.
    await service.update_location_id_per_sensor_device_cache(db=db)
    assert service._location_id_per_sensor_device_cache == {10: 1}


async def test_record_reading_rejects_reading_for_inactive_site(monkeypatch):
    """A device on a deactivated site must not record — deactivation gates
    ingest even while the device row is still marked active."""
    device = SimpleNamespace(id=10, location_id=3, is_active=True)
    monkeypatch.setattr(
        sensor_device_crud, "get_entity", AsyncMock(return_value=device)
    )
    monkeypatch.setattr(
        location_crud,
        "get",
        AsyncMock(return_value=SimpleNamespace(id=3, is_active=False)),
    )
    create = AsyncMock()
    monkeypatch.setattr(sensor_reading_crud, "create_record", create)

    result = await sensor_reading_service.record_reading(
        db=MagicMock(), obj_in=SimpleNamespace(sensor_device_id=10)
    )

    assert "inactive" in result.status
    create.assert_not_awaited()
