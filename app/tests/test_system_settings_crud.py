"""system_settings_crud signals a missing key with None — the API boundary
maps it to 404, non-HTTP callers handle it themselves."""

from unittest.mock import AsyncMock, MagicMock

import app.crud.system_settings as crud_module
from app.crud.system_settings import system_settings_crud
from app.models.system_settings import SystemSettings


def _db_returning(row):
    result = MagicMock()
    result.scalars.return_value.first.return_value = row
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


async def test_get_returns_none_when_key_missing():
    assert await system_settings_crud.get(_db_returning(None), "missing") is None


async def test_get_returns_row_when_key_present():
    row = SystemSettings(key="data_retention_days", json_value=30)
    assert await system_settings_crud.get(_db_returning(row), "data_retention_days") is row


async def test_get_value_returns_json_value_when_key_present():
    row = SystemSettings(key="alert_thresholds", json_value={"tier_1_max": 30})
    assert await system_settings_crud.get_value(_db_returning(row), "alert_thresholds") == {"tier_1_max": 30}


async def test_get_value_returns_none_when_key_missing():
    assert await system_settings_crud.get_value(_db_returning(None), "missing") is None


def test_crud_layer_does_not_depend_on_fastapi():
    assert not hasattr(crud_module, "HTTPException")
