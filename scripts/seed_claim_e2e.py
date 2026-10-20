"""Seed fixtures for the claim-flow E2E check (agos-patrol-mobile #18).

Creates on the configured DATABASE_URL:
- 3 locations: "E2E Claim Test Site" (home site) plus 2 extra sites used to
  exercise multi-site (presence/deployment) flows (get-or-create)
- 2 ACTIVE responders with fixed test phone numbers (get-or-create)
- open ResponderDeployment rows posting both responders to the extra sites,
  which is what makes them appear in the app's site picker
- 2 sensor devices + 2 camera devices per site (get-or-create), and a short
  sensor-reading trend on each site's primary sensor so the sparkline has data
- 2 BLOCKAGE dispatches on the home site, each with SENT deliveries to both
  responders: one for the HTTP smoke test, one left unclaimed for the
  on-device half of the checklist.

Prints responder ids/phones, claim tokens, and dispatch ids.
Re-run to mint fresh unclaimed dispatches for another device pass; the
site/device/deployment fixtures are get-or-create so they don't duplicate.

Usage (repo venv):  python scripts/seed_claim_e2e.py
"""

import asyncio
import random
import secrets
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

sys.path.insert(0, ".")
from app.core.database import AsyncSessionLocal  # noqa: E402
from app.core.security import get_password_hash  # noqa: E402
from app.models import AdminUser, Location, Responder  # noqa: E402
from app.models.data_sources.camera_device import CameraDevice  # noqa: E402
from app.models.data_sources.model_readings import ModelReadings  # noqa: E402
from app.models.data_sources.sensor_device import SensorConfig, SensorDevice  # noqa: E402
from app.models.data_sources.sensor_reading import SensorReading  # noqa: E402
from app.models.notification_dispatch import NotificationDispatch  # noqa: E402
from app.models.notification_template import NotificationType  # noqa: E402
from app.models.responder_related.notification_delivery import (  # noqa: E402
    DeliveryStatus,
    NotificationDelivery,
)
from app.models.responder_related.responder_deployment import (  # noqa: E402
    ResponderDeployment,
)
from app.models.responder_related.responders import ResponderStatus  # noqa: E402

SITE_NAME = "E2E Claim Test Site"
# (name, lat, lng) — all within Metro Manila so a map view looks sane.
EXTRA_SITES = [
    ("E2E North Site", 14.6760, 121.0437),
    ("E2E Riverside Site", 14.5650, 121.0340),
]
# Stored in E.164 form — the app's phone normalizer converts 09... -> +63...
# before lookup, and get_by_phone_number is an exact match.
PHONES = ["+639171180001", "+639171180002", "+639271676511"]
NAMES = [("E2E", "ClaimOne"), ("E2E", "ClaimTwo"), ("E2E", "UserPhone")]


async def main() -> None:
    async with AsyncSessionLocal() as db:
        admin = (await db.execute(select(AdminUser).limit(1))).scalar_one_or_none()
        if admin is None:
            admin = AdminUser(
                phone_number="+639000000000",
                first_name="E2E",
                last_name="Admin",
                hashed_password=get_password_hash(secrets.token_urlsafe(32)),
                is_superuser=False,
                is_enabled=False,
                force_password_change=False,
            )
            db.add(admin)
            await db.flush()
            print(f"created admin_user {admin.id} (disabled, seed-only)")

        site = (
            await db.execute(select(Location).where(Location.name == SITE_NAME))
        ).scalar_one_or_none()
        if site is None:
            site = Location(name=SITE_NAME, latitude=14.5995, longitude=120.9842, is_active=True)
            db.add(site)
            await db.flush()
        elif not site.is_active:
            site.is_active = True
        print(f"site: id={site.id} name={site.name!r} active={site.is_active}")

        sites = [site]
        for name, lat, lng in EXTRA_SITES:
            extra = (
                await db.execute(select(Location).where(Location.name == name))
            ).scalar_one_or_none()
            if extra is None:
                extra = Location(name=name, latitude=lat, longitude=lng, is_active=True)
                db.add(extra)
                await db.flush()
            elif not extra.is_active:
                extra.is_active = True
            sites.append(extra)
            print(f"site: id={extra.id} name={extra.name!r} active={extra.is_active}")

        # Devices per site: 2 sensors + 2 cameras. The responder app's
        # config/by-location resolves the oldest ACTIVE sensor on the site, so
        # the first device listed is the one the app actually reads.
        sensor_configs = [
            SensorConfig(installation_height=400, warning_threshold=120, critical_threshold=220),
            SensorConfig(installation_height=350, warning_threshold=100, critical_threshold=180),
        ]
        # ASCII-only device names — an em-dash here round-trips through the
        # connection encoding mangled, which breaks the get-or-create match.
        for s in sites:
            for idx in range(2):
                sensor_name = f"{s.name} - Ultrasonic {idx + 1}"
                existing = (
                    await db.execute(
                        select(SensorDevice).where(
                            SensorDevice.location_id == s.id,
                            SensorDevice.device_name == sensor_name,
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    db.add(
                        SensorDevice(
                            location_id=s.id,
                            device_name=sensor_name,
                            sensor_config=sensor_configs[idx],
                            is_active=True,
                        )
                    )
            for cam_suffix in ("Culvert Cam", "Bridge Cam"):
                cam_name = f"{s.name} - {cam_suffix}"
                existing_cam = (
                    await db.execute(
                        select(CameraDevice).where(
                            CameraDevice.location_id == s.id,
                            CameraDevice.device_name == cam_name,
                        )
                    )
                ).scalar_one_or_none()
                if existing_cam is None:
                    db.add(
                        CameraDevice(
                            location_id=s.id,
                            device_name=cam_name,
                            stream_url=None,
                            is_active=True,
                        )
                    )
            await db.flush()

            # A short trend on the site's primary sensor so the sparkline has
            # data — skipped on re-runs that already have readings.
            primary_sensor = (
                await db.execute(
                    select(SensorDevice)
                    .where(SensorDevice.location_id == s.id)
                    .order_by(SensorDevice.id)
                    .limit(1)
                )
            ).scalar_one()
            existing_readings = (
                await db.execute(
                    select(SensorReading)
                    .where(SensorReading.sensor_device_id == primary_sensor.id)
                    .order_by(SensorReading.timestamp)
                )
            ).scalars().all()
            now = datetime.now(timezone.utc)
            # SENSOR_WARNING_PERIOD_MINUTES=8 — stale readings make the WS send
            # "no recent data", so re-stamp the latest rows fresh on every run.
            if existing_readings:
                for i, reading in enumerate(existing_readings):
                    reading.timestamp = now - timedelta(minutes=1 * (len(existing_readings) - i))
            else:
                for i, cm in enumerate((60, 72, 85, 95, 110, 125)):
                    db.add(
                        SensorReading(
                            sensor_device_id=primary_sensor.id,
                            water_level_cm=cm,
                            raw_distance_cm=cm + 10,
                            signal_strength=-62,
                            timestamp=now - timedelta(minutes=1 * (6 - i)),
                        )
                    )
            await db.flush()

            # Fresh blockage reading per site on every run — the model feed is
            # normally live hardware, so a new randomized row each seed keeps
            # the detection card fresh and varied across test sessions.
            primary_cam = (
                await db.execute(
                    select(CameraDevice)
                    .where(CameraDevice.location_id == s.id)
                    .order_by(CameraDevice.id)
                    .limit(1)
                )
            ).scalar_one()
            status, pct = random.choice(
                [
                    ("clear", random.uniform(0, 15)),
                    ("partial", random.uniform(30, 60)),
                    ("blocked", random.uniform(75, 100)),
                ]
            )
            db.add(
                ModelReadings(
                    camera_device_id=primary_cam.id,
                    image_path="seed/e2e-blockage.jpg",
                    blockage_percentage=round(pct, 1),
                    blockage_status=status,
                    timestamp=now,
                )
            )
            await db.flush()
            print(f"  {s.name}: blockage={status} ({pct:.0f}%)")
        print("devices: 2 sensors + 2 cameras per site, trend on primary sensor")

        responders = []
        for phone, (first, last) in zip(PHONES, NAMES):
            r = (
                await db.execute(select(Responder).where(Responder.phone_number == phone))
            ).scalar_one_or_none()
            if r is None:
                r = Responder(
                    phone_number=phone,
                    first_name=first,
                    last_name=last,
                    status=ResponderStatus.ACTIVE,
                    location_id=site.id,
                    activated_at=datetime.now(timezone.utc),
                    created_by=admin.id,
                )
                db.add(r)
                await db.flush()
            else:
                r.status = ResponderStatus.ACTIVE
                r.location_id = site.id
            responders.append(r)
            print(f"responder: id={r.id} phone={r.phone_number} status={r.status.value}")

        # One open deployment per responder (uq_responder_deployments_one_open
        # enforces a single open deployment) — responders cycle over the extra
        # sites, so the two accounts land on different extra sites.
        for i, r in enumerate(responders):
            s = sites[1 + (i % (len(sites) - 1))]
            # The unique index caps each responder at ONE open deployment —
            # match on responder alone, not responder+location, or a stale
            # open deployment elsewhere would make the insert below explode.
            existing_dep = (
                await db.execute(
                    select(ResponderDeployment).where(
                        ResponderDeployment.responder_id == r.id,
                        ResponderDeployment.ends_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if existing_dep is not None and existing_dep.location_id != s.id:
                existing_dep.ends_at = datetime.now(timezone.utc)
                await db.flush()
                existing_dep = None
            if existing_dep is None:
                db.add(
                    ResponderDeployment(
                        responder_id=r.id,
                        location_id=s.id,
                        starts_at=datetime.now(timezone.utc),
                        created_by=admin.id,
                    )
                )
        await db.flush()
        print("deployments: 1 open deployment per responder (max allowed)")

        dispatch_ids = []
        for label in ("smoke", "device"):
            d = NotificationDispatch(
                type=NotificationType.BLOCKAGE,
                title=f"E2E blockage dispatch ({label})",
                message="Seeded for claim-flow verification.",
                location_id=site.id,
            )
            db.add(d)
            await db.flush()
            for r in responders:
                db.add(
                    NotificationDelivery(
                        dispatch_id=d.id,
                        responder_id=r.id,
                        status=DeliveryStatus.SENT,
                        sent_at=datetime.now(timezone.utc),
                    )
                )
            dispatch_ids.append((label, d.id))

        await db.commit()

        print("\n--- smoke inputs ---")
        for (first, last), r in zip(NAMES, responders):
            print(f"{first} {last}: {r.id}")
        print("(mint tokens ad hoc with create_responder_token — don't print them)")
        for label, did in dispatch_ids:
            print(f"dispatch[{label}]: {did}")


if __name__ == "__main__":
    asyncio.run(main())
