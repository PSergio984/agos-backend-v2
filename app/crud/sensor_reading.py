import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import SensorReading
from app.schemas import SensorReadingCreate
from app.crud.base import CRUDBase
from app.models import SensorDevice
from sqlalchemy import delete, func, select, Row
from typing import List, Sequence


class CRUDSensorReading(CRUDBase[SensorReading, SensorReadingCreate, None]):

    # For getting the latest reading for a specific sensor
    async def get_latest_reading(self, db: AsyncSession, sensor_device_id: int) -> SensorReading | None:

        result = await db.execute(
            select(self.model)
            .filter(self.model.sensor_device_id == sensor_device_id)
            .order_by(self.model.timestamp.desc())
            .limit(1)
            .execution_options(populate_existing=False) 
        )
        return result.scalars().first()


    # For getting the previous reading for a specific sensor before a timestamp
    async def get_previous_reading(self, db: AsyncSession, before_timestamp: datetime, sensor_device_id: int) -> SensorReading | None:

        result = await db.execute(
            select(self.model)
            .filter(self.model.sensor_device_id == sensor_device_id)
            .filter(self.model.timestamp < before_timestamp)
            .order_by(self.model.timestamp.desc())
            .limit(1)
            .execution_options(populate_existing=False)
        )
        return result.scalars().first()


    # For "Sensor" page's table
    async def get_items_paginated(
        self, 
        db: AsyncSession, 
        sensor_device_id: int, 
        page: int = 1, 
        page_size: int = 10) -> Sequence[Row]:

        # Use LAG window function to get previous reading's water level
        prev_water_level = func.lag(self.model.water_level_cm).over(
            order_by=self.model.timestamp
        ).label("prev_water_level")

        skip = (page - 1) * page_size
        query = (
            select(
                self.model.id,
                self.model.timestamp, 
                self.model.water_level_cm,
                prev_water_level,
            )
            .filter(self.model.sensor_device_id == sensor_device_id)
            .order_by(self.model.timestamp.desc())
            .join(SensorDevice)
            .offset(skip)
            .limit(page_size + 1)
        )

        result = await db.execute(query)
        items = result.all()
        return items


    async def get_readings_since(self, db: AsyncSession, sensor_device_id: int, since_datetime: datetime) -> Sequence[Row]:

        result = await db.execute(
            select(self.model.water_level_cm, self.model.timestamp)
            .filter(self.model.sensor_device_id == sensor_device_id)
            .filter(self.model.timestamp >= since_datetime)
            .order_by(self.model.timestamp.asc())
        )
        return result.all()


    async def get_available_reading_days(self, db: AsyncSession, sensor_device_id: int) -> List[str]:

        result = await db.execute(
            select(func.distinct(func.date(self.model.timestamp)))
            .filter(self.model.sensor_device_id == sensor_device_id)
            .order_by(func.date(self.model.timestamp).desc())
        )
        dates = result.scalars().all()
        return [date.isoformat() for date in dates]


    async def get_readings_for_export(
        self, 
        db: AsyncSession, 
        start_datetime: datetime, 
        end_datetime: datetime, 
        sensor_device_id: int) -> Sequence[Row]:
        
        # Use LAG window function to get previous reading's water level
        prev_water_level = func.lag(self.model.water_level_cm).over(
            order_by=self.model.timestamp
        ).label("prev_water_level")
        
        result = await db.execute(
            select(
                self.model.timestamp,
                self.model.water_level_cm,
                prev_water_level,
                self.model.signal_strength,
            )
            .filter(self.model.sensor_device_id == sensor_device_id)
            .filter(self.model.timestamp >= start_datetime)
            .filter(self.model.timestamp <= end_datetime)
            .order_by(self.model.timestamp.desc())
        )
        return result.all()


    async def delete_older_than(self, db: AsyncSession, cutoff: datetime.datetime) -> int:
        result = await db.execute(
            delete(self.model).where(self.model.timestamp < cutoff)
        )
        await db.commit()
        return result.rowcount

    # For sensor's periodic reading insertion
    async def create_record(self, db: AsyncSession, db_obj: SensorReading) -> SensorReading:

        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj


    async def get_recent_trend(
        self, db: AsyncSession, sensor_device_id: int, hours: int = 24, max_points: int = 50
    ) -> list[dict]:
        from datetime import timezone, timedelta
        cutoff = datetime.datetime.now(timezone.utc) - timedelta(hours=hours)

        result = await db.execute(
            select(self.model.timestamp, self.model.water_level_cm)
            .filter(
                self.model.sensor_device_id == sensor_device_id,
                self.model.timestamp >= cutoff,
            )
            .order_by(self.model.timestamp.asc())
        )
        rows = result.all()

        # Downsample if too many points
        if len(rows) > max_points:
            step = len(rows) / max_points
            rows = [rows[int(i * step)] for i in range(max_points)]

        return [{"timestamp": r.timestamp, "water_level_cm": float(r.water_level_cm)} for r in rows]


sensor_reading_crud = CRUDSensorReading(SensorReading)