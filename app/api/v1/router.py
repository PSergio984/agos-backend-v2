from app.api.v1.endpoints import (
    auth,
    admin_users,
    admin_audit_log,
    notification_templates,
    system_settings,
)
from app.api.v1.endpoints import sensor_reading, sensor_device, weather
from app.api.v1.endpoints import responder, responder_group, push, responder_app
from app.api.v1.endpoints import stream, core
from app.api.v1.endpoints import daily_summary, analysis
from app.api.v1.endpoints import notification_logs
from app.api.v1.endpoints import model_reading_logs
from app.api.v1.endpoints import health
from app.api.v1.endpoints import iot
from app.api.v1.endpoints import evacuation_center, evacuation
from app.api.v1.endpoints import locations
from app.api.v1.endpoints import public
from fastapi import APIRouter

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router)
api_router.include_router(admin_audit_log.router)
api_router.include_router(admin_users.router)
api_router.include_router(analysis.router)
api_router.include_router(core.router)
api_router.include_router(daily_summary.router)
api_router.include_router(notification_logs.router)
api_router.include_router(notification_templates.router)
api_router.include_router(push.router)
api_router.include_router(responder.router)
api_router.include_router(responder_app.router)
api_router.include_router(responder_group.router)
api_router.include_router(sensor_device.router)
api_router.include_router(sensor_reading.router)
api_router.include_router(stream.router)
api_router.include_router(system_settings.router)
api_router.include_router(model_reading_logs.router)
api_router.include_router(weather.router)
api_router.include_router(health.router)
api_router.include_router(iot.router)
api_router.include_router(evacuation_center.router)
api_router.include_router(evacuation.router)
api_router.include_router(locations.router)
api_router.include_router(public.router)
