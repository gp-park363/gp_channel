"""업무 알림: 대기열에 넣는 지점, 발송 직전 판정, 발송 작업, 기기 구독."""

import json

import httpx2
import pytest
from sqlalchemy import select, update

from app.core.db import SessionLocal
from app.models import PushOutbox, PushPreference, PushSubscription, User
from app.services import push
from app.services.push_worker import run_once
from app.services.realtime import Hub
from tests.conftest import FULL, add_channel, add_group, add_user, browser, grant, post_id, run
from tests.test_approvals import make_office
from tests.test_flows import act, completed, make_place, process, submitted
from tests.test_push import Device


@pytest.fixture(autouse=True)
def vapid(monkeypatch):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_private_key", push.generate_private_key())
    monkeypatch.setattr(settings, "vapid_subject", "mailto:push@example.com")
    return settings


class Service:
    """푸시 서비스. 받은 요청을 기기별로 모으고 정한 상태로 답한다."""

    def __init__(self) -> None:
        self.status = 201
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status)


@pytest.fixture
def service(monkeypatch) -> Service:
    fake = Service()
    monkeypatch.setattr(push, "transport", httpx2.MockTransport(fake))
    return fake


def fetch(query) -> list:
    async def load():
        async with SessionLocal() as session:
            return list(await session.scalars(query))

    return run(load())


def execute(statement) -> None:
    async def go():
        async with SessionLocal() as session:
            await session.execute(statement)
            await session.commit()

    run(go())


def queued() -> list[tuple[str, int, str | None]]:
    """(종류, 받는 사람, 보내지 않은 까닭)"""
    rows = fetch(select(PushOutbox).order_by(PushOutbox.id))
    return [(row.kind, row.user_id, row.dropped_reason) for row in rows]


def kinds_for(user: User) -> list[str]:
    return [kind for kind, user_id, _ in queued() if user_id == user.id]


def device_of(user: User, endpoint: str | None = None) -> Device:
    """사용자가 그 기기로 알림을 켠다."""
    device = Device(endpoint or f"https://fcm.googleapis.com/fcm/send/{user.name}")
    response = browser(user).post("/account/push/subscribe", json=device.json())
    assert response.status_code == 200, response.text
    return device


def deliver() -> int:
    return run(run_once())


def opened(device: Device, service: Service) -> list[dict]:
    return [
        device.open(request.content)
        for request in service.requests
        if str(request.url) == device.endpoint
    ]


@pytest.fixture
def talk():
    author = add_user("author")
    target = add_user("target")
    channel = add_channel(add_group(author), "공지")
    for user in (author, target):
        grant(channel, user, **FULL)
    return channel, author, target


def mention(channel, author, body="@target 확인해 주세요") -> int:
    response = browser(author).post(f"/channels/{channel.id}/posts", data={"body": body})
    assert response.status_code == 200, response.text
    return post_id(response)


# 멘션


def test_mention_reaches_the_target_device_with_only_kind_and_channel(talk, service) -> None:
    channel, author, target = talk
    device = device_of(target)
    device_of(author)
    post = mention(channel, author, "@target 비밀 본문")
    assert queued() == [("mention", target.id, None)]
    assert deliver() == 1
    (payload,) = opened(device, service)
    assert payload == {
        "title": "읽음 확인 요청이 왔습니다",
        "body": "공지",
        "tag": f"mention-{post}",
        "url": f"/posts/{post}",
    }
    assert "비밀" not in json.dumps(payload, ensure_ascii=False)
    assert fetch(select(PushOutbox.sent_at))[0] is not None
    assert deliver() == 0


def test_acknowledged_before_sending_is_not_sent(talk, service) -> None:
    channel, author, target = talk
    device_of(target)
    post = mention(channel, author)
    browser(target).post(f"/posts/{post}/ack")
    deliver()
    assert queued() == [("mention", target.id, "done")]
    assert service.requests == []


def test_editing_without_new_targets_does_not_notify_again_but_reconfirm_does(talk) -> None:
    channel, author, target = talk
    post = mention(channel, author)
    browser(target).post(f"/posts/{post}/ack")
    browser(author).post(f"/posts/{post}/edit", data={"body": "@target 고친 공지"})
    assert kinds_for(target) == ["mention"]
    browser(author).post(f"/posts/{post}/edit", data={"body": "@target 공지", "reconfirm": "true"})
    assert kinds_for(target) == ["mention", "mention_again"]


def test_nothing_is_queued_when_push_is_off(talk, vapid, monkeypatch) -> None:
    monkeypatch.setattr(vapid, "vapid_private_key", "")
    channel, author, _ = talk
    mention(channel, author)
    assert queued() == []


def test_turned_off_kind_and_lost_access_are_not_sent(talk, service) -> None:
    channel, author, target = talk
    device_of(target)
    browser(target).post("/account/push/preferences", data={"groups": ["approval"]})
    mention(channel, author)
    deliver()
    assert queued()[-1][2] == "off"
    browser(target).post("/account/push/preferences", data={"groups": ["mention", "approval"]})
    mention(channel, author, "@target 두 번째")
    execute(update(User).where(User.id == target.id).values(status="inactive"))
    deliver()
    assert queued()[-1][2] == "inactive"
    # 비활성 사용자의 기기는 지운다.
    assert fetch(select(PushSubscription.id)) == fetch(
        select(PushSubscription.id).where(PushSubscription.user_id == author.id)
    )
    assert service.requests == []


def test_preferences_are_saved_per_user(talk) -> None:
    _, _, target = talk
    signed_in = browser(target)
    assert 'value="approval_receiving">' in signed_in.get("/account/notifications").text
    signed_in.post("/account/push/preferences", data={"groups": ["mention"]})
    saved = dict(
        (row.kind, row.enabled)
        for row in fetch(select(PushPreference).where(PushPreference.user_id == target.id))
    )
    assert saved["mention"] is True and saved["approval"] is False
    page = signed_in.get("/account/notifications").text
    assert 'value="mention" checked' in page and 'value="approval" checked' not in page


# 결재


def test_approval_turns_review_and_result() -> None:
    office = make_office()
    reviewer = add_user("checker")
    signed_in = browser(office.drafter)
    from tests.test_approvals import draft

    post = draft(signed_in, office, body="@first 함께 봐 주세요")
    signed_in.post(f"/approvals/{post}/submit", data={"reviewer_ids": [reviewer.id]})
    # 같은 글로 멘션과 차례가 겹치면 차례 하나만 보낸다.
    assert kinds_for(office.first) == ["approval_turn"]
    assert kinds_for(reviewer) == ["approval_review"]
    assert browser(office.first).post(f"/approvals/{post}/approve").status_code == 200
    assert kinds_for(office.final) == ["approval_turn"]
    deliver()
    # 검토가 남아 최종 승인자는 아직 누를 수 없다.
    final_rows = [row for row in queued() if row[1] == office.final.id]
    assert final_rows == [("approval_turn", office.final.id, "done")]
    browser(reviewer).post(f"/approvals/{post}/review", data={"result": "pass"})
    assert kinds_for(office.final) == ["approval_turn", "approval_turn"]
    # 결과를 바꿔도 다시 알리지 않는다.
    browser(reviewer).post(f"/approvals/{post}/review", data={"result": "revise"})
    assert kinds_for(office.final) == ["approval_turn", "approval_turn"]
    assert browser(office.final).post(f"/approvals/{post}/approve").status_code == 200
    assert kinds_for(office.drafter) == ["approval_result"]
    assert office.final.id not in [row[1] for row in queued() if row[0] == "approval_result"]


def test_reject_notifies_drafter_and_withdraw_does_not() -> None:
    office = make_office()
    from tests.test_approvals import submitted as approval_submitted

    _, post = approval_submitted(office)
    browser(office.first).post(f"/approvals/{post}/reject", data={"comment": "다시"})
    assert kinds_for(office.drafter) == ["approval_result"]
    signed_in, other = approval_submitted(office)
    signed_in.post(f"/approvals/{other}/withdraw")
    assert kinds_for(office.drafter) == ["approval_result"]


# 워크플로우


def test_flow_turns_return_and_result() -> None:
    place = make_place()
    post = submitted(place)
    assert kinds_for(place.worker) == ["flow_turn"]
    process(place, post)
    assert kinds_for(place.reviewer) == ["flow_turn"]
    assert act(place.reviewer, post, "return", target=0, reason="다시").status_code == 200
    assert kinds_for(place.writer) == ["flow_returned"]
    finished = completed(place)
    assert ("flow_result", place.writer.id, None) in queued()
    assert all(row[1] != place.final.id for row in queued() if row[0] == "flow_result")
    assert finished


# 발송


def test_gone_device_is_removed_and_server_errors_are_retried(talk, service) -> None:
    channel, author, target = talk
    device_of(target)
    mention(channel, author)
    service.status = 410
    deliver()
    assert fetch(select(PushSubscription).where(PushSubscription.user_id == target.id)) == []
    assert queued()[-1][2] == "rejected"

    device_of(target, "https://fcm.googleapis.com/fcm/send/second")
    mention(channel, author, "@target 다시")
    service.status = 503
    deliver()
    (row,) = fetch(
        select(PushOutbox)
        .where(PushOutbox.sent_at.is_(None))
        .order_by(PushOutbox.id.desc())
        .limit(1)
    )
    assert row.dropped_reason is None and row.attempts == 1
    # 다시 보낼 때가 되면 성공한다.
    execute(
        update(PushOutbox).where(PushOutbox.id == row.id).values(next_attempt_at=row.created_at)
    )
    service.status = 201
    deliver()
    assert fetch(select(PushOutbox.sent_at).where(PushOutbox.id == row.id))[0] is not None


def test_user_without_device_is_skipped(talk, service) -> None:
    channel, author, target = talk
    mention(channel, author)
    deliver()
    assert queued() == [("mention", target.id, "no_device")]


def test_hub_wakes_the_worker_on_push_signal() -> None:
    hub = Hub()
    woke = []
    hub.on_push = lambda: woke.append(1)
    hub._on_notify(None, 0, "", json.dumps({"push": 1}))
    assert woke == [1]


# 기기 구독


def test_device_moves_to_whoever_signs_in_and_logout_forgets_it(talk) -> None:
    _, author, target = talk
    device = device_of(author, "https://fcm.googleapis.com/fcm/send/shared")
    device_of(target, device.endpoint)
    owners = fetch(select(PushSubscription.user_id))
    assert owners == [target.id]
    # 다른 사람의 구독은 로그아웃으로 지울 수 없다.
    other = browser(author)
    other.post("/logout", data={"push_endpoint": device.endpoint})
    assert fetch(select(PushSubscription.user_id)) == [target.id]
    mine = browser(target)
    mine.post("/logout", data={"push_endpoint": device.endpoint})
    assert fetch(select(PushSubscription.id)) == []


def test_devices_are_listed_and_removed_only_by_owner(talk) -> None:
    _, author, target = talk
    device_of(target)
    (device_id,) = fetch(select(PushSubscription.id))
    page = browser(target).get("/account/notifications").text
    assert f'data-device="{device_id}"' in page and "기기 · 브라우저" in page
    assert browser(author).post(f"/account/push/devices/{device_id}/delete").status_code == 200
    assert fetch(select(PushSubscription.id)) == [device_id]
    browser(target).post(f"/account/push/devices/{device_id}/delete")
    assert fetch(select(PushSubscription.id)) == []


def test_unsubscribe_and_bad_subscription(talk) -> None:
    _, _, target = talk
    device = device_of(target)
    signed_in = browser(target)
    bad = {**device.json(), "endpoint": "https://evil.example/x"}
    assert signed_in.post("/account/push/subscribe", json=bad).status_code == 400
    signed_in.post("/account/push/unsubscribe", json={"endpoint": device.endpoint})
    assert fetch(select(PushSubscription.id)) == []


@pytest.mark.parametrize(
    "module",
    ["app.cli", "app.seed", "app.services.flows", "app.services.notify", "app.routers.auth"],
)
def test_entry_modules_import_on_their_own(module: str) -> None:
    """app.main을 거치지 않고 불러도 순환 import가 없어야 한다(서버 관리 도구가 app.cli로 시작)."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr[-500:]
