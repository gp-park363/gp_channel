"""로그인 세션. 14일 유지, 서버에서 끊기, 로그인 뒤 원래 주소, 시도 제한(notification.md 7절)."""

import hashlib
import hmac
import logging
import time
from collections import defaultdict, deque
from collections.abc import MutableMapping
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import PushSubscription, User
from app.services.admin import is_system_admin

logger = logging.getLogger(__name__)

Session = MutableMapping[str, Any]


def admin_key() -> str:
    """환경 변수의 관리자 비밀번호가 바뀌면 달라지는 값. 바꾸면 관리자 세션이 모두 끊긴다."""
    settings = get_settings()
    digest = hmac.new(
        settings.secret_key.encode(), settings.admin_password.encode(), hashlib.sha256
    )
    return digest.hexdigest()[:32]


def session_valid(stored: Session, user: User) -> bool:
    """세션이 만들어진 뒤 비밀번호를 바꾸거나 모든 기기에서 로그아웃했으면 끊는다."""
    if stored.get("ver", 0) != user.session_version:
        return False
    return not is_system_admin(user) or stored.get("admin") == admin_key()


def start_session(stored: Session, user: User) -> None:
    stored.clear()
    stored["user_id"] = user.id
    stored["ver"] = user.session_version
    stored["issued"] = int(time.time())
    if is_system_admin(user):
        stored["admin"] = admin_key()


async def end_other_sessions(session: AsyncSession, user: User) -> None:
    """이 사람의 모든 세션을 끊는다. 이 기기의 세션은 부른 쪽이 다시 맞추거나 지운다.

    알림 기기도 지운다. 계속 쓰는 기기는 화면을 열 때 다시 등록된다(push.js).
    """
    user.session_version += 1
    await session.execute(delete(PushSubscription).where(PushSubscription.user_id == user.id))


def safe_next(value: str | None) -> str:
    """이 사이트 안의 경로만 돌려준다. 다른 사이트로 보내는 주소는 홈으로 바꾼다."""
    if not value or len(value) > 2000 or not value.startswith("/"):
        return "/"
    if value.startswith(("//", "/\\")) or any(ord(char) < 32 for char in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path in ("/login", "/logout"):
        return "/"
    return value


class LoginGuard:
    """실패한 로그인을 계정과 IP별로 세어 잠시 막는다. 프로세스마다 따로 센다.

    없는 계정도 똑같이 세어 계정이 있는지 드러나지 않게 한다.
    """

    WINDOW = 15 * 60
    PER_ACCOUNT = 10
    PER_ADDRESS = 50

    def __init__(self) -> None:
        self.failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str, now: float) -> deque[float]:
        found = self.failures[key]
        while found and found[0] <= now - self.WINDOW:
            found.popleft()
        if not found:
            del self.failures[key]
            return deque()
        return found

    def wait_seconds(self, email: str, address: str, now: float | None = None) -> int:
        """막혀 있으면 기다릴 초, 아니면 0."""
        now = time.time() if now is None else now
        waits = []
        for key, limit in (
            (f"account:{email}", self.PER_ACCOUNT),
            (f"ip:{address}", self.PER_ADDRESS),
        ):
            found = self._recent(key, now)
            if len(found) >= limit:
                waits.append(int(found[-limit] + self.WINDOW - now) + 1)
        return max(waits, default=0)

    def failed(self, email: str, address: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for key in (f"account:{email}", f"ip:{address}"):
            self.failures[key].append(now)
        if len(self.failures) > 10_000:
            for key in list(self.failures):
                self._recent(key, now)
        if len(self.failures[f"account:{email}"]) == self.PER_ACCOUNT:
            logger.warning("로그인 실패가 많아 계정을 잠시 막습니다: %s", email[:100])

    def succeeded(self, email: str) -> None:
        self.failures.pop(f"account:{email}", None)

    def clear(self) -> None:
        self.failures.clear()


guard = LoginGuard()
