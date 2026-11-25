"""워크플로우 채널: 정의와 발행, 등록, 단계 처리, 보완, 쓰레드, 결재 연계."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.models import (
    Approval,
    Attachment,
    AuditLog,
    Channel,
    FlowAssignee,
    FlowDefinition,
    FlowItem,
    FlowTask,
    GroupManager,
    PermissionGrant,
    Post,
    User,
    WorkspaceGroup,
)
from app.services import flow_rules as rules
from app.services.delivery import messages_for, todo_count
from app.services.permissions import Authorizer
from app.services.realtime import Event
from tests.conftest import (
    FULL,
    add_channel,
    add_department,
    add_group,
    add_user,
    browser,
    grant,
    join,
    run,
)


async def _add(*rows) -> None:
    async with SessionLocal() as session:
        session.add_all(rows)
        await session.commit()


def fetch(query) -> list:
    async def load() -> list:
        async with SessionLocal() as session:
            return list(await session.scalars(query))

    return run(load())


def execute(statement) -> None:
    async def go() -> None:
        async with SessionLocal() as session:
            await session.execute(statement)
            await session.commit()

    run(go())


def item_of(post: int) -> FlowItem:
    return fetch(select(FlowItem).where(FlowItem.post_id == post))[0]


def tasks_of(post: int) -> list[FlowTask]:
    item = item_of(post)
    return fetch(select(FlowTask).where(FlowTask.item_id == item.id).order_by(FlowTask.id))


def audit_actions() -> list[str]:
    rows = fetch(select(AuditLog).order_by(AuditLog.id))
    return [row.action for row in rows if row.action.startswith("flow.")]


def deactivate(user: User) -> None:
    execute(update(User).where(User.id == user.id).values(status="inactive"))


@dataclass
class Place:
    group: WorkspaceGroup
    channel: Channel
    manager: User
    writer: User
    worker: User
    reviewer: User
    final: User
    reader: User

    @property
    def settings(self) -> str:
        return f"/channels/{self.channel.id}/flow"


def save(place: Place, steps, action="publish", by: User | None = None):
    return browser(by or place.manager).post(
        f"{place.settings}/{action}", data={"data": json.dumps({"steps": steps})}
    )


def definition(place: Place) -> FlowDefinition:
    return fetch(
        select(FlowDefinition)
        .where(FlowDefinition.channel_id == place.channel.id, FlowDefinition.version.is_not(None))
        .order_by(FlowDefinition.version.desc())
    )[0]


def make_place(steps=None, publish=True) -> Place:
    manager = add_user("manager")
    group = add_group(manager)
    created = browser(manager).post(
        f"/groups/{group.id}/channels", data={"name": "홍보물 요청", "type": "flow"}
    )
    assert created.headers["HX-Redirect"].endswith("/flow")
    channel = fetch(select(Channel).where(Channel.type == "flow"))[0]
    writer, worker, reviewer, final, reader = (
        add_user(name) for name in ("writer", "worker", "reviewer", "final", "reader")
    )
    grant(channel, writer, **FULL)
    for user in (worker, reviewer, final, reader):
        grant(channel, user, read="allow", reply_read="allow")
    place = Place(group, channel, manager, writer, worker, reviewer, final, reader)
    if publish:
        response = save(place, steps or rules.default_steps())
        assert response.status_code == 200, response.text
    return place


@pytest.fixture
def place() -> Place:
    return make_place()


def step_of(place: Place, kind: str, nth: int = 0) -> dict:
    return [step for step in definition(place).steps if step["kind"] == kind][nth]


def register(place: Place, by: User | None = None, action="submit", signed_in=None, **data):
    current = definition(place)
    payload = {
        "definition_id": current.id,
        "title": "브로슈어 2안",
        "v_f1": "표지를 고쳐 주세요",
        "action": action,
    }
    people = {"process": place.worker, "review": place.reviewer, "confirm": place.final}
    for step in current.steps[1:]:
        if rules.is_picked(step):
            payload[f"p_{step['key']}"] = people[step["kind"]].id
    payload |= data
    files = payload.pop("files", None)
    payload = {key: value for key, value in payload.items() if value is not None}
    return (signed_in or browser(by or place.writer)).post(
        f"/channels/{place.channel.id}/flows", data=payload, files=files
    )


def submitted(place: Place, **data) -> int:
    response = register(place, **data)
    assert response.status_code == 200, response.text
    return int(response.headers["HX-Redirect"].rsplit("/", 1)[1])


def act(user: User, post: int, action="complete", signed_in=None, **data):
    item = item_of(post)
    payload = {"round": item.round, "step_no": item.current_step, "seen": 0} | data
    files = payload.pop("files", None)
    return (signed_in or browser(user)).post(f"/flows/{post}/{action}", data=payload, files=files)


def done(user: User, post: int, **data) -> None:
    response = act(user, post, **data)
    assert response.status_code == 200, response.text


def process(place: Place, post: int, text="작업함", **data) -> None:
    done(place.worker, post, v_f3=text, **data)


def review(place: Place, post: int, text="좋습니다") -> None:
    done(place.reviewer, post, v_f5=text)


def completed(place: Place) -> int:
    post = submitted(place)
    process(place, post)
    review(place, post)
    done(place.final, post, comment="확정합니다")
    assert item_of(post).status == "done"
    return post


def count(user: User) -> int:
    return run(todo_count(user.id))


def middle_steps() -> list[dict]:
    """등록 → 처리 → 중간 확정 → 검토 → 확정."""
    steps = rules.default_steps()
    steps.insert(
        2,
        {"kind": "confirm", "label": "1차 확정", "assign": "registrant", "complete": "all"},
    )
    return steps


# 정의 규칙


def clean(steps, published=None):
    return rules.clean_steps(steps, rules.Keys([published] if published else []), published)


def test_definition_needs_register_first_and_confirm_last() -> None:
    steps = rules.default_steps()
    for broken, message in (
        (steps[1:], "맨 처음"),
        (steps[:-1], "마지막 단계는 확정"),
        ([steps[0], steps[0], steps[-1]], "하나만"),
        ([steps[0]], "2~12개"),
        ("x", "2~12개"),
    ):
        with pytest.raises(rules.FlowError, match=message):
            clean(broken)


def test_middle_confirm_is_decided_by_one_person_and_has_no_fields() -> None:
    raw = middle_steps()
    raw[2]["fields"] = [{"kind": "text", "label": "몰래"}]
    steps, _ = clean(raw)
    assert steps[2]["complete"] == "any" and steps[2]["fields"] == []


def test_title_cannot_be_removed_or_made_optional() -> None:
    raw = rules.default_steps()
    raw[0]["fields"] = [{"key": "title", "kind": "text", "label": "x", "required": False}]
    steps, _ = clean(raw)
    assert steps[0]["fields"] == [
        {"key": "title", "kind": "text", "label": "제목", "required": True}
    ]
    raw[1]["fields"] = [{"key": "title", "kind": "text", "label": "가짜"}]
    assert clean(raw)[0][1]["fields"][0]["key"] != "title"


def test_changed_kind_or_assign_gets_new_key() -> None:
    old, _ = clean(rules.default_steps())
    raw = copy.deepcopy(old)
    raw[1]["assign"] = "fixed"
    raw[0]["fields"][1]["kind"] = "text"
    raw[2]["label"] = "이름만 바꿈"
    new, _ = clean(raw, old)
    assert new[1]["key"] != old[1]["key"]
    assert new[0]["fields"][1]["key"] != old[0]["fields"][1]["key"]
    assert new[2]["key"] == old[2]["key"]
    used = {step["key"] for step in old} | {f["key"] for s in old for f in s["fields"]}
    assert new[1]["key"] not in used


def field(kind: str, **extra) -> dict:
    return {"key": "f1", "kind": kind, "label": "항목", "required": False} | extra


@pytest.mark.parametrize(
    ("spec", "given", "expected"),
    [
        (field("text"), ["  a  b "], "a b"),
        (field("number", decimals=2, min="0", max="10"), ["1,0.5"], None),
        (field("number", decimals=1, min=None, max=None), ["1,234.5"], "1234.5"),
        (field("date"), ["2026-11-25"], "2026-11-25"),
        (field("select", options=["가", "나"]), ["나"], "나"),
        (field("multiselect", options=["가", "나"]), ["가", "나", "가"], ["가", "나"]),
        (field("check"), ["1"], True),
        (field("person", multiple=True), ["3", "4"], [3, 4]),
    ],
)
def test_values_are_parsed(spec, given, expected) -> None:
    data = rules.FormData({"f1": given})
    if expected is None:
        values, _ = rules.parse_values([spec], rules.FormData({"f1": ["10"]}), require=True)
        assert values == {"f1": "10"}
        return
    assert rules.parse_values([spec], data, require=True)[0] == {"f1": expected}


@pytest.mark.parametrize(
    ("spec", "given"),
    [
        (field("text"), ["x" * 201]),
        (field("number", decimals=0, min=None, max=None), ["1.5"]),
        (field("number", decimals=0, min="1", max="5"), ["6"]),
        (field("number", decimals=0, min=None, max=None), ["NaN"]),
        (field("number", decimals=0, min=None, max=None), ["1e99"]),
        (field("date"), ["2026-13-01"]),
        (field("select", options=["가"]), ["다"]),
        (field("multiselect", options=["가"]), ["가", "다"]),
        (field("person", multiple=False), ["1", "2"]),
        (field("person", multiple=False), ["1; DROP"]),
    ],
)
def test_wrong_values_are_rejected(spec, given) -> None:
    with pytest.raises(rules.FlowError):
        rules.parse_values([spec], rules.FormData({"f1": given}), require=False)


@pytest.mark.parametrize("kind", list(rules.FIELD_KINDS))
def test_required_is_checked_only_when_submitting(kind: str) -> None:
    spec = field(kind, options=["가"], decimals=0, required=True)
    assert rules.parse_values([spec], rules.FormData({}), require=False)[0] == {}
    with pytest.raises(rules.FlowError, match="필수"):
        rules.parse_values([spec], rules.FormData({}), require=True)
    spec["required"] = False
    assert rules.parse_values([spec], rules.FormData({}), require=True)[0] == {}


# 채널 만들기와 발행


def test_new_channel_opens_settings_and_is_closed_until_published() -> None:
    place = make_place(publish=False)
    page = browser(place.manager).get(place.settings)
    assert "발행 전" in page.text and '"key": "f3"' in page.text
    assert (
        "아직 준비 중인 채널입니다"
        in browser(place.writer).get(f"/channels/{place.channel.id}").text
    )
    assert browser(place.writer).get(f"/channels/{place.channel.id}/flows/new").status_code == 409
    assert save(place, rules.default_steps()).status_code == 200
    assert "새 등록" in browser(place.writer).get(f"/channels/{place.channel.id}").text
    assert "1판 사용 중" in browser(place.manager).get(place.settings).text
    assert "미리보기" in browser(place.manager).get(f"{place.settings}/preview").text


def test_only_group_manager_changes_settings(place: Place) -> None:
    for path in ("draft", "publish"):
        assert save(place, rules.default_steps(), path, by=place.writer).status_code == 403
    assert browser(place.writer).get(place.settings).status_code == 403
    assert browser(place.writer).get(f"{place.settings}/staff?q=").status_code == 403


def test_publish_needs_people_for_fixed_and_listed_steps() -> None:
    place = make_place(publish=False)
    steps = rules.default_steps()
    steps[1]["assign"] = "fixed"
    assert "담당자를 한 명 이상" in save(place, steps).json()["detail"]
    steps[1]["people"] = [place.worker.id]
    steps[3]["assign"] = "listed"
    assert "명단에 사람을 한 명 이상" in save(place, steps).json()["detail"]
    steps[3]["people"] = [place.final.id]
    assert save(place, steps).status_code == 200
    assert sorted(row.user_id for row in fetch(select(FlowAssignee))) == sorted(
        [place.worker.id, place.final.id]
    )


def test_publish_rejects_empty_options_and_unchanged_definition(place: Place) -> None:
    steps = copy.deepcopy(definition(place).steps)
    assert "바뀐 내용이 없습니다" in save(place, steps).json()["detail"]
    steps[1]["fields"].append({"kind": "select", "label": "구분", "options": []})
    assert "선택지를 적어" in save(place, steps).json()["detail"]


def test_saving_only_people_applies_without_new_version() -> None:
    steps = rules.default_steps()
    steps[1] |= {"assign": "fixed"}
    place = make_place(publish=False)
    steps[1]["people"] = [place.worker.id]
    assert save(place, steps).status_code == 200
    again = copy.deepcopy(definition(place).steps)
    again[1]["people"] = [place.reader.id]
    assert save(place, again, "draft").status_code == 200
    assert [row.user_id for row in fetch(select(FlowAssignee))] == [place.reader.id]
    rows = fetch(select(FlowDefinition))
    assert len(rows) == 1 and rows[0].version == 1
    assert "flow.assignees" in audit_actions()


def test_running_item_keeps_its_version_and_draft_moves_to_new_one(place: Place) -> None:
    running = submitted(place)
    draft = submitted(place, action="draft")
    steps = copy.deepcopy(definition(place).steps)
    # 검토 단계를 없애고, 내용 항목의 종류를 바꾼다.
    del steps[2]
    steps[0]["fields"][1] = {"kind": "text", "label": "한 줄 설명", "required": True}
    assert save(place, steps).status_code == 200

    process(place, running)
    review(place, running)
    done(place.final, running)
    assert item_of(running).status == "done"

    signed_in = browser(place.writer)
    page = signed_in.get(f"/flows/{draft}/edit").text
    assert "값은 사라집니다: 내용" in page and "한 줄 설명" in page
    new = definition(place)
    key = new.steps[0]["fields"][1]["key"]
    old_form = signed_in.post(
        f"/flows/{draft}/edit", data={"definition_id": new.id - 1, "title": "t"}
    )
    assert old_form.status_code == 409
    response = signed_in.post(
        f"/flows/{draft}/edit",
        data={
            "definition_id": new.id,
            "title": "t",
            f"v_{key}": "값",
            "action": "submit",
            f"p_{new.steps[1]['key']}": place.worker.id,
            f"p_{new.steps[2]['key']}": place.final.id,
        },
    )
    assert response.status_code == 200, response.text
    assert item_of(draft).definition_id == new.id


def test_removed_step_people_lose_channel_access_after_running_items_end() -> None:
    place = make_place(publish=False)
    outsider = add_user("outsider")
    steps = rules.default_steps()
    steps[2] |= {"assign": "fixed", "people": [outsider.id]}
    assert save(place, steps).status_code == 200
    post = submitted(place)
    again = copy.deepcopy(definition(place).steps)
    del again[2]
    assert save(place, again).status_code == 200
    # 진행 중인 건이 이 단계를 쓰므로 아직 담당자다.
    assert fetch(select(FlowAssignee.user_id)) == [outsider.id]
    assert "이전 판 단계" in browser(place.manager).get(place.settings).text
    process(place, post)
    done(outsider, post, v_f5="봤습니다")
    done(place.final, post)
    assert fetch(select(FlowAssignee.user_id)) == []
    assert browser(outsider).get(f"/channels/{place.channel.id}").status_code == 404
    assert browser(outsider).get(f"/flows/{post}").status_code == 200


# 등록


def test_register_needs_write_permission_and_required_values(place: Place) -> None:
    assert register(place, by=place.reader).status_code == 403
    assert "필수" in register(place, v_f1="").json()["detail"]
    assert "제목" in register(place, title=" ").json()["detail"]
    key = step_of(place, "review")["key"]
    assert "담당자를 지정" in register(place, **{f"p_{key}": None}).json()["detail"]
    assert fetch(select(FlowItem)) == []


def test_draft_is_private_and_can_be_deleted(place: Place) -> None:
    post = submitted(place, action="draft", v_f1="")
    assert item_of(post).status == "draft"
    assert browser(place.reader).get(f"/flows/{post}").status_code == 404
    assert browser(place.worker).get(f"/flows/{post}").status_code == 404
    assert "브로슈어" not in browser(place.reader).get(f"/channels/{place.channel.id}").text
    assert "브로슈어" in browser(place.writer).get(f"/channels/{place.channel.id}").text
    assert count(place.worker) == 0
    signed_in = browser(place.writer)
    assert signed_in.post(f"/posts/{post}/replies", data={"body": "x"}).status_code == 409
    assert browser(place.reader).post(f"/flows/{post}/delete").status_code == 404
    assert signed_in.post(f"/flows/{post}/delete").status_code == 200
    assert signed_in.get(f"/flows/{post}").status_code == 404


def test_submitted_item_cannot_be_edited_deleted_or_pinned(place: Place) -> None:
    post = submitted(place)
    signed_in = browser(place.writer)
    assert signed_in.post(f"/flows/{post}/delete").status_code == 409
    assert signed_in.post(f"/posts/{post}/delete").status_code == 403
    assert signed_in.post(f"/posts/{post}/edit", data={"body": "x"}).status_code == 403
    assert signed_in.get(f"/flows/{post}/edit").status_code == 409
    assert browser(place.manager).post(f"/posts/{post}/pin").status_code in (400, 404)
    assert signed_in.get(f"/posts/{post}").headers["location"] == f"/flows/{post}"


def test_picks_are_limited_to_channel_readers_or_list(place: Place) -> None:
    stranger = add_user("stranger")
    key = step_of(place, "confirm")["key"]
    assert "지정할 수 없는" in register(place, **{f"p_{key}": stranger.id}).json()["detail"]
    assert register(place, **{f"p_{key}": place.writer.id}).status_code == 400

    steps = copy.deepcopy(definition(place).steps)
    steps[3] |= {"assign": "listed", "people": [place.final.id]}
    assert save(place, steps).status_code == 200
    key = step_of(place, "confirm")["key"]
    assert register(place, **{f"p_{key}": place.reader.id}).status_code == 400
    assert register(place).status_code == 200
    found = browser(place.writer).get(f"/channels/{place.channel.id}/flow/people?step={key}&q=")
    assert [row["name"] for row in found.json()] == ["final"]


def test_people_search_hides_registrant_from_review_and_confirm(place: Place) -> None:
    signed_in = browser(place.writer)

    def names(kind: str) -> set[str]:
        key = step_of(place, kind)["key"]
        url = f"/channels/{place.channel.id}/flow/people?step={key}&q="
        return {row["name"] for row in signed_in.get(url).json()}

    assert "writer" in names("process")
    assert "writer" not in names("review") and "writer" not in names("confirm")
    assert (
        browser(place.reader).get(f"/channels/{place.channel.id}/flow/people?q=").status_code == 403
    )


def test_registrant_can_process_but_not_be_only_reviewer() -> None:
    place = make_place(publish=False)
    steps = rules.default_steps()
    steps[2] |= {"assign": "fixed", "people": [place.writer.id]}
    assert save(place, steps).status_code == 200
    assert "등록자뿐이라" in register(place).json()["detail"]
    steps = copy.deepcopy(definition(place).steps)
    steps[2]["people"] = [place.writer.id, place.reviewer.id]
    assert save(place, steps, "draft").status_code == 200
    key = step_of(place, "process")["key"]
    post = submitted(place, **{f"p_{key}": place.writer.id})
    done(place.writer, post, v_f3="직접 처리")
    waiting = [row.assignee_id for row in tasks_of(post) if row.status == "waiting"]
    assert waiting == [place.reviewer.id]


def test_processor_cannot_be_only_reviewer(place: Place) -> None:
    key = step_of(place, "review")["key"]
    response = register(place, **{f"p_{key}": place.worker.id})
    assert "처리한 사람은 검토할 수 없습니다" in response.json()["detail"]


def test_write_only_registrant_sees_only_own_items(place: Place) -> None:
    other = submitted(place)
    lone = add_user("lone")
    grant(place.channel, lone, write="allow")
    post = submitted(place, by=lone, title="내 요청")
    signed_in = browser(lone)
    page = signed_in.get(f"/channels/{place.channel.id}").text
    assert "내 요청" in page and "브로슈어" not in page
    assert signed_in.get(f"/flows/{other}").status_code == 404
    assert signed_in.get(f"/flows/{post}").status_code == 200
    # 쓰기만 있어도 자기 건의 쓰레드는 쓴다.
    reply = signed_in.post(f"/posts/{post}/replies", data={"body": "추가", "flow_note": "true"})
    assert reply.status_code == 200


# 진행


def test_full_run_moves_turn_and_counts(place: Place) -> None:
    post = submitted(place)
    assert count(place.worker) == 1 and count(place.reviewer) == 0
    assert "처리할 워크플로우" in browser(place.worker).get("/todo").text
    process(place, post)
    assert count(place.worker) == 0 and count(place.reviewer) == 1
    review(place, post)
    done(place.final, post, comment="확정합니다")
    item = item_of(post)
    assert item.status == "done" and item.closed_step == 3 and item.closed_at is not None
    assert count(place.writer) == 1
    todo = browser(place.writer).get("/todo").text
    assert "워크플로우 결과" in todo and "내가 등록한 워크플로우" in todo
    page = browser(place.reader).get(f"/flows/{post}").text
    for text in ("표지를 고쳐 주세요", "작업함", "좋습니다", "확정합니다", "완료"):
        assert text in page
    assert audit_actions() == [
        "flow.publish",
        "flow.submit",
        "flow.complete",
        "flow.complete",
        "flow.complete",
    ]


def test_notice_is_separate_from_approval_notice(place: Place) -> None:
    post = completed(place)
    signed_in = browser(place.writer)
    page = signed_in.get("/todo").text
    notice = int(page.split("/notifications/")[1].split("/")[0])
    assert signed_in.post(f"/notifications/{notice}/read").status_code == 200
    assert count(place.writer) == 0
    assert post


def test_only_current_assignee_acts(place: Place) -> None:
    post = submitted(place)
    for user in (place.writer, place.reader, place.reviewer, place.final):
        response = act(user, post, v_f3="x")
        assert response.status_code == 409, user.name
    # 검토 단계에서는 반려할 수 없다.
    process(place, post)
    assert act(place.reviewer, post, "reject", reason="x").status_code == 403


def test_step_required_fields_are_checked_on_complete(place: Place) -> None:
    post = submitted(place)
    assert "필수" in act(place.worker, post, v_f3=" ").json()["detail"]
    # 다른 단계의 항목이나 없는 키는 무시한다.
    process(place, post, v_f5="남의 항목", v_f999="없는 항목")
    (task,) = [row for row in tasks_of(post) if row.status == "done"]
    assert task.values == {"f3": "작업함"}


def test_stale_form_is_rejected(place: Place) -> None:
    post = submitted(place)
    process(place, post)
    response = browser(place.reviewer).post(
        f"/flows/{post}/complete", data={"round": 1, "step_no": 1, "seen": 0, "v_f3": "x"}
    )
    assert response.status_code == 409


def test_any_step_ends_with_first_person_and_all_waits_for_everyone(place: Place) -> None:
    process_key, review_key = step_of(place, "process")["key"], step_of(place, "review")["key"]
    post = submitted(
        place,
        **{
            f"p_{process_key}": [place.worker.id, place.reader.id],
            f"p_{review_key}": [place.reviewer.id, place.final.id],
        },
    )
    process(place, post)
    assert {row.assignee_id: row.status for row in tasks_of(post) if row.step_no == 1} == {
        place.worker.id: "done",
        place.reader.id: "skipped",
    }
    review(place, post)
    assert item_of(post).current_step == 2 and count(place.reviewer) == 0
    page = browser(place.reader).get(f"/flows/{post}").text
    assert "1/2 완료" in page and "좋습니다" in page
    done(place.final, post, v_f5="저도 좋습니다")
    assert item_of(post).current_step == 3


def test_fixed_assignee_change_applies_to_steps_not_started() -> None:
    place = make_place(publish=False)
    steps = rules.default_steps()
    steps[1] |= {"assign": "fixed", "people": [place.worker.id]}
    steps[2] |= {"assign": "fixed", "people": [place.reviewer.id]}
    assert save(place, steps).status_code == 200
    post = submitted(place)
    again = copy.deepcopy(definition(place).steps)
    again[1]["people"] = [place.reader.id]
    again[2]["people"] = [place.final.id]
    assert save(place, again, "draft").status_code == 200
    process(place, post)
    waiting = [row.assignee_id for row in tasks_of(post) if row.status == "waiting"]
    assert waiting == [place.final.id]


def test_files_are_stored_per_field_and_limited_to_viewers(place: Place) -> None:
    post = submitted(place, files=[("v_f2", ("시안.txt", b"hello"))])
    (file,) = fetch(select(Attachment))
    assert item_of(post).values["f2"] == [file.id]
    assert browser(place.reader).get(f"/attachments/{file.id}").content == b"hello"
    assert browser(add_user("stranger")).get(f"/attachments/{file.id}").status_code == 404
    assert browser(place.writer).post(f"/attachments/{file.id}/delete").status_code == 403
    process(place, post, files=[("v_f4", ("결과.txt", b"result"))])
    page = browser(place.reader).get(f"/flows/{post}").text
    assert "시안.txt" in page and "결과.txt" in page


def test_file_of_other_item_cannot_be_claimed(place: Place) -> None:
    first = submitted(place, action="draft", files=[("v_f2", ("비밀.txt", b"secret"))])
    (file,) = fetch(select(Attachment))
    post = submitted(place, action="draft")
    response = browser(place.writer).post(
        f"/flows/{post}/edit",
        data={
            "definition_id": definition(place).id,
            "title": "t",
            "k_f2": file.id,
            "action": "draft",
        },
    )
    assert response.status_code == 200 and "f2" not in item_of(post).values
    assert item_of(first).values["f2"] == [file.id]


def test_too_many_files_are_rejected(place: Place) -> None:
    files = [("v_f2", (f"{n}.txt", b"x")) for n in range(11)]
    assert register(place, files=files).status_code == 400
    assert fetch(select(Attachment)) == []


def test_draft_can_drop_file(place: Place) -> None:
    post = submitted(place, action="draft", files=[("v_f2", ("시안.txt", b"hello"))])
    response = browser(place.writer).post(
        f"/flows/{post}/edit",
        data={"definition_id": definition(place).id, "title": "t", "action": "draft"},
    )
    assert response.status_code == 200
    assert fetch(select(Attachment))[0].deleted_at is not None


# 중간 확정


def test_middle_confirm_can_continue() -> None:
    place = make_place(middle_steps())
    post = submitted(place)
    process(place, post)
    assert act(place.final, post).status_code == 400
    page = browser(place.final).get(f"/flows/{post}").text
    assert "확정하고 마무리" in page and "확정하고 다음 단계로" in page
    done(place.final, post, decision="continue")
    assert item_of(post).current_step == 3
    review(place, post)
    page = browser(place.final).get(f"/flows/{post}").text
    assert "확정하고 마무리" not in page
    # 마지막 확정은 선택 값을 보내도 그대로 완료다.
    done(place.final, post, decision="continue")
    assert item_of(post).status == "done" and item_of(post).closed_step == 4


def test_middle_confirm_can_finish_and_skips_rest() -> None:
    place = make_place(middle_steps())
    post = submitted(place)
    process(place, post)
    done(place.final, post, decision="finish")
    item = item_of(post)
    assert item.status == "done" and item.closed_step == 2
    assert count(place.reviewer) == 0 and count(place.writer) == 1
    page = browser(place.reader).get(f"/flows/{post}").text
    assert "1차 확정에서 마무리" in page and "생략" in page
    assert "완료 (1차 확정)" in browser(place.reader).get(f"/channels/{place.channel.id}").text
    # 검토하지 않은 사람은 결재를 올릴 수 없다.
    assert act(place.final, post, "cancel").status_code == 409


def test_concurrent_middle_decisions_keep_one() -> None:
    place = make_place(middle_steps())
    keys = [step["key"] for step in definition(place).steps]
    post = submitted(place, **{f"p_{keys[2]}": [place.final.id, place.reviewer.id]})
    process(place, post)
    browsers = [browser(place.final), browser(place.reviewer)]
    item = item_of(post)

    def decide(pair) -> int:
        signed_in, decision = pair
        data = {"round": item.round, "step_no": item.current_step, "seen": 0, "decision": decision}
        return signed_in.post(f"/flows/{post}/complete", data=data).status_code

    with ThreadPoolExecutor(2) as pool:
        codes = sorted(pool.map(decide, zip(browsers, ("finish", "continue"), strict=True)))
    assert codes == [200, 409]
    assert [row.status for row in tasks_of(post) if row.step_no == 2].count("done") == 1


def test_concurrent_complete_and_return_keep_one(place: Place) -> None:
    key = step_of(place, "review")["key"]
    post = submitted(place, **{f"p_{key}": [place.reviewer.id, place.final.id]})
    process(place, post)
    first, second = browser(place.reviewer), browser(place.final)
    base = {"round": 1, "step_no": 2, "seen": 0}

    def complete() -> int:
        data = {**base, "v_f5": "좋음"}
        return first.post(f"/flows/{post}/complete", data=data).status_code

    def back() -> int:
        data = {**base, "target": 1, "reason": "다시"}
        return second.post(f"/flows/{post}/return", data=data).status_code

    with ThreadPoolExecutor(2) as pool:
        codes = [job.result() for job in (pool.submit(complete), pool.submit(back))]
    item = item_of(post)
    # 보완 요청은 완료보다 늦어도 성립하지만, 보완 요청 뒤의 완료는 막힌다.
    assert codes[1] == 200 and codes[0] in (200, 409)
    assert (item.round, item.current_step) == (2, 1)


# 보완, 반려, 취소, 회수


def test_return_to_register_reruns_every_step(place: Place) -> None:
    post = submitted(place)
    process(place, post)
    assert act(place.reviewer, post, "return", target=0).status_code == 400
    response = act(place.reviewer, post, "return", target=0, reason="문구가 틀립니다")
    assert response.status_code == 200
    item = item_of(post)
    assert (item.status, item.round, item.current_step) == ("returned", 2, 0)
    assert (
        count(place.writer) == 1 and "보완할 워크플로우" in browser(place.writer).get("/todo").text
    )
    signed_in = browser(place.writer)
    assert "문구가 틀립니다" in signed_in.get(f"/flows/{post}/edit").text
    data = {"definition_id": item.definition_id, "title": "브로슈어 3안", "v_f1": "고친 내용"}
    for step in definition(place).steps[1:]:
        person = {"process": place.worker, "review": place.reviewer, "confirm": place.final}
        data[f"p_{step['key']}"] = person[step["kind"]].id
    assert signed_in.post(f"/flows/{post}/edit", data=data).status_code == 200
    assert item_of(post).status == "in_progress" and item_of(post).current_step == 1
    assert "작업함" in browser(place.worker).get(f"/flows/{post}").text
    process(place, post, "다시 작업함")
    page = browser(place.reader).get(f"/flows/{post}").text
    assert "고친 내용" in page and "1회차의 지난 내용" in page and "브로슈어 2안" in page
    review(place, post)
    done(place.final, post)
    assert item_of(post).status == "done"


def test_return_to_process_step_keeps_registration(place: Place) -> None:
    post = submitted(place)
    process(place, post, files=[("v_f4", ("초안.txt", b"1"))])
    review(place, post)
    # 확정 단계는 고를 수 없고, 없는 단계도 안 된다.
    assert act(place.final, post, "return", target=3, reason="x").status_code == 400
    response = act(place.final, post, "return", target=1, reason="결과를 다시")
    assert response.status_code == 200
    item = item_of(post)
    assert (item.status, item.round, item.current_step) == ("in_progress", 2, 1)
    assert count(place.worker) == 1 and count(place.writer) == 0
    page = browser(place.worker).get(f"/flows/{post}").text
    assert "초안.txt" in page and "작업함</textarea>" in page
    (file,) = fetch(select(Attachment))
    process(place, post, "고침", k_f4=file.id)
    newest = [row for row in tasks_of(post) if row.step_no == 1 and row.status == "done"][-1]
    assert newest.values == {"f3": "고침", "f4": [file.id]}
    # 예전 검토는 유효하지 않다. 다시 검토해야 한다.
    assert item_of(post).current_step == 2 and count(place.reviewer) == 1


def test_processor_still_cannot_review_after_return_to_review(place: Place) -> None:
    key = step_of(place, "review")["key"]
    process_key = step_of(place, "process")["key"]
    post = submitted(
        place,
        **{
            f"p_{process_key}": [place.worker.id, place.reader.id],
            f"p_{key}": [place.worker.id, place.reviewer.id],
        },
    )
    process(place, post)
    review(place, post)
    assert act(place.final, post, "return", target=2, reason="다시").status_code == 200
    waiting = [row.assignee_id for row in tasks_of(post) if row.status == "waiting"]
    assert waiting == [place.reviewer.id]


def test_reject_needs_reason_and_ends(place: Place) -> None:
    post = submitted(place)
    process(place, post)
    review(place, post)
    assert act(place.final, post, "reject", reason=" ").status_code == 400
    assert act(place.final, post, "reject", reason="예산 없음").status_code == 200
    assert item_of(post).status == "rejected" and count(place.writer) == 1
    page = browser(place.writer).get(f"/flows/{post}").text
    assert "예산 없음" in page and "새로 등록" in page
    assert act(place.final, post).status_code == 409


def test_copy_makes_independent_draft(place: Place) -> None:
    post = submitted(place, files=[("v_f2", ("시안.txt", b"hello"))])
    signed_in = browser(place.writer)
    assert signed_in.post(f"/flows/{post}/copy").status_code == 409
    assert signed_in.post(f"/flows/{post}/withdraw").status_code == 200
    response = signed_in.post(f"/flows/{post}/copy")
    copied = int(response.headers["HX-Redirect"].split("/")[2])
    item = item_of(copied)
    assert item.status == "draft" and item.source_post_id == post
    assert item.values["f1"] == "표지를 고쳐 주세요"
    assert item.values["f2"] != item_of(post).values["f2"]
    assert "원래 건" in signed_in.get(f"/flows/{copied}").text
    assert browser(place.reader).post(f"/flows/{post}/copy").status_code == 403


def test_cancel_returns_turn_until_next_person_acts(place: Place) -> None:
    post = submitted(place)
    process(place, post)
    assert act(place.reviewer, post, "cancel").status_code == 409
    assert act(place.worker, post, "cancel").status_code == 200
    assert item_of(post).current_step == 1
    assert count(place.worker) == 1 and count(place.reviewer) == 0
    process(place, post, "다시")
    review(place, post)
    assert act(place.worker, post, "cancel").status_code == 409
    done(place.final, post)
    assert act(place.final, post, "cancel").status_code == 409


def test_cancel_gives_turn_back_to_skipped_people(place: Place) -> None:
    key = step_of(place, "process")["key"]
    post = submitted(place, **{f"p_{key}": [place.worker.id, place.reader.id]})
    process(place, post)
    assert count(place.reader) == 0
    assert act(place.worker, post, "cancel").status_code == 200
    assert count(place.reader) == 1


def test_cancel_after_return_is_not_possible(place: Place) -> None:
    post = submitted(place)
    process(place, post)
    review(place, post)
    assert act(place.final, post, "return", target=2, reason="다시").status_code == 200
    assert act(place.worker, post, "cancel").status_code == 409


def test_concurrent_cancel_and_next_complete_keep_one(place: Place) -> None:
    post = submitted(place)
    process(place, post)
    worker, reviewer = browser(place.worker), browser(place.reviewer)
    data = {"round": 1, "step_no": 2, "seen": 0, "v_f5": "좋음"}
    with ThreadPoolExecutor(2) as pool:
        jobs = [
            pool.submit(lambda: worker.post(f"/flows/{post}/cancel").status_code),
            pool.submit(lambda: reviewer.post(f"/flows/{post}/complete", data=data).status_code),
        ]
    assert sorted(job.result() for job in jobs) == [200, 409]


def test_withdraw_only_before_anyone_handles(place: Place) -> None:
    post = submitted(place)
    assert browser(place.reader).post(f"/flows/{post}/withdraw").status_code == 409
    process(place, post)
    signed_in = browser(place.writer)
    assert signed_in.post(f"/flows/{post}/withdraw").status_code == 409
    act(place.reviewer, post, "return", target=0, reason="다시")
    assert signed_in.post(f"/flows/{post}/withdraw").status_code == 200
    assert item_of(post).status == "withdrawn" and count(place.writer) == 0
    other = submitted(place)
    assert signed_in.post(f"/flows/{other}/withdraw").status_code == 200
    assert count(place.worker) == 0


# 관리자


def test_stalled_item_is_shown_to_manager_and_reassigned(place: Place) -> None:
    post = submitted(place)
    deactivate(place.worker)
    assert count(place.manager) == 1
    assert "멈춘 워크플로우" in browser(place.manager).get("/todo").text
    # 관리자는 읽기 권한이 없으면 본문을 볼 수 없다.
    assert browser(place.manager).get(f"/flows/{post}").status_code == 404
    signed_in = browser(place.manager)
    people = signed_in.get(f"/flows/{post}/reassign-people?q=").json()
    assert "reader" in {row["name"] for row in people}
    data = {"old_id": place.worker.id, "new_id": place.reader.id}
    assert signed_in.post(f"/flows/{post}/reassign", data=data).status_code == 400
    response = signed_in.post(f"/flows/{post}/reassign", data={**data, "reason": "퇴사"})
    assert response.status_code == 200, response.text
    assert count(place.manager) == 0 and count(place.reader) == 1
    done(place.reader, post, v_f3="대신 처리")
    # 되돌아와도 새 담당자가 맡는다.
    act(place.reviewer, post, "return", target=1, reason="다시")
    waiting = [row.assignee_id for row in tasks_of(post) if row.status == "waiting"]
    assert waiting == [place.reader.id]


def test_reassign_is_limited_to_manager_and_allowed_people(place: Place) -> None:
    post = submitted(place)
    stranger = add_user("stranger")
    data = {"old_id": place.worker.id, "new_id": stranger.id, "reason": "x"}
    assert browser(place.manager).post(f"/flows/{post}/reassign", data=data).status_code == 400
    data["new_id"] = place.reader.id
    assert browser(place.writer).post(f"/flows/{post}/reassign", data=data).status_code == 403
    process(place, post)
    # 처리한 사람과 등록자는 검토를 맡을 수 없다.
    for user in (place.worker, place.writer):
        data = {"old_id": place.reviewer.id, "new_id": user.id, "reason": "x"}
        response = browser(place.manager).post(f"/flows/{post}/reassign", data=data)
        assert response.status_code == 400


def test_discard_only_when_stalled(place: Place) -> None:
    post = submitted(place)
    signed_in = browser(place.manager)
    assert signed_in.post(f"/flows/{post}/discard", data={"reason": "x"}).status_code == 409
    deactivate(place.worker)
    assert signed_in.post(f"/flows/{post}/discard", data={"reason": "담당 없음"}).status_code == 200
    assert item_of(post).status == "discarded" and count(place.writer) == 1


def test_step_without_people_stops_until_manager_adds_someone() -> None:
    place = make_place(publish=False)
    steps = rules.default_steps()
    steps[2] |= {"assign": "fixed", "people": [place.reviewer.id]}
    assert save(place, steps).status_code == 200
    post = submitted(place)
    again = copy.deepcopy(definition(place).steps)
    again[2]["people"] = [place.worker.id]
    assert save(place, again, "draft").status_code == 200
    process(place, post)
    assert [row for row in tasks_of(post) if row.status == "waiting"] == []
    assert count(place.manager) == 1
    again[2]["people"] = [place.worker.id, place.final.id]
    assert save(place, again, "draft").status_code == 200
    data = {"old_id": "", "new_id": place.final.id, "reason": "담당자 추가"}
    assert browser(place.manager).post(f"/flows/{post}/reassign", data=data).status_code == 200
    done(place.final, post, v_f5="검토함")
    assert item_of(post).current_step == 3


# 권한


def test_fixed_assignee_gets_channel_read_and_reply_but_not_write() -> None:
    place = make_place(publish=False)
    outsider = add_user("outsider")
    team = add_department("팀")
    join(outsider, team)
    grant(place.channel, outsider, read="deny", reply_read="deny")
    steps = rules.default_steps()
    steps[1] |= {"assign": "fixed", "people": [outsider.id]}
    assert save(place, steps).status_code == 200

    async def access():
        async with SessionLocal() as session:
            channel = await session.get(Channel, place.channel.id)
            user = await session.get(User, outsider.id)
            return await Authorizer(session, user).access(channel)

    found = run(access())
    assert found.read and found.reply_read and found.reply_write and not found.write
    assert register(place, by=outsider).status_code == 403


def test_picked_assignee_keeps_item_after_losing_channel(place: Place) -> None:
    post = submitted(place)
    other = submitted(
        place, title="다른 건", **{f"p_{step_of(place, 'process')['key']}": place.reader.id}
    )
    execute(PermissionGrant.__table__.delete().where(PermissionGrant.subject_id == place.worker.id))
    signed_in = browser(place.worker)
    assert signed_in.get(f"/channels/{place.channel.id}").status_code == 404
    assert signed_in.get(f"/flows/{post}").status_code == 200
    assert signed_in.get(f"/flows/{other}").status_code == 404
    process(place, post)
    reply = signed_in.post(f"/posts/{post}/replies", data={"body": "추가", "flow_note": "true"})
    assert reply.status_code == 200
    # 글 단위 권한만 있어도 쓰레드에서 멘션하고 확인한다.
    mention = browser(place.writer).post(f"/posts/{post}/replies", data={"body": "@worker 확인"})
    assert mention.status_code == 200, mention.text
    assert count(place.worker) == 1
    assert "@worker" in signed_in.get("/todo").text
    candidates = (
        browser(place.writer)
        .get(f"/channels/{place.channel.id}/mention-candidates?post={post}&q=worker")
        .json()
    )
    assert candidates[0]["disabled"] is False


def test_outsider_is_blocked_everywhere(place: Place) -> None:
    post = submitted(place)
    stranger = browser(add_user("stranger"))
    for path in ("", "/edit", "/people"):
        assert stranger.get(f"/flows/{post}{path}").status_code == 404
    data = {"round": 1, "step_no": 1, "seen": 0, "target": 0, "reason": "x"}
    for action in ("complete", "return", "reject", "cancel", "withdraw", "copy", "delete"):
        assert stranger.post(f"/flows/{post}/{action}", data=data).status_code == 404, action
    assert stranger.get(f"/channels/{place.channel.id}/flows").status_code == 404


def test_archived_group_closes_items(place: Place) -> None:
    from datetime import UTC, datetime

    post = submitted(place)
    execute(
        update(WorkspaceGroup)
        .where(WorkspaceGroup.id == place.group.id)
        .values(archived_at=datetime.now(UTC))
    )
    assert browser(place.worker).get(f"/flows/{post}").status_code == 404
    assert count(place.worker) == 0


# 쓰레드


def note(user: User, post: int, body="자료를 더합니다", **data):
    return browser(user).post(
        f"/posts/{post}/replies", data={"body": body, "flow_note": "true", **data}
    )


def test_note_is_for_participants_and_locked(place: Place) -> None:
    post = submitted(place)
    grant_reply = place.reader
    execute(
        update(PermissionGrant)
        .where(PermissionGrant.subject_id == grant_reply.id)
        .values(can_reply_write="allow")
    )
    assert note(grant_reply, post).status_code == 403
    assert (
        browser(grant_reply).post(f"/posts/{post}/replies", data={"body": "일반"}).status_code
        == 200
    )
    # 차례가 오지 않은 확정자도 남긴다.
    response = note(place.final, post)
    assert response.status_code == 200 and "처리 단계 · 1회차 · final" in response.text
    reply = fetch(select(Post).where(Post.body_md == "자료를 더합니다"))[0]
    signed_in = browser(place.final)
    assert signed_in.post(f"/posts/{reply.id}/edit", data={"body": "x"}).status_code == 403
    assert signed_in.post(f"/posts/{reply.id}/delete").status_code == 403
    page = browser(place.reader).get(f"/flows/{post}").text
    assert "추가 내용" in page and "자료를 더합니다" in page
    assert "추가 내용 1" in browser(place.reader).get(f"/channels/{place.channel.id}").text
    assert "flow.note" in audit_actions()


def test_note_file_cannot_be_deleted(place: Place) -> None:
    post = submitted(place)
    response = browser(place.writer).post(
        f"/posts/{post}/replies",
        data={"body": "파일", "flow_note": "true"},
        files=[("files", ("추가.txt", b"x"))],
    )
    assert response.status_code == 200
    (file,) = fetch(select(Attachment))
    assert browser(place.writer).post(f"/attachments/{file.id}/delete").status_code == 403


def test_unseen_note_stops_complete_once(place: Place) -> None:
    post = submitted(place)
    assert note(place.writer, post).status_code == 200
    blocked = act(place.worker, post, v_f3="작업함")
    assert blocked.status_code == 409 and "새 추가 내용" in blocked.json()["detail"]
    seen = json.loads(blocked.headers["HX-Trigger"])["flow-seen"]["seen"]
    assert act(place.worker, post, v_f3="작업함", seen=seen).status_code == 200
    # 화면을 새로 열면 그때까지의 추가 내용은 본 것이다.
    assert f'name="seen" value="{seen}"' in browser(place.reviewer).get(f"/flows/{post}").text


def test_own_note_does_not_stop_complete(place: Place) -> None:
    post = submitted(place)
    assert note(place.worker, post).status_code == 200
    assert act(place.worker, post, v_f3="작업함").status_code == 200


def test_note_after_completion_is_marked(place: Place) -> None:
    post = completed(place)
    response = note(place.writer, post, "나중에 더함")
    assert "완료 후 추가" in response.text
    assert item_of(post).status == "done"


def test_flow_note_is_ignored_outside_flow_channels() -> None:
    owner = add_user("owner")
    channel = add_channel(add_group(owner))
    grant(channel, owner, **FULL)
    signed_in = browser(owner)
    signed_in.post(f"/channels/{channel.id}/posts", data={"body": "글"})
    response = signed_in.post("/posts/1/replies", data={"body": "답", "flow_note": "true"})
    assert response.status_code == 403


# 결재 연계


def add_expense(place: Place, *writers: User) -> Channel:
    channel = add_channel(place.group, "지출결의", "approval", approval_template_md="## 지출 내용")
    for user in writers:
        grant(channel, user, **FULL)
    response = browser(place.manager).post(
        f"{place.settings}/approval-targets", data={"target_channel_id": channel.id}
    )
    assert response.status_code == 200
    return channel


def raise_approval(user: User, channel: Channel, post: int, signed_in=None, **data):
    return (signed_in or browser(user)).post(
        f"/channels/{channel.id}/approvals",
        data={"title": "인쇄비", "body": "본문", "flow_post_id": post, **data},
    )


def test_only_people_who_handled_can_raise_approval(place: Place) -> None:
    everyone = (place.writer, place.worker, place.reviewer, place.final, place.reader)
    channel = add_expense(place, *everyone)
    running = submitted(place)
    assert raise_approval(place.writer, channel, running).status_code == 403
    post = completed(place)
    assert raise_approval(place.reader, channel, post).status_code == 403
    for user in (place.writer, place.worker, place.reviewer, place.final):
        page = browser(user).get(f"/flows/{post}").text
        assert "결재 올리기" in page and f"/channels/{channel.id}/approvals/new?flow={post}" in page
    assert "결재 올리기" not in browser(place.reader).get(f"/flows/{post}").text
    assert raise_approval(place.reviewer, channel, post).status_code == 200


def test_skipped_assignee_and_old_round_cannot_raise(place: Place) -> None:
    key = step_of(place, "process")["key"]
    channel = add_expense(place, place.worker, place.reader)
    post = submitted(place, **{f"p_{key}": [place.worker.id, place.reader.id]})
    process(place, post)
    act(place.reviewer, post, "return", target=1, reason="다시")
    done(place.reader, post, v_f3="제가 다시 했습니다")
    review(place, post)
    done(place.final, post)
    assert raise_approval(place.worker, channel, post).status_code == 403
    assert raise_approval(place.reader, channel, post).status_code == 200


def test_approval_form_is_filled_with_copy(place: Place) -> None:
    channel = add_expense(place, place.writer)
    post = submitted(place, files=[("v_f2", ("시안.txt", b"hello"))])
    process(place, post)
    note(place.writer, post, "추가로 더한 설명")
    browser(place.writer).post(f"/posts/{post}/replies", data={"body": "일반 답글입니다"})
    act(place.reviewer, post, v_f5="좋습니다", seen=10**8)
    done(place.final, post, comment="확정 의견", seen=10**8)
    signed_in = browser(place.writer)
    page = signed_in.get(f"/channels/{channel.id}/approvals/new?flow={post}").text
    for text in (
        "## 지출 내용",
        "표지를 고쳐 주세요",
        "작업함",
        "좋습니다",
        "확정 의견",
        "추가로 더한 설명",
        f"/flows/{post}",
        "시안.txt",
    ):
        assert text in page, text
    assert "일반 답글입니다" not in page
    (file,) = fetch(select(Attachment))
    response = raise_approval(place.writer, channel, post, flow_files=[file.id])
    approval_post = int(response.headers["HX-Redirect"].rsplit("/", 1)[1])
    copies = fetch(select(Attachment).where(Attachment.post_id == approval_post))
    assert len(copies) == 1 and copies[0].storage_key != file.storage_key
    assert f"/flows/{post}" in signed_in.get(f"/approvals/{approval_post}").text
    assert "결재 준비 중" in signed_in.get(f"/flows/{post}").text
    assert "flow.approval" in audit_actions()


def test_one_live_approval_at_a_time(place: Place) -> None:
    channel = add_expense(place, place.writer, place.final)
    post = completed(place)
    browsers = [browser(place.writer), browser(place.final)]
    with ThreadPoolExecutor(2) as pool:
        codes = list(
            pool.map(lambda one: raise_approval(None, channel, post, one).status_code, browsers)
        )
    assert sorted(codes) == [200, 409]
    approval = fetch(select(Approval))[0]
    owner = browsers[codes.index(200)]
    assert owner.post(f"/posts/{approval.post_id}/delete").status_code == 200
    assert raise_approval(place.writer, channel, post).status_code == 200


def test_approval_result_is_shown_and_rejected_one_can_be_raised_again(place: Place) -> None:
    channel = add_expense(place, place.writer)
    post = completed(place)
    signed_in = browser(place.writer)
    first = raise_approval(place.writer, channel, post)
    approval_post = int(first.headers["HX-Redirect"].rsplit("/", 1)[1])
    assert signed_in.post(f"/approvals/{approval_post}/submit").status_code == 200
    assert "지출결의: 결재 중" in signed_in.get(f"/flows/{post}").text
    rejected = signed_in.post(f"/approvals/{approval_post}/reject", data={"comment": "다시"})
    assert rejected.status_code == 200
    page = signed_in.get(f"/flows/{post}").text
    assert "지출결의: 반려" in page and "결재 올리기" in page
    # 결재가 반려되어도 건은 완료다.
    assert item_of(post).status == "done"


def test_approval_target_must_be_set_and_writable(place: Place) -> None:
    channel = add_expense(place, place.writer)
    other = add_channel(place.group, "품의", "approval")
    grant(other, place.writer, **FULL)
    post = completed(place)
    assert raise_approval(place.writer, other, post).status_code == 403
    assert raise_approval(place.final, channel, post).status_code in (403, 404)
    talk = add_channel(place.group, "잡담")
    response = browser(place.manager).post(
        f"{place.settings}/approval-targets", data={"target_channel_id": talk.id}
    )
    assert response.status_code == 400
    browser(place.manager).post(
        f"{place.settings}/approval-targets",
        data={"target_channel_id": channel.id, "remove": "true"},
    )
    assert "결재 올리기" not in browser(place.writer).get(f"/flows/{post}").text


def test_finished_by_middle_confirm_can_raise() -> None:
    place = make_place(middle_steps())
    channel = add_expense(place, place.writer, place.reviewer)
    post = submitted(place)
    process(place, post)
    done(place.final, post, decision="finish")
    # 생략된 검토 담당자는 올릴 수 없다.
    assert raise_approval(place.reviewer, channel, post).status_code == 403
    assert raise_approval(place.writer, channel, post).status_code == 200


def test_long_copy_is_shortened(place: Place) -> None:
    steps = copy.deepcopy(definition(place).steps)
    steps[0]["fields"] += [
        {"kind": "markdown", "label": f"설명{n}", "required": False} for n in range(6)
    ]
    assert save(place, steps).status_code == 200
    channel = add_expense(place, place.writer)
    keys = [row["key"] for row in definition(place).steps[0]["fields"][3:]]
    post = submitted(place, **{f"v_{key}": "가" * 10_000 for key in keys})
    process(place, post)
    review(place, post)
    done(place.final, post)
    page = browser(place.writer).get(f"/channels/{channel.id}/approvals/new?flow={post}")
    assert page.status_code == 200 and "(원본 참고)" in page.text


# 화면과 실시간


def test_values_are_escaped(place: Place) -> None:
    bad = "<img src=x onerror=alert(1)>"
    post = submitted(place, title=bad, v_f1=bad)
    for url in (f"/flows/{post}", f"/channels/{place.channel.id}"):
        assert "<img src=x" not in browser(place.reader).get(url).text
    steps = copy.deepcopy(definition(place).steps)
    steps[1]["label"] = "</script><script>alert(1)</script>"
    assert save(place, steps, "draft").status_code == 200
    assert "</script><script>alert" not in browser(place.manager).get(place.settings).text
    assert "<script>alert" not in browser(place.manager).get(f"{place.settings}/preview").text


def test_list_tabs_filter(place: Place) -> None:
    running = submitted(place, title="진행건")
    finished = completed(place)
    execute(update(Post).where(Post.id == finished).values(title="끝난건"))

    def rows(user: User, tab: str) -> str:
        return browser(user).get(f"/channels/{place.channel.id}/flows?tab={tab}").text

    assert "진행건" in rows(place.reader, "open") and "끝난건" not in rows(place.reader, "open")
    assert "끝난건" in rows(place.reader, "closed")
    assert "진행건" in rows(place.worker, "mine") and "진행건" not in rows(place.reader, "mine")
    assert "진행건" in rows(place.writer, "registered")
    assert "진행건" in rows(place.reader, "no-such-tab")
    assert running


def test_search_finds_title_and_step_values(place: Place) -> None:
    post = submitted(place)
    process(place, post, "특수지로 인쇄")
    signed_in = browser(place.reader)
    assert f"/posts/{post}" in signed_in.get("/search?q=브로슈어").text
    assert f"/posts/{post}" in signed_in.get("/search?q=특수지").text


def test_events_send_row_and_card(place: Place) -> None:
    post = submitted(place)

    def names(event: Event, user: User) -> list[str]:
        return [name for name, _ in run(messages_for(event, user.id))]

    created = names(Event("created", place.channel.id, post, place.writer.id), place.reader)
    assert created == [f"ch-{place.channel.id}"]
    updated = names(Event("updated", place.channel.id, post, place.writer.id), place.reader)
    assert f"row-{post}" in updated and f"post-{post}" in updated
    note(place.writer, post)
    reply = fetch(select(Post).where(Post.parent_post_id == post))[0]
    replied = names(Event("created", place.channel.id, reply.id, place.writer.id), place.reader)
    assert f"thread-{post}" in replied and f"row-{post}" in replied


def test_unread_count_does_not_include_flow(place: Place) -> None:
    submitted(place)
    assert 'data-unread="' in browser(place.reader).get("/").text
    page = browser(place.reader).get("/").text
    assert f'data-unread="{place.channel.id}" hidden' in page


def test_audit_page_shows_flow_actions(place: Place) -> None:
    completed(place)
    page = browser(add_user("admin")).get("/admin/audit")
    assert page.status_code == 200
    assert "단계 완료" in page.text and "워크플로우 발행" in page.text


def test_manager_of_other_group_cannot_touch(place: Place) -> None:
    other = add_user("other")
    group = add_group(other, "다른 그룹")
    assert fetch(select(GroupManager).where(GroupManager.group_id == group.id))
    post = submitted(place)
    assert save(place, rules.default_steps(), by=other).status_code == 403
    data = {"old_id": place.worker.id, "new_id": place.reader.id, "reason": "x"}
    assert browser(other).post(f"/flows/{post}/reassign", data=data).status_code == 403


# 사용 중지


def test_disabled_type_closes_channels_and_keeps_data(place: Place, monkeypatch) -> None:
    channel = add_expense(place, place.writer)
    finished = completed(place)
    raised = raise_approval(place.writer, channel, finished)
    approval = raised.headers["HX-Redirect"]
    post = submitted(place)
    note(place.writer, post, "중지 전에 남긴 내용")
    assert count(place.worker) == 1
    monkeypatch.setattr(get_settings(), "disabled_channel_types", frozenset({"flow"}))

    for user in (place.writer, place.worker, place.final, place.reader):
        signed_in = browser(user)
        assert signed_in.get(f"/channels/{place.channel.id}").status_code == 404
        assert signed_in.get(f"/flows/{post}").status_code == 404
        assert "홍보물 요청" not in signed_in.get("/").text
        todo = signed_in.get("/todo")
        assert todo.status_code == 200 and "워크플로우" not in todo.text
        assert count(user) == 0
        assert "중지 전에" not in signed_in.get("/search?q=중지").text
    assert act(place.worker, post, v_f3="x").status_code == 404
    assert register(place).status_code == 404
    assert run(messages_for(Event("updated", place.channel.id, post, 0), place.reader.id)) is None

    manager = browser(place.manager)
    page = manager.get(f"/groups/{place.group.id}/settings").text
    assert "홍보물 요청" not in page and 'value="flow"' not in page
    created = manager.post(
        f"/groups/{place.group.id}/channels", data={"name": "새것", "type": "flow"}
    )
    assert created.status_code == 400
    assert manager.get(place.settings).status_code == 404
    assert manager.get(f"/channels/{place.channel.id}/settings").status_code == 404
    data = {"old_id": place.worker.id, "new_id": place.reader.id, "reason": "x"}
    assert manager.post(f"/flows/{post}/reassign", data=data).status_code == 404

    # 이미 올린 결재는 그대로 진행한다. 열리지 않는 원본 링크만 숨긴다.
    writer = browser(place.writer)
    assert "원본 워크플로우: " not in writer.get(approval).text
    assert writer.post(f"{approval}/submit").status_code == 200

    # 다시 켜면 하던 데서 이어진다.
    monkeypatch.setattr(get_settings(), "disabled_channel_types", frozenset())
    assert count(place.worker) == 1
    done(place.worker, post, v_f3="이어서", seen=10**8)
    assert item_of(post).current_step == 2


def test_work_is_disabled_by_default_and_setting_is_checked(monkeypatch) -> None:
    monkeypatch.delenv("BARO_DISABLED_CHANNEL_TYPES")
    assert Settings(_env_file=None).disabled_channel_types == {"work"}
    monkeypatch.setenv("BARO_DISABLED_CHANNEL_TYPES", "")
    assert Settings(_env_file=None).disabled_channel_types == frozenset()
    monkeypatch.setenv("BARO_DISABLED_CHANNEL_TYPES", "talk")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_other_channels_stay_open_when_flow_is_disabled(monkeypatch) -> None:
    owner = add_user("owner")
    channel = add_channel(add_group(owner), "할 일", "work")
    grant(channel, owner, **FULL)
    monkeypatch.setattr(get_settings(), "disabled_channel_types", frozenset({"flow"}))
    assert browser(owner).get(f"/channels/{channel.id}").status_code == 200
    monkeypatch.setattr(get_settings(), "disabled_channel_types", frozenset({"work"}))
    assert browser(owner).get(f"/channels/{channel.id}").status_code == 404
