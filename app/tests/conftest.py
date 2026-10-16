from typing import AsyncGenerator, Generator
import os
import pytest
from httpx import ASGITransport, AsyncClient
from fastapi.testclient import TestClient

os.environ["ENV_STATE"] = "test"

from app.main import app


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


# This fixture provides a TestClient for testing the FastAPI.
@pytest.fixture()
def client(db) -> Generator:
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def db() -> Generator:
    """Database fixture - app uses get_db for actual DB access."""
    yield


# This fixture provides an AsyncClient for testing the FastAPI.
@pytest.fixture()
async def async_client(db) -> AsyncGenerator:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Endpoint-contract seam (shared by test_locations/test_device_registry/
# test_deployments): TestClient WITHOUT context manager (lifespan opens a real
# DB), get_db overridden with a MagicMock, auth deps overridden per role.
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock  # noqa: E402

import uuid  # noqa: E402

from app.api.v1.dependencies import (  # noqa: E402
    CurrentResponder,
    CurrentUser,
    optional_auth,
    require_auth,
    require_iot_api_key,
    require_responder_auth,
)
from app.core.database import get_db  # noqa: E402


async def _fake_db():
    yield MagicMock()


@pytest.fixture()
def admin_client():
    app.dependency_overrides[require_auth] = lambda: CurrentUser(
        id=str(uuid.uuid4()), is_superuser=True, is_enabled=True,
        force_password_change=False,
    )
    # Routes that gate a privileged flag behind optional auth (locations
    # include_inactive) should see this client as an admin too.
    app.dependency_overrides[optional_auth] = lambda: CurrentUser(
        id=str(uuid.uuid4()), is_superuser=True, is_enabled=True,
        force_password_change=False,
    )
    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def responder_client():
    app.dependency_overrides[require_responder_auth] = lambda: CurrentResponder(
        id=str(uuid.uuid4())
    )
    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def anon_client():
    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def iot_client():
    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[require_iot_api_key] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()
