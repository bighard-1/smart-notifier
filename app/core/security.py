import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.services.settings_service import SettingsService

security = HTTPBasic()


async def verify_basic_auth(
    credentials: HTTPBasicCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> str:
    values = await SettingsService.get_all(db)
    username = values.get("web_username") or settings.web_username
    password = values.get("web_password") or settings.web_password

    username_ok = secrets.compare_digest(credentials.username, username)
    password_ok = secrets.compare_digest(credentials.password, password)

    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username
