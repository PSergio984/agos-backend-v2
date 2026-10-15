"""Daily summary service: orchestration and CRUD."""

from datetime import date, datetime, timezone, timedelta

from sqlalchemy import select, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.crud.daily_summary import daily_summary_crud
from app.crud.system_settings import system_settings_crud
from app.models.data_sources.sensor_reading import SensorReading
from app.models.data_sources.model_readings import ModelReadings
from app.models.data_sources.weather import Weather
from app.schemas import DailySummaryResponse
from app.services.cache_service import cache_service

from .summary_generator import (
    extract_water_level_summary,
    extract_model_readings_summary,
    extract_weather_summary,
    calculate_risk_scores,
    smooth_model_readings,
)


class DailySummaryService:
    async def generate_summary_for_location(
        self, db: AsyncSession, location_id: int, target_date: date
    ) -> dict:
        """Generate daily summary data for a single location."""
        local_start = datetime.combine(target_date, datetime.min.time()).replace(
            tzinfo=settings.APP_TIMEZONE
        )
        local_end = local_start + timedelta(days=1)
        start_of_day = local_start.astimezone(timezone.utc)
        end_of_day = local_end.astimezone(timezone.utc)

        device_ids = await cache_service.get_device_ids_per_location(db, location_id)

        # A site with no active sensor device has no per-device config; its
        # summary simply has no sensor-derived risk (critical_level only feeds
        # sensor-event critical_percentage).
        critical_level = 0.0
        if device_ids.sensor_device_id:
            sensor_config = await cache_service.get_sensor_config(
                db, sensor_device_id=device_ids.sensor_device_id
            )
            if not sensor_config:
                raise ValueError("Missing sensor config from cache_service.get_sensor_config()")
            critical_level = float(sensor_config.critical_threshold)

        sensor_context = []
        model_context = []
        weather_context = []
        sensor_readings = []
        model_readings = []
        weather_readings = []

        if device_ids and device_ids.sensor_device_id:
            result = await db.execute(
                select(SensorReading).where(
                    and_(
                        SensorReading.sensor_device_id == device_ids.sensor_device_id,
                        SensorReading.timestamp >= start_of_day,
                        SensorReading.timestamp < end_of_day,
                    )
                ).order_by(SensorReading.timestamp)
            )
            sensor_readings = list(result.scalars().all())
            # Two preceding readings preserve the last pre-midnight trend.
            result = await db.execute(
                select(SensorReading).where(
                    SensorReading.sensor_device_id == device_ids.sensor_device_id,
                    SensorReading.timestamp < start_of_day,
                ).order_by(SensorReading.timestamp.desc()).limit(2)
            )
            sensor_context = list(reversed(result.scalars().all()))

        if device_ids and device_ids.camera_device_id:
            result = await db.execute(
                select(ModelReadings).where(
                    and_(
                        ModelReadings.camera_device_id == device_ids.camera_device_id,
                        ModelReadings.timestamp >= start_of_day,
                        ModelReadings.timestamp < end_of_day,
                    )
                ).order_by(ModelReadings.timestamp)
            )
            model_readings = list(result.scalars().all())
            # Reconstruct a complete camera confidence window across midnight.
            result = await db.execute(
                select(ModelReadings).where(
                    ModelReadings.camera_device_id == device_ids.camera_device_id,
                    ModelReadings.timestamp < start_of_day,
                ).order_by(ModelReadings.timestamp.desc()).limit(max(1, settings.OBSTRUCTION_WINDOW_K))
            )
            model_context = list(reversed(result.scalars().all()))

        result = await db.execute(
            select(Weather).where(
                and_(
                    Weather.location_id == location_id,
                    Weather.created_at >= start_of_day,
                    Weather.created_at < end_of_day,
                )
            ).order_by(Weather.created_at)
        )
        weather_readings = list(result.scalars().all())
        if not sensor_readings and not model_readings and not weather_readings:
            return {}  # Raw data may have expired; do not invent an empty summary.
        result = await db.execute(
            select(Weather).where(
                Weather.location_id == location_id,
                Weather.created_at < start_of_day,
            ).order_by(Weather.created_at.desc()).limit(1)
        )
        weather_context = list(result.scalars().all())
        smoothed_models = smooth_model_readings(model_context + model_readings)
        day_models = [reading for reading in smoothed_models if reading.timestamp >= start_of_day]

        summary_data = {}
        if sensor_readings:
            summary_data.update(extract_water_level_summary(sensor_readings))
        if day_models:
            summary_data.update(extract_model_readings_summary(day_models))
        if weather_readings:
            summary_data.update(extract_weather_summary(weather_readings))

        risk_summary = calculate_risk_scores(
            sensor_context + sensor_readings,
            smoothed_models,
            weather_context + weather_readings,
            critical_level,
            await cache_service.get_alert_thresholds(db),
            start_of_day,
        )
        summary_data.update(risk_summary)

        return summary_data

    async def generate_all_summaries(
        self, db: AsyncSession, target_date: date
    ) -> int:
        """Generate summaries for all locations. Returns count created."""
        location_ids = await cache_service.get_all_location_ids(db)
        created_count = 0

        for loc_id in location_ids:
            existing = await daily_summary_crud.get_by_location_and_date(
                db, loc_id, target_date
            )
            if existing:
                print(f"📋 Summary already exists for location {loc_id} on {target_date}, skipping.")
                continue

            summary_data = await self.generate_summary_for_location(db, loc_id, target_date)
            if not summary_data:
                continue
            try:
                await daily_summary_crud.create_daily_summary(db, loc_id, target_date, summary_data)
                created_count += 1
                print(f"✅ Created daily summary for location {loc_id} on {target_date}")
            except IntegrityError:
                await db.rollback()  # Summary was created by concurrent job, skip

        return created_count

    async def backfill_missing_summaries(self, db: AsyncSession, days: int | None = None) -> int:
        """Recover completed days within the current raw-data retention period."""
        if days is None:
            days = int(await system_settings_crud.get_value(db, "data_retention_days"))
        if days < 1:
            raise ValueError("Daily summary backfill requires a positive retention period")
        location_ids = await cache_service.get_all_location_ids(db)
        today = datetime.now(settings.APP_TIMEZONE).date()
        backfilled = 0

        for day_offset in range(1, days + 1):
            target_date = today - timedelta(days=day_offset)
            for loc_id in location_ids:
                try:
                    existing = await daily_summary_crud.get_by_location_and_date(db, loc_id, target_date)
                    if existing:
                        continue
                    summary_data = await self.generate_summary_for_location(db, loc_id, target_date)
                    if not summary_data:
                        continue
                    await daily_summary_crud.create_daily_summary(db, loc_id, target_date, summary_data)
                    backfilled += 1
                    print(f"📋 Backfilled summary for location {loc_id} on {target_date}")
                except IntegrityError:
                    await db.rollback()  # Summary was created by concurrent job, skip
                except Exception as e:
                    await db.rollback()
                    print(f"⚠️ Failed to backfill summary for location {loc_id} on {target_date}: {e}")

        return backfilled

    async def get_daily_summaries(
        self,
        db: AsyncSession,
        location_id: int,
        start_date: date,
        end_date: date,
    ) -> list[DailySummaryResponse]:
        db_summaries = await daily_summary_crud.get_daily_summaries(
            db=db,
            location_id=location_id,
            start_date=start_date,
            end_date=end_date,
        )
        return [DailySummaryResponse.model_validate(s) for s in db_summaries]

    async def get_available_summary_days(
        self, db: AsyncSession, location_id: int
    ) -> list[date]:
        return await daily_summary_crud.get_available_summary_days(
            db=db, location_id=location_id
        )


daily_summary_service = DailySummaryService()
