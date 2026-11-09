from app.schemas import AdminUserCreate, AdminUserResponse, AdminAuditLogCreate
from app.crud import admin_user_crud
from app.crud import admin_audit_log_crud
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status
from app.models.admin_user import AdminUser
from app.api.v1.dependencies import CurrentUser
from app.utils import format_name_proper


class AdminUserService:
    
    async def get_all_admins(self, db: AsyncSession) -> list[AdminUserResponse]:

        admins: list[AdminUser] = await admin_user_crud.get_all_admins(db=db)
        result = [
            AdminUserResponse(
                id=item.id,
                phone_number=item.phone_number,
                first_name=item.first_name,
                last_name=item.last_name,
                is_superuser=item.is_superuser,
                is_enabled=item.is_enabled,
                last_login=item.last_login,
                created_by=f"{item.admin_creator.first_name} {item.admin_creator.last_name}" if item.admin_creator else None
            ) for item in admins
        ]
        return result


    async def create_new_admin_user(
        self, 
        db: AsyncSession, 
        admin_user_create: 
        AdminUserCreate, current_user: CurrentUser) -> AdminUserResponse:

        # Check if phone number already exists
        if await admin_user_crud.phone_exists(db=db, phone_number=admin_user_create.phone_number):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Phone number is already registered."
            )
        
        # Set created_by to current superuser's ID
        admin_user_create.created_by = current_user.id

        # format names to proper case
        admin_user_create.first_name = format_name_proper(admin_user_create.first_name)
        admin_user_create.last_name = format_name_proper(admin_user_create.last_name)

        user_in_db = await admin_user_crud.create(db=db, obj_in=admin_user_create)

        # Log the creation action
        await admin_audit_log_crud.create_only(db=db, obj_in=AdminAuditLogCreate(
            admin_user_id=current_user.id,
            action=f"Added {user_in_db.first_name} {user_in_db.last_name} as a new admin."
        ))
    
        return AdminUserResponse(
            id=user_in_db.id,
            phone_number=user_in_db.phone_number,
            first_name=user_in_db.first_name,
            last_name=user_in_db.last_name,
            is_superuser=user_in_db.is_superuser,
            is_enabled=user_in_db.is_enabled,
            last_login=user_in_db.last_login,
            created_by=f"{user_in_db.admin_creator.first_name} {user_in_db.admin_creator.last_name}" if user_in_db.admin_creator else None
        )


    async def deactivate_admin(
        self, db: AsyncSession, user_id: str, current_user: CurrentUser, reason: str
    ) -> None:
        if current_user.id == user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot deactivate your own account."
            )

        user = await admin_user_crud.deactivate(db=db, user_id=user_id, deactivated_by=current_user.id, reason=reason)

        # Invalidate all sessions
        from app.crud import refresh_token_crud
        await refresh_token_crud.delete_by_user_id(db=db, user_id=user_id)

        # Audit log
        await admin_audit_log_crud.create_only(db=db, obj_in=AdminAuditLogCreate(
            admin_user_id=current_user.id,
            action=f"Deactivated admin {user.first_name} {user.last_name}. Reason: {reason}"
        ))

    async def reactivate_admin(
        self, db: AsyncSession, user_id: str, current_user: CurrentUser
    ) -> None:
        user = await admin_user_crud.reactivate(db=db, user_id=user_id)

        await admin_audit_log_crud.create_only(db=db, obj_in=AdminAuditLogCreate(
            admin_user_id=current_user.id,
            action=f"Reactivated admin {user.first_name} {user.last_name}."
        ))


admin_user_service = AdminUserService()