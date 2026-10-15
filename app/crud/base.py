from typing import Generic, TypeVar, Type, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from sqlalchemy import select

ModelType = TypeVar("ModelType")
CreateSchemaType = TypeVar("CreateSchemaType", bound=BaseModel)
UpdateSchemaType = TypeVar("UpdateSchemaType", bound=BaseModel)

class CRUDBase(Generic[ModelType, CreateSchemaType, UpdateSchemaType]):
    
    def __init__(self, model: Type[ModelType]):
        self.model = model


    async def get(self, db: AsyncSession, id: int) -> Optional[ModelType]:

        result = await db.execute(
            select(self.model).filter(self.model.id == id)
            .execution_options(populate_existing=False)
        )
        return result.scalars().first()
    

    async def get_all(self, db: AsyncSession) -> List[ModelType]:

        result = await db.execute(
            select(self.model)
            .execution_options(populate_existing=False)
        )
        return result.scalars().unique().all()
    

    async def get_with_lock(self, db: AsyncSession, id: str) -> ModelType:

        result = await db.execute(
            select(self.model)
            .filter(self.model.id == id)
            .with_for_update()
        )
        return result.scalars().first()


    async def create_and_return(self, db: AsyncSession, obj_in: CreateSchemaType) -> ModelType:

        obj_in_data = obj_in.model_dump()
        db_obj = self.model(**obj_in_data)
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj
    

    async def create_and_return_no_commit(self, db: AsyncSession, obj_in: CreateSchemaType) -> ModelType:

        """Flush-only variant: the service layer owns the commit so the write
        and any sibling writes (e.g. audit log) land in ONE transaction."""
        obj_in_data = obj_in.model_dump()
        db_obj = self.model(**obj_in_data)
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj


    async def create_only(self, db: AsyncSession, obj_in: CreateSchemaType) -> None:

        obj_in_data = obj_in.model_dump()
        db_obj = self.model(**obj_in_data)
        db.add(db_obj)
        await db.commit()


    async def update_no_commit(self, db: AsyncSession, db_obj: ModelType, obj_in: UpdateSchemaType) -> ModelType:

        """Flush-only variant of update: caller owns the commit boundary."""
        obj_data = obj_in.model_dump(exclude_unset=True)
        for field, value in obj_data.items():
            setattr(db_obj, field, value)
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj


    async def create_multi(self, db: AsyncSession, objs_in: List[CreateSchemaType]) -> List[ModelType]:

        db_objs = []
        for obj_in in objs_in:
            obj_in_data = obj_in.model_dump()
            db_obj = self.model(**obj_in_data)
            db_objs.append(db_obj)
        db.add_all(db_objs)
        await db.commit()
        return db_objs


    async def update(self, db: AsyncSession, db_obj: ModelType, obj_in: UpdateSchemaType) -> ModelType:

        obj_data = obj_in.model_dump(exclude_unset=True)
        for field, value in obj_data.items():
            setattr(db_obj, field, value)
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj
    

    async def delete(self, db: AsyncSession, id: int) -> Optional[ModelType]:
        
        result = await db.execute(
            select(self.model).filter(self.model.id == id)
        )
        obj = result.scalars().first()
        if obj:
            db.delete(obj)
            await db.commit()
        return obj