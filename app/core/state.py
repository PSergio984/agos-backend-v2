import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.fusion_scoring import calculate_fusion_data
from app.schemas import (
    FusionData,
    BlockageStatus,
    WaterLevelStatus,
    WeatherStatus,
    FusionAnalysisData,
)
from app.schemas.reading_summary_response import FusionWebSocketResponse
from app.services.cache_service import cache_service
from app.schemas import DevicePerLocation


WARNING_NOTIFY_COOLDOWN_SECONDS = 30 * 60   # 30 minutes
CRITICAL_NOTIFY_COOLDOWN_SECONDS = 30 * 60  # 30 minutes
BLOCKAGE_NOTIFY_COOLDOWN_SECONDS = 4 * 60 * 60  # 4 hours — blockages persist


class FusionAnalysisState:

    def __init__(self, location_id: int = None, camera_device_id: int = None, sensor_device_id: int = None):
        self.fusion_analysis: FusionAnalysisData | None = None
        self.location_id = location_id
        self.camera_device_id = camera_device_id
        self.sensor_device_id = sensor_device_id

        self.fusion_data: FusionData = FusionData(
            alert_name="Normal",
            combined_risk_score=0,
            triggered_conditions=[]
        )
        self.blockage_status: BlockageStatus | None = None
        self.water_level_status: WaterLevelStatus | None = None
        self.weather_status: WeatherStatus | None = None
        self._last_warning_notify_time: float = 0
        self._last_critical_notify_time: float = 0
        self._last_blockage_notify_time: float = 0
        # F4 — admin-facing evacuation recommendation (cooldown + auto-clear on relax)
        self._last_recommendation_time: float = 0
        self._recommendation_active: bool = False
        self._lock = asyncio.Lock()


    async def broadcast_fusion_analysis(self):
        from app.services import websocket_service

        fusion_websocket_response = FusionWebSocketResponse(
            status="success",
            message="Retrieved successfully",
            fusion_analysis=self.fusion_analysis
        )

        await websocket_service.broadcast_update(
            "fusion_analysis_update", 
            fusion_websocket_response.model_dump(mode='json'),
            location_id=self.location_id
        )


    async def load_initial_state(self, db: AsyncSession):
        """
        Loads the latest data from the database upon server startup.
        Verifies if the data is recent enough to be considered valid for fusion analysis.
        """
        from app.crud import sensor_reading_crud
        from app.crud import model_readings_crud
        from app.crud import weather_crud
        from app.services import sensor_reading_service
        from app.services import weather_service
        from app.core.config import settings
        from datetime import datetime, timezone, timedelta

        # --- 1. Load Sensor Reading (Water Level) ---
        latest_sensor = await sensor_reading_crud.get_latest_reading(db=db, sensor_device_id=self.sensor_device_id)
        if latest_sensor:
            # Check if data is within the acceptable warning period (not too stale)
            is_valid = latest_sensor.timestamp >= datetime.now(timezone.utc) - timedelta(minutes=settings.SENSOR_WARNING_PERIOD_MINUTES)
            
            if is_valid:
                # Calculate summary to get trend and critical percentage
                summary = await sensor_reading_service.calculate_record_summary(db=db, reading=latest_sensor)
                
                self.water_level_status = WaterLevelStatus(
                    timestamp=latest_sensor.timestamp,
                    water_level_cm=summary.water_level.current_cm,
                    change_rate=summary.water_level.change_rate,
                    critical_percentage=summary.alert.percentage_of_critical,
                    trend=summary.water_level.trend
                )

        # --- 2. Load Model Reading (Blockage) ---
        latest_model = await model_readings_crud.get_latest_reading(db=db, camera_device_id=self.camera_device_id)
        if latest_model:
            # Check if data is within the acceptable warning period
            model_timestamp = latest_model["timestamp"]
            is_valid = model_timestamp >= datetime.now(timezone.utc) - timedelta(minutes=settings.SENSOR_WARNING_PERIOD_MINUTES)
            
            if is_valid:
                self.blockage_status = BlockageStatus(
                    timestamp=model_timestamp,
                    status=latest_model["blockage_status"]
                )

        # --- 3. Load Weather Condition ---
        latest_weather = await weather_crud.get_latest_weather(db, location_id=self.location_id)
        if latest_weather:
            # Check if data is within the acceptable warning period
            weather_timestamp = latest_weather["created_at"]
            is_valid = weather_timestamp >= datetime.now(timezone.utc) - timedelta(minutes=settings.WEATHER_CONDITION_WARNING_PERIOD_MINUTES)
            
            if is_valid:
                # Get weather description
                weather_summary = weather_service.get_weather_summary(
                    created_at=weather_timestamp,
                    weather_code=latest_weather["weather_code"],
                    precipitation_mm=latest_weather["precipitation_mm"]
                )
                
                self.weather_status = WeatherStatus(
                    timestamp=weather_timestamp,
                    precipitation_mm=latest_weather["precipitation_mm"],
                    weather_condition=weather_summary.condition
                )
        
        # --- 4. Recalculate Fusion Data with loaded values ---
        await self._recalculate_fusion_data()
        await self._broadcast_and_notify()


    async def calculate_visual_status_score(self, blockage_status: BlockageStatus = None):
        async with self._lock:
            if blockage_status:
                self.blockage_status = blockage_status
            await self._recalculate_fusion_data()
        # Broadcast and notify OUTSIDE the lock — push delivery is slow network
        # I/O and must not block other sensor/camera/weather updates for this
        # location while it runs.
        await self._broadcast_and_notify()


    async def calculate_water_level_score(self, water_level_status: WaterLevelStatus = None):
        async with self._lock:
            if water_level_status:
                self.water_level_status = water_level_status
            await self._recalculate_fusion_data()
        await self._broadcast_and_notify()


    async def calculate_weather_score(self, weather_status: WeatherStatus = None):
        async with self._lock:
            if weather_status:
                self.weather_status = weather_status
            await self._recalculate_fusion_data()
        await self._broadcast_and_notify()


    async def _recalculate_fusion_data(self) -> None:
        """Compute fusion state from the current inputs. Runs under self._lock."""
        async with AsyncSessionLocal() as db:
            alert_thresholds = await cache_service.get_alert_thresholds(db)

        self.fusion_data = calculate_fusion_data(
            blockage_status=self.blockage_status,
            water_level_status=self.water_level_status,
            weather_status=self.weather_status,
            alert_thresholds=alert_thresholds,
        )

        self.fusion_analysis = FusionAnalysisData(
            fusion_data=self.fusion_data,
            blockage_status=self.blockage_status,
            water_level_status=self.water_level_status,
            weather_status=self.weather_status,
        )

    async def _broadcast_and_notify(self) -> None:
        """Broadcast the latest fusion state and dispatch auto-notifications.
        Intentionally NOT holding self._lock — this does slow network I/O."""
        await self.broadcast_fusion_analysis()

        # Auto-notify responders on fusion alert level (cooldown-gated)
        if self.fusion_data.alert_name == "Critical":
            await self._auto_notify_critical()
        elif self.fusion_data.alert_name == "Warning":
            await self._auto_notify_warning()

        # F1 — auto-notify only on sustained obstruction evidence (tier likely/confirmed),
        # and suppress entirely when the BLIND_CAMERA anomaly casts doubt on the camera.
        self._apply_blind_camera_downgrade()
        if self._should_notify_obstruction():
            await self._auto_notify_blockage()

        # F4 — emit an admin-facing evacuation RECOMMENDATION (never a public blast).
        await self._maybe_emit_evacuation_recommendation()

    async def _maybe_emit_evacuation_recommendation(self) -> None:
        """When fusion risk crosses the evacuation threshold, notify admins with a
        recommendation. This is advisory only — the public alert requires an admin
        to POST /evacuation/confirm. Cooldown-gated; auto-clears when risk relaxes."""
        import time
        from app.core.config import settings

        score = self.fusion_data.combined_risk_score
        if score < settings.EVACUATION_RECOMMEND_MIN_SCORE:
            self._recommendation_active = False
            return

        now = time.monotonic()
        if (
            self._recommendation_active
            and now - self._last_recommendation_time < settings.EVACUATION_RECOMMEND_COOLDOWN_SECONDS
        ):
            return

        try:
            from app.services import websocket_service
            from app.services.evacuation_service import build_recommendation

            recommendation = build_recommendation(self.location_id, self.fusion_analysis)
            await websocket_service.broadcast_update(
                update_type="evacuation_recommendation",
                data=recommendation.model_dump(mode="json"),
                location_id=self.location_id,
            )
            self._last_recommendation_time = now
            self._recommendation_active = True
            print(f"🟠 [EVAC RECOMMENDATION] location {self.location_id} score={score}")
        except Exception as e:
            print(f"⚠️ Evacuation recommendation emit failed: {e}")

    def _apply_blind_camera_downgrade(self) -> None:
        """When fusion flags BLIND_CAMERA, cap the obstruction confidence so a
        suspect camera can never *raise* confidence or trigger an alert."""
        from app.schemas import AnomalyType

        confidence = getattr(self.blockage_status, "confidence", None)
        if not confidence:
            return
        if AnomalyType.BLIND_CAMERA in self.fusion_data.anomalies and confidence.tier in ("likely", "confirmed"):
            confidence.tier = "possible"

    def _should_notify_obstruction(self) -> bool:
        confidence = getattr(self.blockage_status, "confidence", None)
        if confidence is not None:
            return confidence.tier in ("likely", "confirmed")
        # Fallback for states hydrated without a confidence object (e.g. initial load).
        return bool(self.blockage_status and self.blockage_status.status == "blocked")

    async def _send_system_template(self, notification_type, log_label: str) -> bool:
        """Look up the system template for this notification type and dispatch it
        to this site's responders with push subscriptions. Returns True on
        successful send, False if no template is configured or no subscribers
        exist at this site."""
        from app.services import notification_service
        from app.crud import responder_crud
        from app.crud import notification_template_crud
        from app.schemas.subscription import SendNotificationSchema

        async with AsyncSessionLocal() as db:
            template = await notification_template_crud.get_by_type(
                db=db, notification_type=notification_type
            )
            if not template:
                print(f"⚠️ No system template configured for {notification_type.value} — skipping auto-notify")
                return False

            responder_ids = await responder_crud.get_responder_ids_with_push_subscription(
                db=db, location_id=self.location_id
            )
            if not responder_ids:
                return False

            payload = SendNotificationSchema(
                responder_ids=responder_ids,
                template_id=template.id,
                custom_notification=None,
            )
            await notification_service.send_notification_to_subscribers(
                payload=payload,
                db=db,
                system_initiated=True,
                location_id=self.location_id,
            )
            print(f"{log_label} Auto-notified {len(responder_ids)} responders (location {self.location_id})")
            return True


    async def _auto_notify_critical(self) -> None:
        import time
        from app.models.notification_template import NotificationType
        now = time.monotonic()
        if now - self._last_critical_notify_time < CRITICAL_NOTIFY_COOLDOWN_SECONDS:
            return
        try:
            # Arm cooldown only on successful send so a missing template doesn't
            # silence Critical alerts for the cooldown window.
            if await self._send_system_template(NotificationType.CRITICAL, "🚨 [CRITICAL]"):
                self._last_critical_notify_time = now
        except Exception as e:
            print(f"⚠️ Auto-notify critical failed: {e}")


    async def _auto_notify_warning(self) -> None:
        import time
        from app.models.notification_template import NotificationType
        now = time.monotonic()
        if now - self._last_warning_notify_time < WARNING_NOTIFY_COOLDOWN_SECONDS:
            return
        try:
            if await self._send_system_template(NotificationType.WARNING, "⚠️ [WARNING]"):
                self._last_warning_notify_time = now
        except Exception as e:
            print(f"⚠️ Auto-notify warning failed: {e}")


    async def _auto_notify_blockage(self) -> None:
        import time
        from app.models.notification_template import NotificationType
        now = time.monotonic()
        if now - self._last_blockage_notify_time < BLOCKAGE_NOTIFY_COOLDOWN_SECONDS:
            return
        try:
            if await self._send_system_template(NotificationType.BLOCKAGE, "🪵 [SURFACE OBSTRUCTION]"):
                self._last_blockage_notify_time = now
        except Exception as e:
            print(f"⚠️ Auto-notify surface obstruction failed: {e}")


class StateManager:

    def __init__(self):
        self._fusion_analysis_states: dict[int, FusionAnalysisState] = {}
        # int is location id


    # Retrieve existing Fusion Analysis Data for a location
    def get_fusion_analysis_state(self, location_id: int) -> FusionAnalysisState:
        if location_id not in self._fusion_analysis_states:
            raise ValueError(f"No FusionAnalysisState found for location_id {location_id}")
        return self._fusion_analysis_states[location_id].fusion_analysis

    def get_alert_name(self, location_id: int) -> str | None:
        """Current fusion alert level for a location (F1 adaptive sampling)."""
        state = self._fusion_analysis_states.get(location_id)
        return state.fusion_data.alert_name if state else None


    async def recalculate_water_level_score(self, water_level_status: WaterLevelStatus, location_id: int) -> None:
        fusion_state = self._fusion_analysis_states.get(location_id)
        if fusion_state:
            await fusion_state.calculate_water_level_score(water_level_status=water_level_status)


    async def recalculate_visual_status_score(self, blockage_status: BlockageStatus, location_id: int) -> None:
        fusion_state = self._fusion_analysis_states.get(location_id)
        if fusion_state:
            await fusion_state.calculate_visual_status_score(blockage_status=blockage_status)


    async def recalculate_weather_score(self, weather_status: WeatherStatus, location_id: int) -> None:
        fusion_state = self._fusion_analysis_states.get(location_id)
        if fusion_state:
            await fusion_state.calculate_weather_score(weather_status=weather_status)


    def start_fusion_analysis_state(self, location_id: int, sensor_device_id: int, camera_device_id: int) -> FusionAnalysisState:
        if location_id not in self._fusion_analysis_states:
            fusion_state = FusionAnalysisState(location_id=location_id, camera_device_id=camera_device_id, sensor_device_id=sensor_device_id)
            self._fusion_analysis_states[location_id] = fusion_state
            return fusion_state


    async def start_all_states(self) -> None:
        async with AsyncSessionLocal() as db:
            location_ids = await cache_service.get_all_location_ids(db=db)

            if not location_ids:
                print("⚠️ No location IDs found to start fusion analysis states.")
                return
            
            for loc_id in location_ids:
                    
                device_ids: DevicePerLocation = await cache_service.get_device_ids_per_location(db=db, location_id=loc_id)

                if not device_ids:
                    print(f"⚠️ No device IDs found for location ID {loc_id}. Skipping fusion state initialization.")
                    continue

                fusion_state = self.start_fusion_analysis_state(location_id=loc_id, 
                                                                sensor_device_id=device_ids.sensor_device_id, 
                                                                camera_device_id=device_ids.camera_device_id)
                await fusion_state.load_initial_state(db=db)


fusion_state_manager = StateManager()
