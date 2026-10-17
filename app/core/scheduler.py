from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime, timedelta
import logging
from app.core.database import AsyncSessionLocal
from app.core.config import settings


scheduler = AsyncIOScheduler(timezone=settings.APP_TIMEZONE)
logger = logging.getLogger(__name__)


async def midnight_summary_job():
    """Generate daily summaries for the day that just ended.

    The cron fires at 00:00 local time, so the completed day is yesterday.
    """
    from app.services import daily_summary_service

    target_date = datetime.now(settings.APP_TIMEZONE).date() - timedelta(days=1)
    print(f"📅 Running daily summary job for {target_date}...")
    
    try:
        async with AsyncSessionLocal() as db:
            count = await daily_summary_service.generate_all_summaries(db, target_date)
            print(f"✅ Daily summaries generated: {count} summaries for {target_date}")
    except Exception as e:
        print(f"❌ Error generating daily summaries: {e}")


async def daily_summary_backfill_job():
    """Recover missed summaries at 00:30, before the 01:00 raw-data cleanup."""
    from app.services import daily_summary_service

    try:
        async with AsyncSessionLocal() as db:
            count = await daily_summary_service.backfill_missing_summaries(db)
            logger.info("Daily summary backfill completed: %d summaries created", count)
    except Exception:
        logger.exception("Daily summary backfill failed")


async def data_cleanup_job():
    """Delete sensor readings, model readings, and weather data older than the configured retention period."""
    from app.crud.system_settings import system_settings_crud
    from app.crud.sensor_reading import sensor_reading_crud
    from app.crud.model_readings import model_readings_crud
    from app.crud.weather import weather_crud
    from app.crud.responder_otp_verification import responder_otp_verification_crud
    from app.crud.password_reset_otp import password_reset_otp_crud
    from app.crud.evacuation_event import evacuation_event_crud
    from app.services.reading_report.service import reading_report_service

    print("🗑️ Running data cleanup job...")

    try:
        async with AsyncSessionLocal() as db:
            now = datetime.now(settings.APP_TIMEZONE)

            # Data retention cleanup
            retention_days = await system_settings_crud.get_value(db, "data_retention_days")
            cutoff = now - timedelta(days=int(retention_days))

            sensor_count = await sensor_reading_crud.delete_older_than(db, cutoff)
            model_count = await model_readings_crud.delete_older_than(db, cutoff)
            weather_count = await weather_crud.delete_older_than(db, cutoff)

            # Evacuation-event (public alert audit) retention: delete rows older
            # than the cutoff OR beyond the newest N per location. Falls back to
            # 60 days / 50 rows if the settings haven't been seeded.
            try:
                alert_retention_days = int(
                    await system_settings_crud.get_value(db, "alert_retention_days")
                )
            except Exception:
                alert_retention_days = 60
            try:
                alert_retention_max = int(
                    await system_settings_crud.get_value(db, "alert_retention_max")
                )
            except Exception:
                alert_retention_max = 50
            alert_cutoff = now - timedelta(days=alert_retention_days)
            evac_event_count = await evacuation_event_crud.prune(
                db,
                older_than=alert_cutoff,
                keep_per_location=alert_retention_max,
            )

            report_count = await reading_report_service.cleanup(db)
            logger.info("Expired reading reports removed: %d", report_count)

            # Expired OTP cleanup
            responder_otp_count = await responder_otp_verification_crud.delete_expired(db, now)
            password_otp_count = await password_reset_otp_crud.delete_expired(db, now)

            print(
                f"✅ Data cleanup complete (retention={retention_days}d, "
                f"alerts={alert_retention_days}d/{alert_retention_max}max): "
                f"sensor_readings={sensor_count}, model_readings={model_count}, weather={weather_count}, "
                f"evacuation_events={evac_event_count}, "
                f"expired_otps={responder_otp_count + password_otp_count}"
            )
    except Exception as e:
        print(f"❌ Error during data cleanup: {e}")


async def _escalation_check_job():
    """Check for unacknowledged critical alerts and re-notify."""
    from app.core.escalation import escalation_check_job
    await escalation_check_job()


def start_scheduler():
    """Start the APScheduler with configured jobs."""
    scheduler.add_job(
        midnight_summary_job,
        CronTrigger(hour=0, minute=0, timezone=settings.APP_TIMEZONE),  # Run at midnight local time
        id="daily_summary_job",
        replace_existing=True,
        misfire_grace_time=3600  # Allow job to run up to 1 hour late if missed
    )
    scheduler.add_job(
        daily_summary_backfill_job,
        CronTrigger(hour=0, minute=30, timezone=settings.APP_TIMEZONE),
        id="daily_summary_backfill_job",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=1800,
    )
    scheduler.add_job(
        data_cleanup_job,
        CronTrigger(hour=1, minute=0, timezone=settings.APP_TIMEZONE),  # Run at 1:00 AM local time
        id="data_cleanup_job",
        replace_existing=True,
        misfire_grace_time=3600
    )
    scheduler.add_job(
        _escalation_check_job,
        IntervalTrigger(minutes=5),
        id="escalation_check_job",
        replace_existing=True,
        # Within one process a slow run must not overlap the next tick —
        # cross-process dedup is the delivery row locks (#26).
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.start()
    print(f"📅 Scheduler started - Daily summary at midnight, summary backfill at 12:30 AM, data cleanup at 1:00 AM, escalation every 5 min (UTC{settings.UTC_OFFSET_HOURS:+g})")


def shutdown_scheduler():
    """Gracefully shutdown the scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        print("📅 Scheduler shutdown complete")
