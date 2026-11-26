"""알림 설정 화면과 기기 구독(notification.md 5·7절)."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionDep
from app.core.deps import AuthzDep, CurrentUser
from app.core.templating import APP_DIR, fragment, templates
from app.models import PushPreference, PushSubscription
from app.services import notify, push
from app.services.navigation import build_navigation
from app.services.work import RateLimiter

router = APIRouter(tags=["alerts"], include_in_schema=False)

SENDS_PER_MINUTE = 10
MAX_DELAY_SECONDS = 120
# 한 사람이 알림을 받는 기기. 넘으면 가장 오래 안 쓴 기기부터 뺀다.
MAX_DEVICES = 10

logger = logging.getLogger(__name__)
limiter = RateLimiter(SENDS_PER_MINUTE)
# 참조를 들고 있지 않으면 기다리던 발송이 중간에 사라질 수 있다.
waiting: set[asyncio.Task[None]] = set()


async def require_push(_: CurrentUser) -> None:
    """로그인을 먼저 확인해 로그인하지 않은 사람에게는 기능을 켰는지도 알리지 않는다."""
    if not push.enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "알림을 쓰지 않는 서버입니다.")


PushOn = [Depends(require_push)]


class Keys(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)


class SubscriptionIn(BaseModel):
    endpoint: str = Field(max_length=push.MAX_ENDPOINT)
    keys: Keys


class EndpointIn(BaseModel):
    endpoint: str = Field(max_length=push.MAX_ENDPOINT)


class TestIn(BaseModel):
    subscription: SubscriptionIn
    delay: int = Field(default=0, ge=0, le=MAX_DELAY_SECONDS)


def parsed(data: SubscriptionIn) -> push.Subscription:
    try:
        return push.parse_subscription(data.endpoint, data.keys.p256dh, data.keys.auth)
    except push.PushError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error


def device_label(user_agent: str) -> str:
    """기기 목록에 보여 줄 이름. 정확하지 않아도 된다."""
    agent = user_agent or ""
    system = next(
        (
            name
            for key, name in [
                ("iPhone", "iPhone"),
                ("iPad", "iPad"),
                ("Android", "Android"),
                ("Windows", "Windows"),
                ("Macintosh", "Mac"),
                ("Linux", "Linux"),
            ]
            if key in agent
        ),
        "기기",
    )
    browser = next(
        (
            name
            for key, name in [
                ("SamsungBrowser", "삼성 인터넷"),
                ("Edg/", "Edge"),
                ("Firefox", "Firefox"),
                ("Chrome", "Chrome"),
                ("Safari", "Safari"),
            ]
            if key in agent
        ),
        "브라우저",
    )
    return f"{system} · {browser}"


@router.get("/sw.js")
async def service_worker() -> FileResponse:
    """뿌리에서 내야 사이트 전체를 맡는다. 브라우저가 늘 최신을 확인하게 캐시하지 않는다."""
    return FileResponse(
        APP_DIR / "static/sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/push-test", dependencies=PushOn)
async def old_test_page() -> RedirectResponse:
    """시험 단계의 주소. 홈 화면에 둔 링크가 끊기지 않게 옮겨 준다."""
    return RedirectResponse("/account/notifications", status.HTTP_303_SEE_OTHER)


async def devices(session: AsyncSession, user_id: int) -> list[PushSubscription]:
    return list(
        await session.scalars(
            select(PushSubscription)
            .where(PushSubscription.user_id == user_id)
            .order_by(PushSubscription.last_seen_at.desc())
        )
    )


@router.get("/account/notifications", response_class=HTMLResponse, dependencies=PushOn)
async def settings_page(request: Request, session: SessionDep, authz: AuthzDep) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "alerts.html",
        {
            "user": authz.user,
            "navigation": await build_navigation(authz),
            "active_tab": "more",
            "groups": notify.GROUPS,
            "chosen": await notify.preferences(session, authz.user.id),
            "devices": await devices(session, authz.user.id),
        },
    )


@router.get("/account/push/devices", response_class=HTMLResponse, dependencies=PushOn)
async def device_list(session: SessionDep, user: CurrentUser) -> HTMLResponse:
    return fragment("partials/push_devices.html", devices=await devices(session, user.id))


@router.post("/account/push/subscribe", dependencies=PushOn)
async def subscribe(
    request: Request, session: SessionDep, user: CurrentUser, data: SubscriptionIn
) -> dict[str, int]:
    """같은 기기(구독 주소)에 다른 사람이 로그인하면 그 사람의 것으로 옮긴다."""
    parsed(data)
    values = {
        "user_id": user.id,
        "p256dh": data.keys.p256dh,
        "auth": data.keys.auth,
        "device_label": device_label(request.headers.get("user-agent", "")),
        "last_seen_at": datetime.now(UTC),
    }
    statement = insert(PushSubscription).values(endpoint=data.endpoint, **values)
    device_id = await session.scalar(
        statement.on_conflict_do_update(
            index_elements=[PushSubscription.endpoint], set_=values
        ).returning(PushSubscription.id)
    )
    extra = (await devices(session, user.id))[MAX_DEVICES:]
    if extra:
        await session.execute(
            delete(PushSubscription).where(PushSubscription.id.in_([row.id for row in extra]))
        )
    await session.commit()
    return {"id": device_id}


@router.post("/account/push/unsubscribe", dependencies=PushOn)
async def unsubscribe(session: SessionDep, user: CurrentUser, data: EndpointIn) -> Response:
    await forget(session, user.id, data.endpoint)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def forget(session: AsyncSession, user_id: int, endpoint: str) -> None:
    """본인의 구독만 지운다. 로그아웃도 쓴다."""
    await session.execute(
        delete(PushSubscription).where(
            PushSubscription.endpoint == endpoint, PushSubscription.user_id == user_id
        )
    )


@router.post(
    "/account/push/devices/{device_id}/delete", response_class=HTMLResponse, dependencies=PushOn
)
async def delete_device(device_id: int, session: SessionDep, user: CurrentUser) -> HTMLResponse:
    await session.execute(
        delete(PushSubscription).where(
            PushSubscription.id == device_id, PushSubscription.user_id == user.id
        )
    )
    await session.commit()
    return HTMLResponse("")


@router.post("/account/push/preferences", response_class=HTMLResponse, dependencies=PushOn)
async def save_preferences(
    session: SessionDep,
    user: CurrentUser,
    groups: Annotated[list[str] | None, Form()] = None,
) -> HTMLResponse:
    chosen = set(groups or [])
    rows = [
        {"user_id": user.id, "kind": group, "enabled": group in chosen} for group in notify.DEFAULTS
    ]
    statement = insert(PushPreference).values(rows)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[PushPreference.user_id, PushPreference.kind],
            set_={"enabled": statement.excluded.enabled},
        )
    )
    await session.commit()
    return fragment("partials/message.html", message="저장했습니다.")


def test_message() -> dict[str, object]:
    now = int(time.time() * 1000)
    return {
        "title": "바로채널 시험 알림",
        "sent_at": now,
        "tag": f"test-{now}",
        "url": f"/account/notifications?opened={now}",
    }


async def send_later(subscription: push.Subscription, delay: int, user_id: int) -> None:
    await asyncio.sleep(delay)
    result = await push.send(subscription, test_message())
    logger.info("시험 알림(사용자 %s, %s초 뒤): %s", user_id, delay, result.status)


@router.post("/account/push/test", dependencies=PushOn)
async def send_test(user: CurrentUser, data: TestIn) -> dict[str, object]:
    """이 기기로 바로 보낸다. 대기열을 거치지 않으므로 업무 알림과 따로 확인할 수 있다."""
    if not limiter.allow(user.id):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "잠시 뒤에 다시 보내 주세요.")
    subscription = parsed(data.subscription)
    if data.delay:
        task = asyncio.create_task(send_later(subscription, data.delay, user.id))
        waiting.add(task)
        task.add_done_callback(waiting.discard)
        return {"scheduled": data.delay}
    result = await push.send(subscription, test_message())
    return {"ok": result.ok, "status": result.status, "detail": result.detail}
