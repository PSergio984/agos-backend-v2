import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyHeader
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.database import get_db
from app.crud import admin_user_crud

security = HTTPBearer()
iot_api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)

class CurrentUser:
    
    """Holds the authenticated user's data from the token"""
    def __init__(self, id: str, is_superuser: bool, is_enabled: bool, force_password_change: bool):
        self.id = id
        self.is_superuser = is_superuser
        self.is_enabled = is_enabled
        self.force_password_change = force_password_change


async def _authenticate_admin(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db)) -> CurrentUser:

    """Verify the admin's token and account status. Does NOT enforce a pending
    forced password change — wrap this with require_auth for that."""
    token = credentials.credentials
    
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id: str | None = payload.get("sub")
        
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    user_in_db = await admin_user_crud.get(db, id=user_id)
    if user_in_db is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )
    
    if not user_in_db.is_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive"
        )

    # Source account flags from the DB row (not the token claims) so a
    # demotion / password-change-required takes effect immediately rather
    # than lingering until the access token expires.
    return CurrentUser(
        id=user_id,
        is_superuser=user_in_db.is_superuser,
        is_enabled=user_in_db.is_enabled,
        force_password_change=user_in_db.force_password_change
    )


async def require_auth(
    current_user: CurrentUser = Depends(_authenticate_admin)) -> CurrentUser:

    """Standard admin auth. Blocks users who still have a pending forced
    password change so they cannot use the API by bypassing the client guard."""
    if current_user.force_password_change:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password change required",
        )
    return current_user


async def require_auth_allow_password_change(
    current_user: CurrentUser = Depends(_authenticate_admin)) -> CurrentUser:

    """Admin auth that permits a user with a pending forced password change.
    Use ONLY for the logout and change-password endpoints."""
    return current_user


optional_security = HTTPBearer(auto_error=False)


async def optional_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_security),
    db: AsyncSession = Depends(get_db)) -> CurrentUser | None:

    """Lazily resolve admin identity — only when a privileged flag is present.

    Without `include_inactive`, credentials are ignored entirely: a responder
    token on a plain picker call must not 401. With it, resolution runs the
    same gates as require_auth — unknown user 401, forced password change 403.
    """
    if request.query_params.get("include_inactive") not in ("true", "1"):
        return None
    if credentials is None:
        return None
    user = await _authenticate_admin(credentials=credentials, db=db)
    if user.force_password_change:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password change required",
        )
    return user


async def require_superuser(
    current_user: CurrentUser = Depends(require_auth)) -> CurrentUser:
    
    """Check if user is logged in AND is a superuser"""
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superuser access required"
        )
    return current_user


class CurrentResponder:
    """Holds the authenticated responder's data from the token"""
    def __init__(self, id: str):
        self.id = id


responder_security = HTTPBearer(auto_error=False)


async def require_responder_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(responder_security),
    db: AsyncSession = Depends(get_db)) -> CurrentResponder:

    """Check if responder has a valid token"""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        responder_id: str | None = payload.get("sub")
        token_type: str | None = payload.get("type")

        if responder_id is None or token_type != "responder":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    from app.crud import responder_crud
    responder = await responder_crud.get(db, id=responder_id)
    if responder is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Responder not found",
        )

    from app.models.responder_related.responders import ResponderStatus
    if responder.status != ResponderStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Responder account is not active",
        )

    return CurrentResponder(id=responder_id)


async def require_iot_api_key(x_api_key: str | None = Depends(iot_api_key_header)) -> None:
    """Validate static API key used by trusted IoT devices."""
    expected = settings.IOT_API_KEY

    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="IOT_API_KEY is not configured",
        )

    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )