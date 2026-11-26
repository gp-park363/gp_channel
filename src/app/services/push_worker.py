"""발송 작업. 대기열을 꺼내 사용자의 기기마다 보낸다(notification.md 5절).

- 여러 프로세스가 돌아도 FOR UPDATE SKIP LOCKED로 같은 건을 나눠 갖지 않는다.
- 외부 호출 동안 DB 트랜잭션을 열어 두지 않는다. 꺼낼 때 잠시 맡아 두고(lease) 커밋한다.
  중간에 멈추면 맡은 시간이 지난 뒤 다시 보낸다. 드물게 두 번 갈 수 있고 tag가 같아 겹쳐 보인다.
"""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select

from app.core.db import SessionLocal
from app.models import PushOutbox, PushSubscription
from app.services import push
from app.services.notify import decide

logger = logging.getLogger(__name__)

BATCH = 20
LEASE = timedelta(minutes=2)
GIVE_UP = timedelta(days=1)
KEEP = timedelta(days=30)
# 깨우는 신호를 놓쳐도 이 간격으로 확인한다.
POLL_SECONDS = 15
CLEANUP_EVERY = timedelta(hours=1)
# 다시 보낼 때까지 기다리는 시간. 횟수가 늘수록 길게 둔다.
BACKOFF = [timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10)]
LONGEST = timedelta(minutes=30)
GONE = (404, 410)


def backoff(attempts: int) -> timedelta:
    return BACKOFF[attempts - 1] if 0 < attempts <= len(BACKOFF) else LONGEST


def retryable(status: int) -> bool:
    """0은 연결 실패다. 나머지 4xx는 다시 보내도 같으므로 버린다."""
    return status == 0 or status == 429 or status >= 500


@dataclass(frozen=True)
class Target:
    id: int
    subscription: push.Subscription


async def claim() -> list[int]:
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        rows = list(
            await session.scalars(
                select(PushOutbox)
                .where(
                    PushOutbox.sent_at.is_(None),
                    PushOutbox.dropped_reason.is_(None),
                    PushOutbox.next_attempt_at <= now,
                )
                .order_by(PushOutbox.id)
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            row.attempts += 1
            row.next_attempt_at = now + LEASE
        await session.commit()
        return [row.id for row in rows]


async def prepare(outbox_id: int) -> tuple[dict[str, str], list[Target]] | None:
    """보낼 내용과 기기. 보내지 않을 건은 여기서 까닭을 남기고 None을 돌려준다."""
    async with SessionLocal() as session:
        row = await session.get(PushOutbox, outbox_id)
        if row is None or row.sent_at is not None or row.dropped_reason is not None:
            return None
        if row.created_at < datetime.now(UTC) - GIVE_UP:
            row.dropped_reason = "expired"
            await session.commit()
            return None
        decision = await decide(session, row)
        targets = []
        if decision.message is not None:
            for found in await session.scalars(
                select(PushSubscription).where(PushSubscription.user_id == row.user_id)
            ):
                try:
                    parsed = push.parse_subscription(found.endpoint, found.p256dh, found.auth)
                except push.PushError:
                    await session.delete(found)
                    continue
                targets.append(Target(found.id, parsed))
        if decision.message is None or not targets:
            row.dropped_reason = decision.reason or "no_device"
            if decision.reason == "inactive":
                await session.execute(
                    delete(PushSubscription).where(PushSubscription.user_id == row.user_id)
                )
            await session.commit()
            return None
        await session.commit()
        return decision.message, targets


async def deliver(outbox_id: int) -> None:
    prepared = await prepare(outbox_id)
    if prepared is None:
        return
    message, targets = prepared
    results = [(target, await push.send(target.subscription, message)) for target in targets]
    gone = [target.id for target, result in results if result.status in GONE]
    delivered = any(result.ok for _, result in results)
    again = any(retryable(result.status) for _, result in results)
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        if gone:
            await session.execute(delete(PushSubscription).where(PushSubscription.id.in_(gone)))
        row = await session.get(PushOutbox, outbox_id)
        if delivered:
            row.sent_at = now
        elif again:
            row.next_attempt_at = now + backoff(row.attempts)
        else:
            row.dropped_reason = "rejected"
        await session.commit()


async def run_once() -> int:
    """꺼낸 건수를 돌려준다. 시험은 이 함수를 직접 부른다."""
    ids = await claim()
    for outbox_id in ids:
        try:
            await deliver(outbox_id)
        except Exception:
            # 한 건 때문에 나머지가 멈추지 않게 한다. 맡은 시간이 지나면 다시 꺼낸다.
            logger.exception("알림 발송 중 오류(대기열 %s)", outbox_id)
    return len(ids)


async def cleanup() -> None:
    limit = datetime.now(UTC) - KEEP
    async with SessionLocal() as session:
        await session.execute(
            delete(PushOutbox).where(
                or_(PushOutbox.sent_at < limit, PushOutbox.created_at < limit),
            )
        )
        await session.commit()


class Worker:
    def __init__(self) -> None:
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        cleaned = datetime.min.replace(tzinfo=UTC)
        while True:
            try:
                while await run_once() == BATCH:
                    pass
                if datetime.now(UTC) - cleaned > CLEANUP_EVERY:
                    await cleanup()
                    cleaned = datetime.now(UTC)
            except Exception:
                logger.exception("알림 발송 작업 오류, 잠시 뒤 다시 시도합니다")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), POLL_SECONDS)
            self._wake.clear()

    def start(self) -> None:
        if push.enabled():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


worker = Worker()
