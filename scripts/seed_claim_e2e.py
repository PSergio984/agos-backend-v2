"""Seed fixtures for the claim-flow E2E check (agos-patrol-mobile #18).

Creates on the configured DATABASE_URL:
- location "E2E Claim Test Site" (get-or-create)
- 2 ACTIVE responders with fixed test phone numbers (get-or-create)
- 2 BLOCKAGE dispatches on that site, each with SENT deliveries to both
  responders: one for the HTTP smoke test, one left unclaimed for the
  on-device half of the checklist.

Prints responder ids/phones, claim tokens, and dispatch ids.
Re-run to mint fresh unclaimed dispatches for another device pass.

Usage (repo venv):  python scripts/seed_claim_e2e.py
"""

import asyncio
import secrets
import sys
from datetime import datetime, timezone

from sqlalchemy import select

sys.path.insert(0, ".")
from app.core.database import AsyncSessionLocal  # noqa: E402
from app.core.security import create_responder_token, get_password_hash  # noqa: E402
from app.models import AdminUser, Location, Responder  # noqa: E402
from app.models.notification_dispatch import NotificationDispatch  # noqa: E402
from app.models.notification_template import NotificationType  # noqa: E402
from app.models.responder_related.notification_delivery import (  # noqa: E402
    DeliveryStatus,
    NotificationDelivery,
)
from app.models.responder_related.responders import ResponderStatus  # noqa: E402

SITE_NAME = "E2E Claim Test Site"
PHONES = ["+639171180001", "+639171180002"]
NAMES = [("E2E", "ClaimOne"), ("E2E", "ClaimTwo")]


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
            print(f"  token: {create_responder_token(str(r.id))}")
        for label, did in dispatch_ids:
            print(f"dispatch[{label}]: {did}")


if __name__ == "__main__":
    asyncio.run(main())
