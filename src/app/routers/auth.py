from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.core.db import SessionDep
from app.core.deps import CurrentUser
from app.core.security import check_password, csrf_token, hash_password
from app.core.templating import fragment, templates
from app.models import User
from app.routers.alerts import forget
from app.services.admin import admin_login, is_admin_email, is_system_admin
from app.services.login import end_other_sessions, guard, safe_next, start_session

router = APIRouter(tags=["auth"], include_in_schema=False)

MIN_PASSWORD_LENGTH = 8


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "") -> HTMLResponse:
    """next는 로그인한 뒤 돌아갈 주소다. 알림을 눌러 들어왔다가 로그인하면 그 글로 간다."""
    target = safe_next(next)
    return templates.TemplateResponse(
        request, "login.html", {"next": "" if target == "/" else target}
    )


@router.post("/login")
async def login(
    request: Request,
    session: SessionDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form(max_length=2000)] = "",
) -> Response:
    email = email.strip().lower()[:255]
    address = request.client.host if request.client else ""
    wait = guard.wait_seconds(email, address)
    if wait:
        minutes = -(-wait // 60)
        return fragment(
            "partials/message.html",
            message=f"로그인 시도가 너무 많습니다. {minutes}분 뒤에 다시 시도해 주세요.",
        )
    if is_admin_email(email):
        user = await admin_login(session, password)
    else:
        user = await session.scalar(select(User).where(User.email == email))
        if not await check_password(user.password_hash if user else None, password):
            user = None
    if user is None or not user.is_active:
        guard.failed(email, address)
        return fragment("partials/message.html", message="이메일 또는 비밀번호가 맞지 않습니다.")
    guard.succeeded(email)
    start_session(request.session, user)
    csrf_token(request)
    return Response(headers={"HX-Redirect": safe_next(next)})


@router.post("/logout")
async def logout(
    request: Request,
    session: SessionDep,
    push_endpoint: Annotated[str, Form(max_length=2000)] = "",
) -> Response:
    """이 기기의 알림 구독도 지운다. 지울 수 있는 것은 로그인한 본인의 구독뿐이다."""
    user_id = request.session.get("user_id")
    if user_id and push_endpoint:
        await forget(session, user_id, push_endpoint)
        await session.commit()
    request.session.clear()
    return Response(headers={"HX-Redirect": "/login"})


@router.get("/account/password", response_class=HTMLResponse)
async def password_page(request: Request, user: CurrentUser) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "password.html", {"user": user, "active_tab": "more"}
    )


@router.post("/account/password")
async def change_password(
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
) -> Response:
    if is_system_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "관리자 비밀번호는 환경 변수로 관리합니다.")
    if not await check_password(user.password_hash, current_password):
        return fragment("partials/message.html", message="현재 비밀번호가 맞지 않습니다.")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        return fragment(
            "partials/message.html", message=f"새 비밀번호는 {MIN_PASSWORD_LENGTH}자 이상입니다."
        )
    user.password_hash = hash_password(new_password)
    # 다른 기기는 로그아웃된다. 이 기기는 새 버전으로 맞춰 그대로 쓴다.
    await end_other_sessions(session, user)
    await session.commit()
    # 열려 있는 화면이 쓰는 보안 토큰은 그대로 둔다.
    token = csrf_token(request)
    start_session(request.session, user)
    request.session["csrf"] = token
    return fragment(
        "partials/message.html",
        message="비밀번호를 바꿨습니다. 다른 기기에서는 다시 로그인해야 합니다.",
    )


@router.post("/account/logout-all")
async def logout_everywhere(request: Request, session: SessionDep, user: CurrentUser) -> Response:
    """휴대폰을 잃어버렸을 때 쓴다. 이 기기를 포함해 모든 기기에서 로그아웃하고 알림도 끊는다."""
    await end_other_sessions(session, user)
    await session.commit()
    request.session.clear()
    return Response(headers={"HX-Redirect": "/login"})
