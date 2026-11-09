from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.system_settings import SystemSettings
from app.schemas import SystemSettingsCreate, SystemSettingsUpdate
from app.crud.base import CRUDBase
from typing import Any


class CRUDSystemSettings(CRUDBase[SystemSettings, SystemSettingsCreate, SystemSettingsUpdate]):
    
    async def get(self, db: AsyncSession, key: str) -> SystemSettings | None:

        result = await db.execute(
            select(self.model)
            .filter(self.model.key == key)
            .execution_options(populate_existing=False)
        )

        return result.scalars().first()


    async def get_value(self, db: AsyncSession, key: str) -> Any:
        
        setting = await self.get(db, key)
        
        return setting.json_value if setting else None


system_settings_crud = CRUDSystemSettings(SystemSettings)
