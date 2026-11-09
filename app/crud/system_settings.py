from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.system_settings import SystemSettings
from app.schemas import SystemSettingsCreate, SystemSettingsUpdate
from app.crud.base import CRUDBase
from typing import Any


class CRUDSystemSettings(CRUDBase[SystemSettings, SystemSettingsCreate, SystemSettingsUpdate]):
    
    async def get(self, db: AsyncSession, key: str) -> SystemSettings:

        result = await db.execute(
            select(self.model)
            .filter(self.model.key == key)
            .execution_options(populate_existing=False)
        )

        row = result.scalars().first()

        if not row:
            raise HTTPException(status_code=404, detail="System settings not found")

        return row


    async def get_value(self, db: AsyncSession, key: str) -> Any:
        
        setting = await self.get(db, key)
        
        if not setting:
            raise HTTPException(status_code=404, detail="System settings not found")
        
        return setting.json_value


system_settings_crud = CRUDSystemSettings(SystemSettings)