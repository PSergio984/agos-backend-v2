from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.schemas import AlertThresholdsResponse, LocationCoordinate, DevicePerLocation
from app.models import SensorConfig
from app.crud import system_settings_crud
from app.crud import location_crud
from app.crud import sensor_device_crud
from app.crud import camera_device_crud

class CacheService:

    def __init__(self):
        self._device_ids_cache: Optional[dict[int, DevicePerLocation]] = None  # cached device IDs per location

        self._location_id_per_sensor_device_cache: Optional[dict[int, int]] = None # cached location id per sensor device id

        self._location_coordinates_cache: Optional[list[LocationCoordinate]] = None  # cached location coordinates

        self._sensor_config_cache: dict[int, SensorConfig] = {}  # cached sensor configuration per sensor device
        
        self._alert_thresholds_cache: Optional[AlertThresholdsResponse] = None  # cached alert thresholds


    # mutable cache
    async def update_alert_thresholds_cache(self, db: AsyncSession) -> None:
        alert_thresholds_data = await system_settings_crud.get_value(db=db, key="alert_thresholds")
        self._alert_thresholds_cache = AlertThresholdsResponse.model_validate(alert_thresholds_data)


    # mutable cache
    async def update_sensor_config_cache(self, db: AsyncSession, sensor_device_id: int) -> None:
        sensor_config_data = await sensor_device_crud.get_device_config(db=db, sensor_device_id=sensor_device_id)
        self._sensor_config_cache[sensor_device_id] = SensorConfig.model_validate(sensor_config_data)


    # unmutable cache
    async def update_location_coordinates_cache(self, db: AsyncSession) -> None:
        rows = await location_crud.get_all_coordinates(db=db)
        self._location_coordinates_cache = [
            LocationCoordinate(id=row.id, latitude=row.latitude, longitude=row.longitude)
            for row in rows
        ]


    # unmutable cache
    async def update_device_ids_cache(self, db: AsyncSession) -> None:
        location_ids = await location_crud.get_all_ids(db=db)
        device_ids_cache = {}
        for loc_id in location_ids:
            camera_device_id = await camera_device_crud.get_id_by_location(db=db, location_id=loc_id)
            sensor_device_id = await sensor_device_crud.get_id_by_location(db=db, location_id=loc_id)
            device_ids_cache[loc_id] = DevicePerLocation(
                camera_device_id=camera_device_id,
                sensor_device_id=sensor_device_id
            )
        self._device_ids_cache = device_ids_cache


    # unmutable cache
    """
        1. Gets all location IDs
        2. For each location ID, gets the corresponding sensor device ID
        3. Builds a dictionary mapping sensor device IDs to location IDs
        4. Stores the dictionary in the cache attribute
    """
    async def update_location_id_per_sensor_device_cache(self, db: AsyncSession) -> None:
        location_ids = await location_crud.get_all_ids(db=db)

        location_id_per_sensor_device_cache = {}
        for loc_id in location_ids:
            sensor_device_id = await sensor_device_crud.get_id_by_location(db=db, location_id=loc_id)
            location_id_per_sensor_device_cache[sensor_device_id] = loc_id
        self._location_id_per_sensor_device_cache = location_id_per_sensor_device_cache


    async def get_sensor_config(self, db: AsyncSession, sensor_device_id: int | None = None) -> SensorConfig:

        if sensor_device_id is None:
            # Fallback for callers without a device context — resolve the first
            # known sensor device (legacy single-site behavior).
            if self._device_ids_cache is None:
                await self.update_device_ids_cache(db=db)

            device_ids = self._device_ids_cache.get(1)
            if device_ids is None:
                first_location_id = next(iter(self._device_ids_cache), None)
                if first_location_id is None:
                    raise ValueError("No device IDs found in cache to load sensor config.")
                device_ids = self._device_ids_cache.get(first_location_id)

            if device_ids is None or device_ids.sensor_device_id is None:
                raise ValueError("No sensor device ID available to load sensor config.")

            sensor_device_id = device_ids.sensor_device_id

        if sensor_device_id not in self._sensor_config_cache:
            await self.update_sensor_config_cache(
                db=db, sensor_device_id=sensor_device_id
            )

        return self._sensor_config_cache[sensor_device_id]


    async def get_all_location_coordinates(self, db: AsyncSession) -> list[LocationCoordinate]:

        if self._location_coordinates_cache is None:
            await self.update_location_coordinates_cache(db=db)

        return self._location_coordinates_cache


    async def get_device_ids_per_location(self, db: AsyncSession, location_id: int) -> DevicePerLocation:

        if self._device_ids_cache is None:
            await self.update_device_ids_cache(db=db)
        
        device_ids = self._device_ids_cache.get(location_id)
        if device_ids is None:
            raise ValueError(f"Device IDs for location id {location_id} not found in cached device IDs.")

        return device_ids


    async def get_location_id_per_sensor_device(self, db: AsyncSession, sensor_device_id: int) -> int:

        if self._location_id_per_sensor_device_cache is None:
            await self.update_location_id_per_sensor_device_cache(db=db)
        
        location_id = self._location_id_per_sensor_device_cache.get(sensor_device_id)
        if location_id is None:
            raise ValueError(f"Location ID for sensor device id {sensor_device_id} not found in cached location IDs.")

        return location_id


    async def get_location_coordinate(self, db: AsyncSession, location_id) -> LocationCoordinate:

        if self._location_coordinates_cache is None:
            await self.update_location_coordinates_cache(db=db)
        
        location_coords = next((loc for loc in self._location_coordinates_cache if loc.id == location_id), None)
        if location_coords is None:
            raise ValueError(f"Location with id {location_id} not found in cached coordinates.")

        return location_coords


    async def get_all_location_ids(self, db: AsyncSession) -> list[int]:

        if self._location_coordinates_cache is None:
            await self.update_location_coordinates_cache(db=db)

        return [loc.id for loc in self._location_coordinates_cache]


    async def get_alert_thresholds(self, db: AsyncSession) -> AlertThresholdsResponse:
        
        if self._alert_thresholds_cache is None:
            await self.update_alert_thresholds_cache(db=db)

        return self._alert_thresholds_cache


cache_service = CacheService()