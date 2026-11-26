"""로그인 개선: 14일 유지, 서버에서 끊기, 로그인 뒤 원래 주소, 시도 제한(notification.md 7절)."""

import time

import itsdangerous.timed
import pytest
from sqlalchemy import select
from starlette.requests import Request

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.deps import LoginRequired
from app.core.sessions import REFRESH_SECONDS
from app.models import PushSubscription, User
from app.routers.events import count_stream, signed_in
from app.services.delivery import count_state, still_allowed
from app.services.login import LoginGuard, guard, safe_next
from tests.conftest import (
    FULL,
    PASSWORD,
    Browser,
    add_channel,
    add_group,
    add_user,
    browser,
    grant,
    run,
)

DAY = 24 * 60 * 60
HTML = {"Accept": "text/html,application/xhtml+xml"}


@pytest.fixture(autouse=True)
def fresh_guard():
    guard.clear()
    yield
    guard.clear()


class Clock:
    """쿠키 서명과 세션 갱신이 보는 시계를 함께 옮긴다."""

    def __init__(self, monkeypatch) -> None:
        self.offset = 0
        real = time.time

        class Fake:
            @staticmethod
            def time() -> float:
                return real() + self.offset

        monkeypatch.setattr(itsdangerous.timed, "time", Fake)
        monkeypatch.setattr("app.core.sessions.time", Fake)

    def forward(self, days: float) -> None:
        self.offset += int(days * DAY)


def user_row(user_id: int) -> User:
    async def load():
        async with SessionLocal() as session:
            return await session.get(User, user_id)

    return run(load())


# 14일 유지


def test_session_is_extended_while_used_and_ends_after_14_idle_days(monkeypatch) -> None:
    clock = Clock(monkeypatch)
    signed = browser(add_user("user"))
    assert "set-cookie" not in signed.get("/").headers
    for _ in range(4):
        # 쓰는 동안은 처음 로그인부터 14일이 지나도 이어진다.
        clock.forward(5)
        response = signed.get("/")
        assert response.status_code == 200 and "set-cookie" in response.headers
    clock.forward(14.1)
    assert signed.get("/").status_code == 303


def test_cookie_is_reissued_at_most_once_a_day(monkeypatch) -> None:
    clock = Clock(monkeypatch)
    signed = browser(add_user("user"))
    clock.forward(REFRESH_SECONDS / DAY + 0.01)
    assert "set-cookie" in signed.get("/").headers
    clock.forward(0.5)
    assert "set-cookie" not in signed.get("/").headers


def test_default_lifetime_is_14_days() -> None:
    assert get_settings().session_max_age_seconds == 14 * DAY


# 서버에서 끊기


def test_password_change_signs_out_other_devices_but_not_this_one() -> None:
    user = add_user("user")
    here, there = browser(user), browser(user)
    response = here.post(
        "/account/password",
        data={"current_password": PASSWORD, "new_password": "new-password-1"},
    )
    assert "다른 기기" in response.text
    assert here.get("/").status_code == 200
    # 열려 있던 화면의 보안 토큰도 그대로 쓴다.
    assert here.post("/channels/0/read").status_code == 404
    assert there.get("/").status_code == 303
    assert user_row(user.id).session_version == 1


def test_logout_everywhere_signs_out_all_and_removes_devices() -> None:
    user = add_user("user")
    here, there = browser(user), browser(user)

    async def add_device() -> None:
        async with SessionLocal() as session:
            session.add(
                PushSubscription(
                    user_id=user.id, endpoint="https://fcm.googleapis.com/x", p256dh="a", auth="b"
                )
            )
            await session.commit()

    run(add_device())
    response = here.post("/account/logout-all")
    assert response.headers["HX-Redirect"] == "/login"
    assert here.get("/").status_code == 303 and there.get("/").status_code == 303

    async def devices() -> list:
        async with SessionLocal() as session:
            return list(await session.scalars(select(PushSubscription.id)))

    assert run(devices()) == []
    assert "모든 기기에서 로그아웃" in browser(user).get("/more").text


def test_realtime_connections_end_when_signed_out_elsewhere() -> None:
    user = add_user("user")
    channel = add_channel(add_group(user))
    grant(channel, user, **FULL)
    old = user_row(user.id).session_version
    assert run(still_allowed(user.id, channel.id, old))
    assert run(count_state(user.id, old)) is not None
    browser(user).post("/account/logout-all")
    assert not run(still_allowed(user.id, channel.id, old))
    assert run(count_state(user.id, old)) is None

    async def first_event():
        return await anext(count_stream(user.id, old), None)

    assert run(first_event()) is None


def test_realtime_requires_a_valid_session() -> None:
    """연결을 실제로 열면 실패할 때 끝나지 않으므로 연결 전 확인만 부른다."""
    user = add_user("user")
    old = {"user_id": user.id, "ver": user_row(user.id).session_version}

    def check(stored: dict):
        return run(signed_in(Request({"type": "http", "session": stored, "headers": []})))

    assert check(dict(old)) == (user.id, old["ver"])
    browser(user).post("/account/logout-all")
    with pytest.raises(LoginRequired):
        check(dict(old))
    with pytest.raises(LoginRequired):
        check({})


def test_changing_admin_password_signs_out_admin_sessions(monkeypatch) -> None:
    admin = Browser().login("admin@test.local")
    assert admin.get("/").status_code == 200
    monkeypatch.setattr(get_settings(), "admin_password", "changed-password")
    assert admin.get("/").status_code == 303


# 로그인 뒤 원래 주소


def test_page_redirects_to_login_with_next_and_back_after_login() -> None:
    add_user("user")
    anonymous = Browser()
    response = anonymous.get("/posts/5?x=1", headers=HTML)
    assert response.headers["location"] == "/login?next=%2Fposts%2F5%3Fx%3D1"
    page = anonymous.get(response.headers["location"]).text
    assert 'name="next" value="/posts/5?x=1"' in page
    anonymous.refresh_token()
    login = anonymous.post(
        "/login", data={"email": "user@test.local", "password": PASSWORD, "next": "/posts/5?x=1"}
    )
    assert login.headers["HX-Redirect"] == "/posts/5?x=1"


def test_htmx_and_non_page_requests() -> None:
    anonymous = Browser()
    anonymous.refresh_token()
    response = anonymous.http.post(
        "/posts/1/ack",
        headers={
            "X-CSRF-Token": anonymous.token,
            "HX-Request": "true",
            "HX-Current-URL": "http://localhost/channels/3?a=b",
        },
    )
    assert response.headers["HX-Redirect"] == "/login?next=%2Fchannels%2F3%3Fa%3Db"
    # 실시간 연결은 화면이 아니므로 돌아갈 주소를 남기지 않는다.
    events = anonymous.get("/events/me", headers={"Accept": "text/event-stream"})
    assert events.headers["location"] == "/login"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "https://evil.example/",
        "//evil.example/x",
        "/\\evil.example",
        "javascript:alert(1)",
        "posts/1",
        "/login",
        "/logout",
        "/posts/1\nSet-Cookie: a=b",
        "/" + "a" * 2001,
    ],
)
def test_only_paths_on_this_site_are_followed(value: str) -> None:
    assert safe_next(value) == "/"


def test_login_ignores_unsafe_next() -> None:
    add_user("user")
    anonymous = Browser()
    anonymous.refresh_token()
    response = anonymous.post(
        "/login",
        data={"email": "user@test.local", "password": PASSWORD, "next": "//evil.example"},
    )
    assert response.headers["HX-Redirect"] == "/"
    assert "evil" not in Browser().get("/login?next=//evil.example").text


# 시도 제한


def attempt(email: str, password: str):
    anonymous = Browser()
    anonymous.refresh_token()
    return anonymous.post("/login", data={"email": email, "password": password})


def test_account_is_held_after_repeated_failures() -> None:
    add_user("user")
    add_user("other")
    for _ in range(LoginGuard.PER_ACCOUNT):
        assert "맞지 않습니다" in attempt("user@test.local", "wrong").text
    held = attempt("user@test.local", PASSWORD)
    assert "HX-Redirect" not in held.headers and "너무 많습니다" in held.text
    # 다른 계정은 막지 않는다.
    assert attempt("other@test.local", PASSWORD).headers["HX-Redirect"] == "/"


def test_unknown_accounts_are_counted_the_same_way() -> None:
    for _ in range(LoginGuard.PER_ACCOUNT):
        attempt("nobody@test.local", "wrong")
    assert "너무 많습니다" in attempt("nobody@test.local", "wrong").text


def test_success_clears_account_failures() -> None:
    add_user("user")
    for _ in range(LoginGuard.PER_ACCOUNT - 1):
        attempt("user@test.local", "wrong")
    assert attempt("user@test.local", PASSWORD).headers["HX-Redirect"] == "/"
    assert "맞지 않습니다" in attempt("user@test.local", "wrong").text


def test_guard_window_and_address_limit() -> None:
    local = LoginGuard()
    for number in range(LoginGuard.PER_ADDRESS):
        local.failed(f"user{number}@test.local", "10.0.0.1", now=1000)
    assert local.wait_seconds("new@test.local", "10.0.0.1", now=1000) > 0
    assert local.wait_seconds("new@test.local", "10.0.0.2", now=1000) == 0
    assert local.wait_seconds("new@test.local", "10.0.0.1", now=1000 + LoginGuard.WINDOW) == 0
