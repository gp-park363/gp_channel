"""실기기 시험용 최소 화면(notification.md 11절 0단계). 시험이 끝나면 정식 알림 화면으로 바꾼다.

구독을 저장하지 않는다. 화면이 보낸 구독으로 그 자리에서 발송하고 끝낸다.
"""

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from app.core.deps import AuthzDep, CurrentUser
from app.core.templating import APP_DIR, templates
from app.services import push
from app.services.navigation import build_navigation
from app.services.work import RateLimiter

router = APIRouter(tags=["push"], include_in_schema=False)

SENDS_PER_MINUTE = 10
MAX_DELAY_SECONDS = 120

logger = logging.getLogger(__name__)
limiter = RateLimiter(SENDS_PER_MINUTE)
# 참조를 들고 있지 않으면 기다리던 발송이 중간에 사라질 수 있다.
waiting: set[asyncio.Task[None]] = set()


async def require_push(_: CurrentUser) -> None:
    """로그인을 먼저 확인해 로그인하지 않은 사람에게는 기능을 켰는지도 알리지 않는다."""
    if not push.enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "알림을 쓰지 않는 서버입니다.")


class Keys(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)


class SubscriptionIn(BaseModel):
    endpoint: str = Field(max_length=push.MAX_ENDPOINT)
    keys: Keys


class TestIn(BaseModel):
    subscription: SubscriptionIn
    delay: int = Field(default=0, ge=0, le=MAX_DELAY_SECONDS)


@router.get("/sw.js")
async def service_worker() -> FileResponse:
    """뿌리에서 내야 사이트 전체를 맡는다. 브라우저가 늘 최신을 확인하게 캐시하지 않는다."""
    return FileResponse(
        APP_DIR / "static/sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/push-test", response_class=HTMLResponse, dependencies=[Depends(require_push)])
async def push_test_page(request: Request, authz: AuthzDep) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "push_test.html",
        {
            "user": authz.user,
            "navigation": await build_navigation(authz),
            "active_tab": "more",
            "public_key": push.public_key(),
        },
    )


def message() -> dict[str, object]:
    now = time.time()
    return {
        "title": "바로채널 시험 알림",
        "sent_at": int(now * 1000),
        "tag": f"push-test-{int(now * 1000)}",
        "url": f"/push-test?opened={int(now * 1000)}",
    }


async def send_later(subscription: push.Subscription, delay: int, user_id: int) -> None:
    await asyncio.sleep(delay)
    result = await push.send(subscription, message())
    logger.info("시험 알림(사용자 %s, %s초 뒤): %s", user_id, delay, result.status)


@router.post("/push-test/send", dependencies=[Depends(require_push)])
async def send_test(user: CurrentUser, data: TestIn) -> dict[str, object]:
    if not limiter.allow(user.id):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "잠시 뒤에 다시 보내 주세요.")
    try:
        subscription = push.parse_subscription(
            data.subscription.endpoint, data.subscription.keys.p256dh, data.subscription.keys.auth
        )
    except push.PushError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    if data.delay:
        task = asyncio.create_task(send_later(subscription, data.delay, user.id))
        waiting.add(task)
        task.add_done_callback(waiting.discard)
        return {"scheduled": data.delay}
    result = await push.send(subscription, message())
    return {"ok": result.ok, "status": result.status, "detail": result.detail}
