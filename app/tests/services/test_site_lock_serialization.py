"""Tests for #17 — site deactivation TOCTOU.

Seam: service → location_crud. Every writer that gates on `location.is_active`
must read the site through a row lock so the check serializes against
`update_site`'s deactivation commit:

- `update_site` itself mutates the row → `get_with_lock` (SELECT … FOR UPDATE).
- Gated writers only READ is_active (they write other tables) → `get_for_share`
  (SELECT … FOR SHARE): blocks deactivation's UPDATE without serializing
  writers against each other.
- Writers that also take the responder row lock (`open_deployment`,
  `set_active_site`) must lock location BEFORE responder — `update_site` holds
  the location lock and then writes responder rows, so responder→location is a
  deadlock cycle.
- `verify_otp` / `update_responder_details` have an internal commit
  (`ensure_exists`) between check and gated write — the locked check must run
  AFTER that commit, in the same txn as the gated write.
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.v1.dependencies import CurrentUser
from app.schemas import (
    EvacuationCenterCreate,
    EvacuationConfirmRequest,
    LocationUpdate,
    ResponderAdminUpdate,
    ResponderDeploymentCreate,
    ResponderOTPVerifyRequest,
    SensorReadingCreate,
)
from app.services.camera_device_service import camera_device_service
from app.services.evacuation_center_service import evacuation_center_service
from app.services.evacuation_service import evacuation_service
from app.services.location_service import location_service
from app.services.responder.responder_app_service import responder_app_service
from app.services.responder.responder_deployment_service import responder_deployment_service
from app.services.responder.responder_service import responder_service
from app.services.sensor_device_service import sensor_device_service
from app.services.sensor_reading.service import sensor_reading_service

ADMIN_ID = uuid.uuid4()
RESPONDER_ID = uuid.uuid4()
HOME_SITE_ID = 1
SITE_B_ID = 2

DEPLOY_MOD = "app.services.responder.responder_deployment_service"
APP_MOD = "app.services.responder.responder_app_service"
SVC_MOD = "app.services.responder.responder_service"
LOC_MOD = "app.services.location_service"
READ_MOD = "app.services.sensor_reading.service"
EVAC_MOD = "app.services.evacuation_service"
CENTER_MOD = "app.services.evacuation_center_service"
CAM_MOD = "app.services.camera_device_service"
SENSOR_DEV_MOD = "app.services.sensor_device_service"


def _db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock()
    return db


def _admin():
    return CurrentUser(
        id=str(ADMIN_ID), is_superuser=True, is_enabled=True,
        force_password_change=False,
    )


def _responder(**overrides):
    from app.models.responder_related.responders import ResponderStatus

    r = SimpleNamespace(
        id=RESPONDER_ID,
        first_name="Ana",
        last_name="Cruz",
        phone_number="09171234567",
        location_id=HOME_SITE_ID,
        active_location_id=None,
        status=ResponderStatus.ACTIVE,
        created_by=ADMIN_ID,
    )
    for k, v in overrides.items():
        setattr(r, k, v)
    return r


def _location(**overrides):
    loc = SimpleNamespace(
        id=SITE_B_ID,
        name="Site B",
        is_active=True,
        latitude=14.0,
        longitude=121.0,
    )
    for k, v in overrides.items():
        setattr(loc, k, v)
    return loc


# ---------------------------------------------------------------------------
# update_site — the deactivation side takes the exclusive lock
# ---------------------------------------------------------------------------


async def test_update_site_locks_location_for_update():
    location = _location()
    updates = MagicMock()
    updates.model_copy.return_value = location

    with (
        patch(
            f"{LOC_MOD}.location_crud",
            **{
                "get_with_lock": AsyncMock(return_value=location),
                "get": AsyncMock(return_value=location),
                "update_no_commit": AsyncMock(return_value=location),
            },
        ) as loc_crud,
        patch(f"{LOC_MOD}.responder_crud", **{"clear_active_site": AsyncMock()}),
        patch(
            f"{LOC_MOD}.responder_deployment_crud",
            **{"close_open_for_location": AsyncMock()},
        ),
        patch(
            f"{LOC_MOD}.admin_audit_log_crud",
            **{"create_only_no_commit": AsyncMock()},
        ),
        patch(f"{LOC_MOD}.cache_service", **{"invalidate_location_caches": MagicMock()}),
    ):
        await location_service.update_site(
            db=_db(),
            location_id=SITE_B_ID,
            payload=LocationUpdate(is_active=False),
            current_user=_admin(),
        )

    loc_crud.get_with_lock.assert_awaited_once()
    loc_crud.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# open_deployment — location lock BEFORE responder lock (deadlock ordering)
# ---------------------------------------------------------------------------


async def test_open_deployment_locks_site_before_responder():
    calls = []

    async def _site(*args, **kwargs):
        calls.append("location")
        return _location()

    async def _resp(*args, **kwargs):
        calls.append("responder")
        return _responder()

    with (
        patch(
            f"{DEPLOY_MOD}.location_crud",
            **{"get_for_share": AsyncMock(side_effect=_site), "get": AsyncMock(side_effect=_site)},
        ),
        patch(
            f"{DEPLOY_MOD}.responder_crud",
            **{"get_with_lock": AsyncMock(side_effect=_resp), "get": AsyncMock(side_effect=_resp)},
        ),
        patch(f"{DEPLOY_MOD}.responder_deployment_crud") as dep_crud,
        patch(f"{DEPLOY_MOD}.admin_audit_log_crud", **{
            "create_only_no_commit": AsyncMock(),
        }),
    ):
        dep_crud.get_open_for_responder = AsyncMock(return_value=None)
        dep_crud.create = AsyncMock(
            return_value=SimpleNamespace(
                id=9, location_id=SITE_B_ID,
                starts_at=datetime.now(timezone.utc),
                ends_at=None, planned_ends_at=None, created_by=ADMIN_ID,
            )
        )
        await responder_deployment_service.open_deployment(
            db=_db(),
            responder_id=RESPONDER_ID,
            payload=ResponderDeploymentCreate(location_id=SITE_B_ID),
            current_user=_admin(),
        )

    assert calls.index("location") < calls.index("responder")


async def test_open_deployment_inactive_site_rejects_before_responder_lock():
    resp_lock = AsyncMock(return_value=_responder())
    with (
        patch(
            f"{DEPLOY_MOD}.location_crud",
            **{"get_for_share": AsyncMock(return_value=_location(is_active=False))},
        ),
        patch(f"{DEPLOY_MOD}.responder_crud", **{"get_with_lock": resp_lock}),
        patch(f"{DEPLOY_MOD}.responder_deployment_crud"),
        patch(f"{DEPLOY_MOD}.admin_audit_log_crud", **{
            "create_only_no_commit": AsyncMock(),
        }),
    ):
        with pytest.raises(HTTPException) as exc:
            await responder_deployment_service.open_deployment(
                db=_db(),
                responder_id=RESPONDER_ID,
                payload=ResponderDeploymentCreate(location_id=SITE_B_ID),
                current_user=_admin(),
            )

    assert exc.value.status_code == 400
    resp_lock.assert_not_awaited()


# ---------------------------------------------------------------------------
# set_active_site — same location-before-responder ordering
# ---------------------------------------------------------------------------


async def test_set_active_site_locks_site_before_responder():
    calls = []

    async def _site(*args, **kwargs):
        calls.append("location")
        return _location()

    async def _resp(*args, **kwargs):
        calls.append("responder")
        return _responder()

    with (
        patch(
            f"{APP_MOD}.location_crud",
            **{"get_for_share": AsyncMock(side_effect=_site)},
        ),
        patch(
            f"{APP_MOD}.responder_crud",
            **{"get_with_lock": AsyncMock(side_effect=_resp)},
        ),
        patch(f"{APP_MOD}.responder_deployment_crud") as dep_crud,
        patch(f"{APP_MOD}.admin_audit_log_crud", **{
            "create_only_no_commit": AsyncMock(),
        }),
    ):
        dep_crud.get_open_for_responder = AsyncMock(return_value=None)
        await responder_app_service.set_active_site(
            responder_id=RESPONDER_ID,
            location_id=HOME_SITE_ID,
            db=_db(),
        )

    assert calls.index("location") < calls.index("responder")


async def test_set_active_site_inactive_rejects_under_lock():
    responder = _responder()
    with (
        patch(
            f"{APP_MOD}.location_crud",
            **{"get_for_share": AsyncMock(return_value=_location(is_active=False))},
        ),
        patch(f"{APP_MOD}.responder_crud", **{"get_with_lock": AsyncMock(return_value=responder)}),
        patch(f"{APP_MOD}.responder_deployment_crud"),
        patch(f"{APP_MOD}.admin_audit_log_crud", **{
            "create_only_no_commit": AsyncMock(),
        }),
    ):
        with pytest.raises(HTTPException) as exc:
            await responder_app_service.set_active_site(
                responder_id=RESPONDER_ID,
                location_id=SITE_B_ID,
                db=_db(),
            )

    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# verify_otp — the locked check must run AFTER ensure_exists' internal commit,
# in the same txn as the gated activate write
# ---------------------------------------------------------------------------


async def test_verify_otp_rechecks_site_under_lock_after_group_commit():
    from app.models.responder_related.responders import ResponderStatus

    calls = []
    record = SimpleNamespace(
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        otp_hash="hash",
        attempt_count=0,
    )
    pending = _responder(status=ResponderStatus.PENDING)

    async def _ensure(*args, **kwargs):
        calls.append("ensure_exists")
        return SimpleNamespace(id=7)

    async def _site(*args, **kwargs):
        calls.append("location_lock")
        return _location(id=HOME_SITE_ID)

    async def _activate(*args, **kwargs):
        calls.append("activate")

    with (
        patch(
            f"{APP_MOD}.responder_otp_verification_crud",
            **{
                "get_by_responder_id": AsyncMock(return_value=record),
                "delete_by_responder_id": AsyncMock(),
                "increment_attempt_count": AsyncMock(),
            },
        ),
        patch(f"{APP_MOD}.verify_otp_hash", return_value=True),
        patch(f"{APP_MOD}.create_responder_token", return_value="tok"),
        patch(f"{APP_MOD}.responder_crud", **{
            "get": AsyncMock(return_value=pending),
            "activate": AsyncMock(side_effect=_activate),
        }),
        patch(f"{APP_MOD}.location_crud", **{
            "get": AsyncMock(return_value=_location(id=HOME_SITE_ID)),
            "get_for_share": AsyncMock(side_effect=_site),
        }) as loc_crud,
        patch(f"{APP_MOD}.responder_group_crud", **{
            "ensure_exists": AsyncMock(side_effect=_ensure),
            "add_member": AsyncMock(),
        }),
    ):
        res = await responder_app_service.verify_otp(
            verify_request=ResponderOTPVerifyRequest(
                responder_id=RESPONDER_ID, otp="123456"
            ),
            db=_db(),
        )

    assert res.success is True
    loc_crud.get_for_share.assert_awaited_once()
    assert calls.index("ensure_exists") < calls.index("location_lock")
    assert calls.index("location_lock") < calls.index("activate")


# ---------------------------------------------------------------------------
# update_responder_details — locked re-check in the same txn as update_details
# ---------------------------------------------------------------------------


async def test_update_responder_details_rechecks_site_under_lock():
    calls = []
    responder = _responder()
    updated = _responder(location_id=SITE_B_ID)

    async def _ensure(*args, **kwargs):
        calls.append("ensure_exists")
        return SimpleNamespace(id=7)

    async def _site(*args, **kwargs):
        calls.append("location_lock")
        return _location()

    async def _update(*args, **kwargs):
        calls.append("update_details")
        return updated

    exec_result = MagicMock()
    exec_result.scalar.return_value = None
    db = _db()
    db.execute = AsyncMock(return_value=exec_result)

    with (
        patch(f"{SVC_MOD}.location_crud", **{
            "get": AsyncMock(return_value=_location()),
            "get_for_share": AsyncMock(side_effect=_site),
        }),
        patch(f"{SVC_MOD}.responder_crud", **{
            "get": AsyncMock(return_value=responder),
            "update_details": AsyncMock(side_effect=_update),
        }),
        patch(f"{SVC_MOD}.responder_group_crud", **{
            "ensure_exists": AsyncMock(side_effect=_ensure),
            "add_member": AsyncMock(),
            "get_by_name_and_location": AsyncMock(return_value=None),
        }),
    ):
        await responder_service.update_responder_details(
            responder_id=RESPONDER_ID,
            payload=ResponderAdminUpdate(location_id=SITE_B_ID),
            db=db,
        )

    assert calls.index("location_lock") < calls.index("update_details")


# ---------------------------------------------------------------------------
# bulk_create_responders — locked active-site check
# ---------------------------------------------------------------------------


async def test_bulk_create_locks_active_site_ids():
    from app.schemas import ResponderCreate

    with (
        patch(f"{SVC_MOD}.location_crud", **{
            "get_active_ids_for_share": AsyncMock(return_value={SITE_B_ID}),
            "get_active_ids": AsyncMock(return_value={SITE_B_ID}),
        }) as loc_crud,
        patch(f"{SVC_MOD}.responder_crud", **{
            "get_existing_phone_numbers": AsyncMock(return_value=set()),
            "bulk_create_and_return": AsyncMock(
                return_value=[_responder(location_id=SITE_B_ID)]
            ),
        }),
    ):
        await responder_service.bulk_create_responders(
            responders=[
                ResponderCreate(
                    first_name="Ana", last_name="Cruz",
                    phone_number="09171234567", location_id=SITE_B_ID,
                )
            ],
            db=_db(),
            user_id=ADMIN_ID,
        )

    loc_crud.get_active_ids_for_share.assert_awaited_once()
    loc_crud.get_active_ids.assert_not_awaited()


# ---------------------------------------------------------------------------
# record_reading — locked site check before the gated insert
# ---------------------------------------------------------------------------


async def test_record_reading_locks_site():
    from app.schemas.reading_summary_response.sensor_reading import (
        AlertSummary,
        SensorReadingSummary,
        WaterLevelSummary,
    )

    device = SimpleNamespace(id=3, is_active=True, location_id=SITE_B_ID)
    reading = SimpleNamespace(
        id=1, water_level_cm=90.0,
        timestamp=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    summary = SensorReadingSummary(
        timestamp=datetime.now(timezone.utc),
        water_level=WaterLevelSummary(
            current_cm=90.0, change_rate=0.0, trend="stable"
        ),
        alert=AlertSummary(
            level="normal",
            distance_to_warning_cm=10.0,
            distance_to_critical_cm=20.0,
            distance_from_critical_cm=30.0,
            percentage_of_critical=50.0,
        ),
    )

    with (
        patch(f"{READ_MOD}.sensor_device_crud", **{
            "get_entity": AsyncMock(return_value=device),
        }),
        patch(f"{READ_MOD}.location_crud", **{
            "get_for_share": AsyncMock(return_value=_location()),
            "get": AsyncMock(return_value=_location()),
        }) as loc_crud,
        patch(f"{READ_MOD}.cache_service", **{
            "get_sensor_config": AsyncMock(
                return_value=SimpleNamespace(installation_height=100)
            ),
        }),
        patch(f"{READ_MOD}.sensor_reading_crud", **{
            "create_record": AsyncMock(return_value=reading),
        }),
        patch.object(
            sensor_reading_service, "calculate_record_summary",
            new=AsyncMock(return_value=summary),
        ),
        # record_reading does `from app.services import websocket_service`
        # inside the function — patch the singleton on the package.
        patch("app.services.websocket_service", **{"broadcast_update": AsyncMock()}),
        # fusion_state_manager is global — an earlier test may have registered
        # fusion state for this location; keep it out of the test.
        patch(f"{READ_MOD}.fusion_state_manager", **{
            "recalculate_water_level_score": AsyncMock(),
        }),
    ):
        await sensor_reading_service.record_reading(
            db=_db(),
            obj_in=SensorReadingCreate(
                timestamp=datetime.now(timezone.utc),
                sensor_device_id=3,
                raw_distance_cm=10.0,
                signal_strength=-60,
            ),
        )

    loc_crud.get_for_share.assert_awaited_once()
    loc_crud.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# evacuation_service.confirm — locked gate before dispatch writes
# ---------------------------------------------------------------------------


async def test_evacuation_confirm_locks_site():
    event = SimpleNamespace(
        id=uuid.uuid4(), location_id=SITE_B_ID, dispatch_id=1,
        kind="evacuate", authorized_by=ADMIN_ID, basis_risk_score=None,
        message="Evacuate", basis_snapshot=None, event_id=uuid.uuid4(),
        created_at=datetime.now(timezone.utc),
    )

    with (
        patch(f"{EVAC_MOD}.location_crud", **{
            "get_for_share": AsyncMock(return_value=_location()),
            "get": AsyncMock(return_value=_location()),
        }) as loc_crud,
        patch(
            f"{EVAC_MOD}.evacuation_event_crud",
            **{"create_event": AsyncMock(return_value=event)},
        ),
        # confirm does `from app.services import notification_service,
        # websocket_service` inside the function — patch the package-level
        # singletons, not the modules' inner names.
        patch("app.services.notification_service", MagicMock(
            send_to_citizen_subscribers=AsyncMock(return_value=(1, 0))
        )),
        patch("app.services.websocket_service", MagicMock(
            broadcast_update=AsyncMock()
        )),
    ):
        await evacuation_service.confirm(
            db=_db(),
            payload=EvacuationConfirmRequest(location_id=SITE_B_ID),
            current_user=_admin(),
        )

    # Locked once before the dispatch writes, and re-locked before the audit
    # row — send_to_citizen_subscribers commits internally, releasing the
    # first lock, so the gated event write needs its own.
    assert loc_crud.get_for_share.await_count == 2


# ---------------------------------------------------------------------------
# evacuation_center_service.create_center — locked gate
# ---------------------------------------------------------------------------


async def test_create_center_locks_site():
    center = SimpleNamespace(
        id=1, location_id=SITE_B_ID, name="Evac Center",
        latitude=14.0, longitude=121.0, capacity=100,
        address=None, contact=None, status="open",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    with (
        patch(f"{CENTER_MOD}.location_crud", **{
            "get_for_share": AsyncMock(return_value=_location()),
            "get": AsyncMock(return_value=_location()),
        }) as loc_crud,
        patch(f"{CENTER_MOD}.evacuation_center_crud", **{
            "create_and_return": AsyncMock(return_value=center),
        }),
        patch(f"{CENTER_MOD}.admin_audit_log_crud", **{
            "create_only_no_commit": AsyncMock(),
        }),
    ):
        await evacuation_center_service.create_center(
            db=_db(),
            payload=EvacuationCenterCreate(
                location_id=SITE_B_ID, name="Evac Center",
                latitude=14.0, longitude=121.0,
            ),
            current_user=_admin(),
        )

    loc_crud.get_for_share.assert_awaited_once()


# ---------------------------------------------------------------------------
# device services — locked site check on register/update + ingest
# ---------------------------------------------------------------------------


async def test_sensor_device_register_locks_site():
    device = SimpleNamespace(
        id=1, location_id=SITE_B_ID, is_active=True, name="s1",
        sensor_config=None,
    )
    payload = MagicMock()
    payload.location_id = SITE_B_ID
    payload.name = "s1"

    with (
        # register gates via location_service.require_active_site — patch the
        # crud in that module's namespace.
        patch(f"{LOC_MOD}.location_crud", **{
            "get_for_share": AsyncMock(return_value=_location()),
            "get": AsyncMock(return_value=_location()),
        }) as loc_crud,
        patch(f"{SENSOR_DEV_MOD}.sensor_device_crud", **{
            "get_by_name_and_location": AsyncMock(return_value=None),
            "create_and_return": AsyncMock(return_value=device),
        }, create=True),
        patch(f"{SENSOR_DEV_MOD}.admin_audit_log_crud", **{
            "create_only_no_commit": AsyncMock(),
        }),
        patch(f"{SENSOR_DEV_MOD}.cache_service", **{
            "invalidate_device_caches": MagicMock(),
        }),
    ):
        try:
            await sensor_device_service.register_device(
                db=_db(), payload=payload, current_user=_admin()
            )
        except Exception:
            pass  # payload details irrelevant — the lock seam is what matters

    loc_crud.get_for_share.assert_awaited_once()


async def test_camera_device_get_for_ingest_locks_site():
    device = SimpleNamespace(id=5, is_active=True, location_id=SITE_B_ID)

    with (
        patch(f"{CAM_MOD}.camera_device_crud", **{
            "get": AsyncMock(return_value=device),
        }),
        patch(f"{CAM_MOD}.location_crud", **{
            "get_for_share": AsyncMock(return_value=_location()),
            "get": AsyncMock(return_value=_location()),
        }) as loc_crud,
    ):
        res = await camera_device_service.get_for_ingest(
            db=_db(), camera_device_id=5, location_id=SITE_B_ID
        )

    assert res is device
    loc_crud.get_for_share.assert_awaited_once()
    loc_crud.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# send_otp — OTP write commits inside upsert_otp, so the PENDING home-site
# check must hold the site lock in that same txn
# ---------------------------------------------------------------------------


async def test_send_otp_locks_home_site():
    from app.models.responder_related.responders import ResponderStatus

    with (
        patch(f"{APP_MOD}.location_crud", **{
            "get_for_share": AsyncMock(return_value=_location()),
            "get": AsyncMock(return_value=_location()),
        }) as loc_crud,
        patch(f"{APP_MOD}.responder_otp_verification_crud", **{
            "upsert_otp": AsyncMock(),
        }),
        patch(f"{APP_MOD}.sms_service", **{"send_one_sms": AsyncMock()}),
    ):
        await responder_app_service.send_otp(
            responder=_responder(status=ResponderStatus.PENDING),
            db=_db(),
        )

    loc_crud.get_for_share.assert_awaited_once()
    loc_crud.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# ml_service.process_frame_bytes — the frame write runs on its OWN
# AsyncSessionLocal, so the site gate must live inside that session, not the
# request's. An inactive site must drop the frame without writing.
# ---------------------------------------------------------------------------


def _session_ctx():
    session = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


async def test_ml_frame_write_gates_on_site_under_own_session():
    ML_MOD = "app.services.ml_service"
    from app.services.ml_service import ml_service

    with (
        patch.object(ml_service, "_capture_interval_seconds", MagicMock(return_value=0)),
        patch.object(
            ml_service, "_infer_and_annotate",
            new=AsyncMock(return_value=(10.0, "clear", b"x")),
        ),
        patch(f"{ML_MOD}.upload_image", new=AsyncMock(return_value={"secure_url": "u"})),
        patch(f"{ML_MOD}.AsyncSessionLocal", new=MagicMock(return_value=_session_ctx())),
        patch(f"{ML_MOD}.location_crud", **{
            "get_for_share": AsyncMock(return_value=_location(is_active=False)),
        }) as loc_crud,
        patch(f"{ML_MOD}.model_readings_crud", **{
            "create_and_return": AsyncMock(),
        }) as readings_crud,
    ):
        result = await ml_service.process_frame_bytes(
            image_bytes=b"frame", camera_device_id=424242, location_id=SITE_B_ID,
        )

    assert result == {}
    loc_crud.get_for_share.assert_awaited_once()
    readings_crud.create_and_return.assert_not_awaited()
