"""알림을 대기열에 넣고, 보내기 직전에 아직 보낼 일인지 판정한다(notification.md 3·5절).

업무 처리는 enqueue만 부른다. 같은 트랜잭션에서 넣으므로 롤백되면 나가지 않는다.
보낼 내용에는 종류와 채널명만 싣는다. 제목, 본문, 보낸 사람은 싣지 않는다.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Approval,
    ApprovalReference,
    ApprovalReview,
    ApprovalStep,
    Channel,
    FlowItem,
    FlowTask,
    MentionAck,
    Post,
    PushOutbox,
    PushPreference,
    User,
)
from app.services import push
from app.services.approvals import can_approve
from app.services.permissions import Authorizer, is_closed
from app.services.realtime import NOTIFY_CHANNEL


@dataclass(frozen=True)
class Kind:
    title: str
    # 알림 설정에서 켜고 끄는 묶음
    group: str
    # 한 트랜잭션에서 같은 사람에게 같은 글로 겹치면 큰 쪽 하나만 보낸다.
    priority: int


KINDS = {
    "approval_turn": Kind("결재 차례입니다", "approval", 90),
    "flow_turn": Kind("워크플로우 차례입니다", "flow", 90),
    "flow_returned": Kind("보완 요청이 왔습니다", "flow", 85),
    "approval_review": Kind("검토 요청이 왔습니다", "approval", 80),
    "approval_result": Kind("결재가 끝났습니다", "approval_result", 70),
    "flow_result": Kind("워크플로우가 끝났습니다", "flow_result", 70),
    "mention_again": Kind("다시 확인해 주세요", "mention", 60),
    "mention": Kind("읽음 확인 요청이 왔습니다", "mention", 50),
    "approval_reference": Kind("참조 문서가 왔습니다", "approval_reference", 30),
    "approval_receiving": Kind("수신 대기 문서가 왔습니다", "approval_receiving", 20),
}

# 알림 설정 화면의 순서. (묶음, 이름, 설명, 기본값)
GROUPS = [
    ("mention", "멘션", "@멘션과 다시 확인 요청", True),
    ("approval", "결재 차례·검토", "승인할 차례, 검토 요청", True),
    ("approval_result", "결재 결과", "내가 올린 결재의 승인·반려·폐기", True),
    ("flow", "워크플로우 차례·보완", "처리할 차례, 보완 요청", True),
    ("flow_result", "워크플로우 결과", "내가 등록한 건의 완료·반려·폐기", True),
    ("approval_reference", "참조 문서", "참조자로 지정됨", False),
    ("approval_receiving", "수신 대기", "수신 채널에 문서가 옴. 받을 사람이 많을 수 있다", False),
]
DEFAULTS = {group: default for group, _, _, default in GROUPS}


async def enqueue(
    session: AsyncSession,
    kind: str,
    post_id: int,
    user_ids: Iterable[int],
    actor_id: int | None,
) -> None:
    """본인의 행동은 본인에게 알리지 않는다. 알림을 끈 서버에서는 쌓지 않는다."""
    ids = sorted(set(user_ids) - {actor_id})
    if not ids or not push.enabled():
        return
    txid = await session.scalar(select(func.txid_current()))
    statement = insert(PushOutbox).values(
        [
            {
                "user_id": user_id,
                "kind": kind,
                "post_id": post_id,
                "actor_id": actor_id,
                "dedupe_key": f"{post_id}:{user_id}:{txid}",
                "attempts": 0,
            }
            for user_id in ids
        ]
    )
    excluded = statement.excluded
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[PushOutbox.dedupe_key],
            set_={
                "kind": case(
                    (_priority(excluded.kind) > _priority(PushOutbox.kind), excluded.kind),
                    else_=PushOutbox.kind,
                )
            },
        )
    )
    # 커밋될 때 발송 작업을 깨운다.
    await session.execute(select(func.pg_notify(NOTIFY_CHANNEL, json.dumps({"push": 1}))))


def _priority(column):
    return case({name: kind.priority for name, kind in KINDS.items()}, value=column, else_=0)


async def enabled_for(session: AsyncSession, user_id: int, group: str) -> bool:
    value = await session.scalar(
        select(PushPreference.enabled).where(
            PushPreference.user_id == user_id, PushPreference.kind == group
        )
    )
    return DEFAULTS.get(group, False) if value is None else value


async def preferences(session: AsyncSession, user_id: int) -> dict[str, bool]:
    rows = dict(
        (
            await session.execute(
                select(PushPreference.kind, PushPreference.enabled).where(
                    PushPreference.user_id == user_id
                )
            )
        ).all()
    )
    return {group: rows.get(group, default) for group, default in DEFAULTS.items()}


@dataclass(frozen=True)
class Decision:
    message: dict[str, str] | None
    # 보내지 않는 까닭. 대기열에 남겨 확인할 수 있게 한다.
    reason: str = ""


def skip(reason: str) -> Decision:
    return Decision(None, reason)


async def decide(session: AsyncSession, row: PushOutbox) -> Decision:
    """보내기 직전의 판정. 그 사이 처리했거나 권한을 잃었으면 보내지 않는다."""
    kind = KINDS.get(row.kind)
    if kind is None:
        return skip("unknown")
    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        return skip("inactive")
    if not await enabled_for(session, user.id, kind.group):
        return skip("off")
    post = await session.get(Post, row.post_id)
    if post is None or post.deleted_at is not None:
        return skip("deleted")
    channel = await session.get(Channel, post.channel_id)
    if channel is None or is_closed(channel):
        return skip("closed")
    parent = await session.get(Post, post.parent_post_id) if post.parent_post_id else None
    # post_views는 워크플로우 화면을 거쳐 flows를 부른다. flows가 이 모듈을 부르므로 여기서 부른다.
    from app.services.post_views import can_view_any

    access = await Authorizer(session, user).post_access(channel, post, parent)
    if not can_view_any(access, post, parent, user.id):
        return skip("no_access")
    if not await still_pending(session, row.kind, post, user.id):
        return skip("done")
    return Decision(
        {
            "title": kind.title,
            "body": channel.name,
            "tag": f"{row.kind}-{post.id}",
            "url": url_for(row.kind, post.id),
        }
    )


def url_for(kind: str, post_id: int) -> str:
    if kind.startswith("approval_"):
        return f"/approvals/{post_id}"
    if kind.startswith("flow_"):
        return f"/flows/{post_id}"
    # 답글이면 원글 화면으로, 결재·워크플로우 글이면 그 화면으로 옮겨 준다.
    return f"/posts/{post_id}"


async def still_pending(session: AsyncSession, kind: str, post: Post, user_id: int) -> bool:
    if kind in ("mention", "mention_again"):
        return bool(
            await session.scalar(
                select(MentionAck.post_id).where(
                    MentionAck.post_id == post.id,
                    MentionAck.user_id == user_id,
                    MentionAck.acked_at.is_(None),
                )
            )
        )
    if kind.startswith("approval_"):
        approval = await session.scalar(select(Approval).where(Approval.post_id == post.id))
        return approval is not None and await approval_pending(session, kind, approval, user_id)
    item = await session.scalar(select(FlowItem).where(FlowItem.post_id == post.id))
    if item is None:
        return False
    if kind == "flow_turn":
        return item.status == "in_progress" and bool(
            await session.scalar(
                select(FlowTask.id).where(
                    FlowTask.item_id == item.id,
                    FlowTask.round == item.round,
                    FlowTask.step_no == item.current_step,
                    FlowTask.assignee_id == user_id,
                    FlowTask.status == "waiting",
                )
            )
        )
    if kind == "flow_returned":
        return item.status == "returned" and item.registrant_id == user_id
    return item.status in ("done", "rejected", "discarded")


async def approval_pending(
    session: AsyncSession, kind: str, approval: Approval, user_id: int
) -> bool:
    if kind == "approval_turn":
        steps = list(
            await session.scalars(
                select(ApprovalStep).where(ApprovalStep.approval_id == approval.id)
            )
        )
        reviews = list(
            await session.scalars(
                select(ApprovalReview).where(ApprovalReview.approval_id == approval.id)
            )
        )
        return bool(steps) and can_approve(approval, steps, reviews, user_id)
    if kind == "approval_review":
        return approval.status == "in_progress" and bool(
            await session.scalar(
                select(ApprovalReview.reviewer_id).where(
                    ApprovalReview.approval_id == approval.id,
                    ApprovalReview.reviewer_id == user_id,
                    ApprovalReview.result.is_(None),
                )
            )
        )
    if kind == "approval_reference":
        return approval.status != "draft" and bool(
            await session.scalar(
                select(ApprovalReference.user_id).where(
                    ApprovalReference.approval_id == approval.id,
                    ApprovalReference.user_id == user_id,
                    ApprovalReference.read_at.is_(None),
                )
            )
        )
    if kind == "approval_receiving":
        return approval.status == "receiving"
    return approval.status in ("approved", "rejected", "discarded")
