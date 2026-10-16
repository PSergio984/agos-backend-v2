"""F4 — human-gated public evacuation workflow.

Fusion may *recommend* evacuation (admin-facing WS); only an admin may *dispatch*
a public alert. This service performs the dispatch + audit, and builds the
recommendation payload consumed by ``state.py``.
"""

import time
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser
from app.core.state import fusion_state_manager
from app.crud import evacuation_event_crud, location_crud
from app.models.evacuation_event import EvacuationEventKind
from app.models.notification_template import NotificationType
from app.schemas import (
    EvacuationConfirmRequest,
    EvacuationEventResponse,
    EvacuationRecommendation,
    PublicAlertPayload,
)

# Reject a second confirm for the same location+kind inside this window so two
# admins can't double-blast the public.
CONFIRM_DEDUPE_SECONDS = 60


def build_suggested_message(alert_name: str, risk_score: int) -> str:
    return (
        "Flood risk in your area has reached a critical level. "
        "Please prepare to evacuate and follow instructions from local authorities."
    )


def build_recommendation(location_id: int, fusion_analysis) -> EvacuationRecommendation:
    """Assemble the admin-facing recommendation payload from a fusion snapshot."""
    fusion_data = fusion_analysis.fusion_data
    return EvacuationRecommendation(
        recommendation_id=str(uuid.uuid4()),
        location_id=location_id,
        risk_score=fusion_data.combined_risk_score,
        alert_name=fusion_data.alert_name,
        triggered_conditions=list(fusion_data.triggered_conditions),
        suggested_message=build_suggested_message(
            fusion_data.alert_name, fusion_data.combined_risk_score
        ),
        created_at=datetime.now(timezone.utc),
    )


class EvacuationService:
    def __init__(self):
        # (location_id, kind) -> monotonic timestamp of last dispatch
        self._last_dispatch: dict[tuple[int, str], float] = {}

    def _guard_double_dispatch(self, location_id: int, kind: str) -> None:
        now = time.monotonic()
        key = (location_id, kind)
        last = self._last_dispatch.get(key)
        if last is not None and now - last < CONFIRM_DEDUPE_SECONDS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A public alert for this location was just dispatched. Please wait a moment.",
            )
        self._last_dispatch[key] = now

    def _fusion_snapshot(self, location_id: int):
        """Return (risk_score, snapshot_dict) for the audit basis, if available."""
        try:
            fusion_analysis = fusion_state_manager.get_fusion_analysis_state(location_id)
        except ValueError:
            return None, None
        if not fusion_analysis:
            return None, None
        return (
            fusion_analysis.fusion_data.combined_risk_score,
            fusion_analysis.model_dump(mode="json"),
        )

    async def confirm(
        self, db: AsyncSession, payload: EvacuationConfirmRequest, current_user: CurrentUser
    ) -> EvacuationEventResponse:
        from app.services import notification_service, websocket_service

        is_all_clear = payload.kind == "all_clear"

        # An evacuation targets a real, active site — an unknown id would
        # otherwise fail on the FK at commit; an inactive one shouldn't get
        # citizen pushes or live alerts. Checked before the dedup guard so a
        # rejected call never poisons the double-dispatch cache.
        site = await location_crud.get(db=db, id=payload.location_id)
        if site is None or not site.is_active:
            raise HTTPException(status_code=400, detail="Site is not active.")

        self._guard_double_dispatch(payload.location_id, payload.kind)

        if is_all_clear:
            title = "All Clear"
            default_message = (
                "The flood threat in your area has subsided. It is now safe. "
                "Continue to stay alert and follow local advisories."
            )
            level = "all_clear"
            notif_type = NotificationType.ANNOUNCEMENT
            event_kind = EvacuationEventKind.ALL_CLEAR
        else:
            title = "Evacuation Order"
            default_message = build_suggested_message("Critical", 0)
            level = "evacuate"
            notif_type = NotificationType.CRITICAL
            event_kind = EvacuationEventKind.EVACUATE

        message = (payload.message or default_message).strip()
        if not message:
            raise HTTPException(status_code=400, detail="Alert message cannot be empty.")

        basis_risk_score, basis_snapshot = self._fusion_snapshot(payload.location_id)

        # One identity shared by the live WS alert and the audit row, so a client
        # can dedupe the live alert against the fetched history.
        event_id = uuid.uuid4()
        event_ts = datetime.now(timezone.utc)

        # 1. Fire-and-forget push to this location's citizen subscribers.
        dispatch_id, sent_count = await notification_service.send_to_citizen_subscribers(
            db=db,
            location_id=payload.location_id,
            title=title,
            message=message,
            notif_type=notif_type,
        )

        # 2. Broadcast the public_alert over WS (location-scoped) for connected apps.
        alert_payload = PublicAlertPayload(
            id=str(event_id),
            level=level,
            title=title,
            message=message,
            location_id=payload.location_id,
            timestamp=event_ts,
        )
        await websocket_service.broadcast_update(
            update_type="public_alert",
            data=alert_payload.model_dump(mode="json"),
            location_id=payload.location_id,
        )

        # 3. Audit row — the authorization happened regardless of subscriber count.
        event = await evacuation_event_crud.create_event(
            db=db,
            location_id=payload.location_id,
            kind=event_kind,
            authorized_by=uuid.UUID(str(current_user.id)),
            message=message,
            dispatch_id=dispatch_id,
            basis_risk_score=basis_risk_score,
            basis_snapshot=basis_snapshot,
            event_id=event_id,
            created_at=event_ts,
        )

        print(
            f"📣 [PUBLIC ALERT] {level} for location {payload.location_id} "
            f"by admin {current_user.id} → {sent_count} citizen device(s)"
        )
        return EvacuationEventResponse.model_validate(event)

    async def get_events(
        self, db: AsyncSession, location_id: int
    ) -> list[EvacuationEventResponse]:
        events = await evacuation_event_crud.get_by_location(db=db, location_id=location_id)
        return [EvacuationEventResponse.model_validate(e) for e in events]

    async def get_public_alerts(
        self, db: AsyncSession, location_id: int, limit: int = 50
    ) -> list[PublicAlertPayload]:
        """Public, PII-free alert history for the citizen app (back-fill on launch).

        Derived from the audit events; only the citizen-facing fields are exposed
        (no authorizer, dispatch id, or fusion snapshot)."""
        events = await evacuation_event_crud.get_by_location(
            db=db, location_id=location_id, limit=limit
        )
        alerts: list[PublicAlertPayload] = []
        for event in events:
            if event.kind == EvacuationEventKind.ALL_CLEAR:
                level, title = "all_clear", "All Clear"
            else:
                level, title = "evacuate", "Evacuation Order"
            alerts.append(
                PublicAlertPayload(
                    id=str(event.id),
                    level=level,
                    title=title,
                    message=event.message,
                    location_id=event.location_id,
                    timestamp=event.created_at,
                )
            )
        return alerts


evacuation_service = EvacuationService()
