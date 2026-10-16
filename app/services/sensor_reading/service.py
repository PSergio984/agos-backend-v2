import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.state import fusion_state_manager
from app.crud import location_crud, sensor_reading_crud, sensor_device_crud
from app.models import SensorReading
from app.schemas import (
    SensorReadingCreate,
    SensorReadingResponse,
    SensorReadingPaginatedResponse,
    SensorDataRecordedResponse,
    SensorWebSocketResponse,
    SensorReadingSummary,
    WaterLevelSummary,
    AlertSummary,
    WaterLevelStatus,
)
from app.services.cache_service import cache_service
from app.utils.sensor_utils import get_status_and_change_rate

from .trend_service import get_readings_trend
from .export_service import get_readings_for_export


logger = logging.getLogger(__name__)


class SensorReadingService:
    def get_signal_quality(self, signal_strength: int) -> str:
        """Expose utility for use by sensor_device_service and others."""
        from app.utils.sensor_utils import get_signal_quality as _get_signal_quality
        return _get_signal_quality(signal_strength)

    async def get_items_paginated(
        self,
        db: AsyncSession,
        sensor_device_id: int,
        page: int = 1,
        page_size: int = 10,
    ) -> SensorReadingPaginatedResponse:
        db_items = await sensor_reading_crud.get_items_paginated(
            db=db,
            page=page,
            page_size=page_size,
            sensor_device_id=sensor_device_id,
        )

        items = []
        for item in db_items[:page_size]:
            status, change_rate = get_status_and_change_rate(
                item.water_level_cm, item.prev_water_level
            )
            items.append(
                SensorReadingResponse(
                    id=item.id,
                    timestamp=item.timestamp,
                    water_level_cm=item.water_level_cm,
                    status=status,
                    change_rate=change_rate,
                )
            )

        has_more = len(db_items) > page_size
        return SensorReadingPaginatedResponse(items=items[:page_size], has_more=has_more)

    async def get_readings_trend(
        self, db: AsyncSession, duration: str, sensor_device_id: int
    ):
        return await get_readings_trend(db=db, duration=duration, sensor_device_id=sensor_device_id)

    async def get_available_reading_days(
        self, db: AsyncSession, sensor_device_id: int
    ) -> list[str]:
        return await sensor_reading_crud.get_available_reading_days(
            db=db, sensor_device_id=sensor_device_id
        )

    async def get_readings_for_export(
        self,
        db: AsyncSession,
        start_datetime: datetime,
        end_datetime: datetime,
        sensor_device_id: int,
    ):
        return await get_readings_for_export(
            db=db,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            sensor_device_id=sensor_device_id,
        )

    async def record_reading(
        self, db: AsyncSession, obj_in: SensorReadingCreate
    ) -> SensorDataRecordedResponse:
        from app.services import websocket_service

        sensor_device = await sensor_device_crud.get_entity(
            db=db, id=obj_in.sensor_device_id
        )
        if sensor_device is None or not sensor_device.is_active:
            return SensorDataRecordedResponse(
                timestamp=datetime.now(timezone.utc),
                status="Error: Sensor device not found",
            )

        # A deactivated site stops accepting readings even while its devices
        # are still individually marked active.
        site = await location_crud.get(db=db, id=sensor_device.location_id)
        if site is None or not site.is_active:
            return SensorDataRecordedResponse(
                timestamp=datetime.now(timezone.utc),
                status="Error: Sensor device site is inactive",
            )

        sensor_config = await cache_service.get_sensor_config(
            db=db, sensor_device_id=obj_in.sensor_device_id
        )
        computed_water_level_cm = (
            sensor_config.installation_height - obj_in.raw_distance_cm
        )
        if computed_water_level_cm < 0:
            logger.warning(
                "Computed negative water_level_cm; clamping to 0. "
                "raw_distance_cm=%s installation_height=%s",
                obj_in.raw_distance_cm,
                sensor_config.installation_height,
            )
        water_level_cm = max(0.0, computed_water_level_cm)

        data = obj_in.model_dump()
        db_obj = SensorReading(**data)
        db_obj.water_level_cm = water_level_cm

        db_reading: SensorReading = await sensor_reading_crud.create_record(
            db=db, db_obj=db_obj
        )
        calculated_summary = await self.calculate_record_summary(db=db, reading=db_reading)

        response = SensorWebSocketResponse(
            status="success",
            message="Retrieved successfully",
            sensor_reading=calculated_summary,
        )
        # The device row is authoritative — the reverse cache only maps each
        # site's default sensor, so a second sensor on a site would raise.
        location_id = sensor_device.location_id
        await websocket_service.broadcast_update(
            update_type="sensor_update",
            data=response.model_dump(mode="json"),
            location_id=location_id,
        )

        await fusion_state_manager.recalculate_water_level_score(
            water_level_status=WaterLevelStatus(
                water_level_cm=db_reading.water_level_cm,
                timestamp=db_reading.timestamp,
                critical_percentage=calculated_summary.alert.percentage_of_critical,
                trend=calculated_summary.water_level.trend,
                change_rate=calculated_summary.water_level.change_rate,
            ),
            location_id=location_id,
        )

        return SensorDataRecordedResponse(
            timestamp=db_reading.created_at,
            status="Success: Reading recorded",
        )

    async def calculate_record_summary(
        self, db: AsyncSession, reading: SensorReading
    ) -> SensorReadingSummary:
        prev_reading = await sensor_reading_crud.get_previous_reading(
            db=db,
            before_timestamp=reading.timestamp,
            sensor_device_id=reading.sensor_device_id,
        )
        water_level_summary = self._calculate_water_level_summary(
            current_cm=reading.water_level_cm, prev_reading=prev_reading
        )
        alert_summary = await self._calculate_alert_summary(
            current_cm=reading.water_level_cm,
            db=db,
            sensor_device_id=reading.sensor_device_id,
        )
        return SensorReadingSummary(
            timestamp=reading.timestamp,
            water_level=water_level_summary,
            alert=alert_summary,
        )

    def _calculate_water_level_summary(
        self, current_cm: float, prev_reading: SensorReading | None
    ) -> WaterLevelSummary:
        change_rate = 0.0
        if prev_reading:
            change_rate = round(current_cm - prev_reading.water_level_cm, 2)

        trend = "stable"
        if change_rate > 1:
            trend = "rising"
        elif change_rate < -1:
            trend = "falling"

        return WaterLevelSummary(
            current_cm=current_cm,
            change_rate=change_rate,
            trend=trend,
        )

    async def _calculate_alert_summary(
        self, current_cm: float, db: AsyncSession, sensor_device_id: int
    ) -> AlertSummary:
        sensor_config = await cache_service.get_sensor_config(
            db=db, sensor_device_id=sensor_device_id
        )
        current_cm = float(current_cm)
        warn = float(sensor_config.warning_threshold)
        crit = float(sensor_config.critical_threshold)

        level = "normal"
        if current_cm >= crit:
            level = "critical"
        elif current_cm >= warn:
            level = "warning"

        if crit:
            percentage_of_critical = round((current_cm / crit) * 100, 1)
        else:
            logger.warning(
                "critical_threshold is 0; using 0.0 for percentage_of_critical"
            )
            percentage_of_critical = 0.0

        return AlertSummary(
            level=level,
            distance_to_warning_cm=round(warn - current_cm, 1),
            distance_from_warning_cm=round(max(0, warn - current_cm), 1),
            distance_to_critical_cm=round(crit - current_cm, 1),
            distance_from_critical_cm=round(max(0, current_cm - crit), 1),
            percentage_of_critical=percentage_of_critical,
        )


sensor_reading_service = SensorReadingService()
