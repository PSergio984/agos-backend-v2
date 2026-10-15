"""Responder group service: CRUD, validation, and audit."""

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser
from app.crud import responder_crud, responder_group_crud, admin_audit_log_crud
from app.models.responder_related.group import DEFAULT_ACTIVE_RESPONDERS_GROUP_NAME
from app.schemas import AdminAuditLogCreate, ResponderGroupCreate, ResponderGroupItem

from .validation import validate_group_name, validate_not_default_group


def _build_group_update_audit_action(
    previous_group_name: str,
    updated_group_name: str,
    name_changed: bool,
    added_count: int,
    removed_count: int,
) -> str:
    if name_changed and (added_count or removed_count):
        return (
            f"Renamed responder group '{previous_group_name}' to '{updated_group_name}' "
            f"and updated members (+{added_count}, -{removed_count})"
        )
    if name_changed:
        return f"Renamed responder group '{previous_group_name}' to '{updated_group_name}'"
    if added_count or removed_count:
        return (
            f"Updated members of responder group '{updated_group_name}' "
            f"(+{added_count}, -{removed_count})"
        )
    return f"Submitted update for responder group '{updated_group_name}' with no changes"


class ResponderGroupService:
    async def get_all_groups(self, db: AsyncSession) -> list[ResponderGroupItem]:
        await responder_group_crud.ensure_exists(
            db=db,
            name=DEFAULT_ACTIVE_RESPONDERS_GROUP_NAME,
        )
        groups = await responder_group_crud.get_all_with_member_ids(db=db)
        return [
            ResponderGroupItem(
                id=group.id,
                group_name=group.name,
                location_id=group.location_id,
                member_ids=[responder.id for responder in group.responders],
            )
            for group in groups
        ]

    async def create_group(
        self, db: AsyncSession, group: ResponderGroupCreate, current_user: CurrentUser
    ) -> ResponderGroupItem:
        normalized_name = validate_group_name(group.group_name)
        dedup_member_ids = list(dict.fromkeys(group.member_ids))

        try:
            # Names are unique per site — admin-created groups are NULL-site,
            # so the collision check runs within that same scope.
            if await responder_group_crud.get_by_name_and_location(
                db=db, name=normalized_name, location_id=None
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="A group with this name already exists.",
                )

            responders = []
            if dedup_member_ids:
                responders = await responder_crud.get_by_ids(db=db, ids=dedup_member_ids)
                if len(responders) != len(dedup_member_ids):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="One or more member IDs do not exist or are invalid.",
                    )

            created = await responder_group_crud.create_with_members(
                db=db,
                name=normalized_name,
                responders=responders,
            )

            await admin_audit_log_crud.create_only_no_commit(
                db=db,
                obj_in=AdminAuditLogCreate(
                    admin_user_id=current_user.id,
                    action=f"Created responder group '{normalized_name}'",
                ),
            )

            await db.commit()
        except HTTPException:
            await db.rollback()
            raise
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A group with this name already exists.",
            )
        except Exception:
            await db.rollback()
            raise

        return ResponderGroupItem(
            id=created.id,
            group_name=created.name,
            location_id=created.location_id,
            member_ids=dedup_member_ids,
        )

    async def update_group(
        self,
        db: AsyncSession,
        group_id: int,
        group: ResponderGroupCreate,
        current_user: CurrentUser,
    ) -> ResponderGroupItem:
        normalized_name = validate_group_name(group.group_name)
        dedup_member_ids = list(dict.fromkeys(group.member_ids))

        try:
            existing_group = await responder_group_crud.get_with_lock(db=db, id=group_id)
            if not existing_group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Responder group not found.",
                )

            existing_is_default = (
                existing_group.name.casefold() == DEFAULT_ACTIVE_RESPONDERS_GROUP_NAME.casefold()
            )
            incoming_is_default = (
                normalized_name.casefold() == DEFAULT_ACTIVE_RESPONDERS_GROUP_NAME.casefold()
            )
            if existing_is_default and not incoming_is_default:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="The system default active-responders group name cannot be changed.",
                )
            if not existing_is_default and incoming_is_default:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="This group name is reserved for the system default active-responders group.",
                )

            # Uniqueness is per site — the check runs in the group's own scope.
            existing_name_match = await responder_group_crud.get_by_name_and_location(
                db=db, name=normalized_name, location_id=existing_group.location_id
            )
            if existing_name_match and existing_name_match.id != group_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="A group with this name already exists.",
                )

            if dedup_member_ids:
                responders = await responder_crud.get_by_ids(db=db, ids=dedup_member_ids)
                if len(responders) != len(dedup_member_ids):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="One or more member IDs do not exist or are invalid.",
                    )

            previous_group_name = existing_group.name
            name_changed = previous_group_name != normalized_name
            updated_group, added_count, removed_count = (
                await responder_group_crud.update_with_members(
                    db=db,
                    group=existing_group,
                    name=normalized_name,
                    member_ids=dedup_member_ids,
                )
            )

            if name_changed or added_count or removed_count:
                audit_action = _build_group_update_audit_action(
                    previous_group_name=previous_group_name,
                    updated_group_name=updated_group.name,
                    name_changed=name_changed,
                    added_count=added_count,
                    removed_count=removed_count,
                )
                await admin_audit_log_crud.create_only_no_commit(
                    db=db,
                    obj_in=AdminAuditLogCreate(
                        admin_user_id=current_user.id,
                        action=audit_action,
                    ),
                )

            await db.commit()
        except HTTPException:
            await db.rollback()
            raise
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A group with this name already exists.",
            )
        except Exception:
            await db.rollback()
            raise

        return ResponderGroupItem(
            id=updated_group.id,
            group_name=updated_group.name,
            location_id=updated_group.location_id,
            member_ids=dedup_member_ids,
        )

    async def delete_group(
        self, db: AsyncSession, group_id: int, current_user: CurrentUser
    ) -> None:
        try:
            existing_group = await responder_group_crud.get_with_lock(db=db, id=group_id)
            if not existing_group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Responder group not found.",
                )

            validate_not_default_group(
                existing_group.name, action="deleted"
            )

            previous_group_name = existing_group.name
            await responder_group_crud.delete_no_commit(db=db, group=existing_group)

            await admin_audit_log_crud.create_only_no_commit(
                db=db,
                obj_in=AdminAuditLogCreate(
                    admin_user_id=current_user.id,
                    action=f"Deleted responder group '{previous_group_name}'",
                ),
            )

            await db.commit()
        except HTTPException:
            await db.rollback()
            raise
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Responder group could not be deleted.",
            )
        except Exception:
            await db.rollback()
            raise


responder_group_service = ResponderGroupService()
