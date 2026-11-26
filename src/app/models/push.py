"""웹 푸시 알림. 기기별 구독, 발송 대기열, 종류별 켬/끔(notification.md 5절)."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class PushSubscription(Base):
    """브라우저 하나의 구독. 같은 기기에서 다른 사람이 로그인하면 그 사람의 것으로 옮긴다."""

    __tablename__ = "push_subscription"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    device_label: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PushOutbox(Base):
    """보낼 알림. 업무 처리와 같은 트랜잭션에서 넣어 롤백되면 나가지 않는다.

    dedupe_key는 한 트랜잭션에서 같은 사람에게 같은 글로 두 번 넣지 않게 한다.
    """

    __tablename__ = "push_outbox"
    __table_args__ = (
        Index(
            "ix_push_outbox_due",
            "next_attempt_at",
            postgresql_where=text("sent_at IS NULL AND dropped_reason IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    kind: Mapped[str] = mapped_column(String(30))
    post_id: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[int | None] = mapped_column(Integer)
    dedupe_key: Mapped[str] = mapped_column(String(100), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dropped_reason: Mapped[str | None] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PushPreference(Base):
    """행이 없으면 종류의 기본값을 따른다."""

    __tablename__ = "push_preference"

    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
