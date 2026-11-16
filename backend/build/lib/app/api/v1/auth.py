"""Authentication routes."""
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core import ratelimit
from app.core.config import get_settings
from app.db.session import get_session
from app.schemas.auth import LoginRequest, TokenResponse, UserPublic
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[Depends(ratelimit.rate_limit("login_ip", "login_rate_limit"))],
)
async def login(
    payload: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    # Per-account limit on top of the per-IP one: slows credential stuffing that
    # rotates source addresses against a single account.
    await ratelimit.check(
        "login_email", get_settings().login_email_rate_limit, payload.email.lower()
    )
    access, user = await auth_service.login(payload, response, session)
    return TokenResponse(access_token=access, user=UserPublic.model_validate(user))


@router.post(
    "/refresh",
    response_model=TokenResponse,
    dependencies=[Depends(ratelimit.rate_limit("refresh_ip", "refresh_rate_limit"))],
)
async def refresh(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    access, user = await auth_service.refresh(
        request.cookies.get(auth_service.REFRESH_COOKIE), response, session
    )
    return TokenResponse(access_token=access, user=UserPublic.model_validate(user))


@router.post("/logout")
async def logout(response: Response) -> dict[str, str]:
    await auth_service.logout(response)
    return {"detail": "Logged out"}


@router.get("/me", response_model=UserPublic)
async def me(user: object = Depends(get_current_user)) -> object:
    return user
