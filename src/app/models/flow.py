from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

FLOW_STATUS_LABELS = {
    "draft": "임시저장",
    "in_progress": "진행 중",
    "returned": "보완 중",
    "done": "완료",
    "rejected": "반려",
    "withdrawn": "회수",
    "discarded": "폐기",
}
FLOW_OPEN = ("in_progress", "returned")


class FlowDefinition(Base):
    """채널의 단계와 입력 항목. 발행하면 버전이 붙고 고치지 않는다. 버전이 비면 초안이다."""

    __tablename__ = "flow_definition"
    __table_args__ = (
        UniqueConstraint("channel_id", "version", name="uq_flow_definition_version"),
        Index(
            "ix_flow_definition_draft",
            "channel_id",
            unique=True,
            postgresql_where=text("version IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channel.id"))
    version: Mapped[int | None] = mapped_column(Integer)
    steps: Mapped[list[dict]] = mapped_column(JSONB)
    created_by: Mapped[int] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FlowAssignee(Base):
    """고정 지정 단계의 담당자, 명단 단계의 명단. 채널의 읽기·답글 권한을 받는다."""

    __tablename__ = "flow_assignee"

    channel_id: Mapped[int] = mapped_column(ForeignKey("channel.id"), primary_key=True)
    step_key: Mapped[str] = mapped_column(String(10), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True, index=True)


class FlowItem(Base):
    __tablename__ = "flow_item"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'in_progress', 'returned', 'done', 'rejected', 'withdrawn',"
            " 'discarded')",
            name="status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"), unique=True)
    definition_id: Mapped[int] = mapped_column(ForeignKey("flow_definition.id"), index=True)
    registrant_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    # 보완 요청으로 되돌아갈 때마다 오른다.
    round: Mapped[int] = mapped_column(Integer, default=1)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    # 등록 항목의 값. 파일 항목은 첨부 번호 목록이다.
    values: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 완료한 단계. 중간 확정에서 마무리했으면 그 단계다.
    closed_step: Mapped[int | None] = mapped_column(Integer)
    # 새로 등록의 원래 건.
    source_post_id: Mapped[int | None] = mapped_column(ForeignKey("post.id"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(Text)


class FlowPick(Base):
    """등록자가 지정한 담당자."""

    __tablename__ = "flow_pick"

    item_id: Mapped[int] = mapped_column(ForeignKey("flow_item.id"), primary_key=True)
    step_key: Mapped[str] = mapped_column(String(10), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True, index=True)


class FlowSubmission(Base):
    """등록자가 제출한 내용의 사본. 고치지 않는다."""

    __tablename__ = "flow_submission"

    item_id: Mapped[int] = mapped_column(ForeignKey("flow_item.id"), primary_key=True)
    round: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    values: Mapped[dict] = mapped_column(JSONB)
    picks: Mapped[dict] = mapped_column(JSONB)
    # 보완 요청으로 돌려보내 잠긴 내용. 이후의 제출은 추가한 내용만 담는다.
    locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FlowTask(Base):
    """단계가 시작될 때 담당자마다 한 행을 만든다. 끝난 행은 그대로 남아 회차 이력이 된다."""

    __tablename__ = "flow_task"
    __table_args__ = (
        CheckConstraint(
            "status IN ('waiting', 'done', 'returned', 'rejected', 'skipped', 'replaced',"
            " 'voided')",
            name="status",
        ),
        CheckConstraint("decision IN ('finish', 'continue')", name="decision"),
        Index(
            "ix_flow_task_active",
            "item_id",
            "round",
            "step_no",
            "assignee_id",
            unique=True,
            postgresql_where=text("status NOT IN ('replaced', 'voided')"),
        ),
        Index(
            "ix_flow_task_waiting",
            "assignee_id",
            postgresql_where=text("status = 'waiting'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("flow_item.id"))
    round: Mapped[int] = mapped_column(Integer)
    step_no: Mapped[int] = mapped_column(Integer)
    assignee_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    status: Mapped[str] = mapped_column(String(10), default="waiting")
    # 중간 확정의 선택.
    decision: Mapped[str | None] = mapped_column(String(10))
    # 보완 요청으로 돌려보낸 단계.
    return_to: Mapped[int | None] = mapped_column(Integer)
    values: Mapped[dict] = mapped_column(JSONB, default=dict)
    comment: Mapped[str | None] = mapped_column(Text)
    # 보완 요청으로 돌려보내 잠긴 완료. 다시 한 완료는 추가한 내용만 담는다.
    locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FlowReply(Base):
    """쓰레드 답글 중 추가 내용. 남긴 때의 단계와 회차를 함께 둔다."""

    __tablename__ = "flow_reply"

    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"), primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("flow_item.id"), index=True)
    round: Mapped[int] = mapped_column(Integer)
    step_no: Mapped[int] = mapped_column(Integer)
    item_status: Mapped[str] = mapped_column(String(20))


class FlowApprovalTarget(Base):
    """완료한 건을 올릴 수 있는 결재 채널."""

    __tablename__ = "flow_approval_target"

    channel_id: Mapped[int] = mapped_column(ForeignKey("channel.id"), primary_key=True)
    target_channel_id: Mapped[int] = mapped_column(ForeignKey("channel.id"), primary_key=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


class FlowApproval(Base):
    """건에서 올린 결재."""

    __tablename__ = "flow_approval"

    approval_id: Mapped[int] = mapped_column(ForeignKey("approval.id"), primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("flow_item.id"), index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
