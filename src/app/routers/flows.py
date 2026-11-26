"""워크플로우 채널. 정의 설정, 등록, 단계 처리, 결재 연계 설정."""

import json
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData as RawForm
from starlette.datastructures import UploadFile

from app.core.config import get_settings
from app.core.db import SessionDep
from app.core.deps import AuthzDep, CurrentUser, current_user
from app.core.templating import fragment, templates
from app.models import (
    Channel,
    FlowApprovalTarget,
    FlowDefinition,
    FlowItem,
    Post,
    User,
    WorkspaceGroup,
)
from app.routers.posts import (
    channel_access,
    check_upload_size,
    not_found,
    post_context,
    store_attachments,
)
from app.routers.spaces import REFRESH, managed_channel
from app.services import flows as engine
from app.services.approvals import people_rows, search_people
from app.services.audit import record_audit
from app.services.dispatch import copy_files
from app.services.flow_rules import (
    ASSIGN_KINDS,
    COMPLETE_KINDS,
    FIELD_KINDS,
    MAX_FIELDS,
    MAX_OPTIONS,
    MAX_PEOPLE,
    MAX_STEPS,
    ROLE_LABELS,
    STEP_KINDS,
    FlowError,
    FormData,
    carry_values,
    default_steps,
    dropped_labels,
    has_list,
    is_picked,
    merge_values,
    parse_values,
    relaxed,
)
from app.services.flow_views import (
    TABS,
    action_view,
    field_inputs,
    flow_list,
    load_files,
    locked_sections,
    open_items,
)
from app.services.flows import FlowDoc, load_doc
from app.services.navigation import build_navigation
from app.services.permissions import Authorizer, ChannelAccess, can_view_replies
from app.services.post_views import build_views
from app.services.realtime import Event, publish, publish_counts

router = APIRouter(tags=["flows"], include_in_schema=False, dependencies=[Depends(current_user)])

Search = Annotated[str, Query(max_length=100)]
Reason = Annotated[str, Form(max_length=1000)]
MAX_SETTINGS = 500_000


def failed(error: FlowError) -> HTTPException:
    code = status.HTTP_409_CONFLICT if error.stale else status.HTTP_400_BAD_REQUEST
    return HTTPException(code, str(error))


def conflict(message: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, message)


def require_reason(reason: str) -> str:
    if not reason.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "사유를 입력해 주세요.")
    return reason.strip()


# 설정


async def managed_flow(session: AsyncSession, channel_id: int, user: User) -> Channel:
    channel = await managed_channel(session, channel_id, user)
    if channel.type != "flow":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "워크플로우 채널이 아닙니다.")
    return channel


def parse_settings(data: str) -> object:
    if len(data) > MAX_SETTINGS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "설정이 너무 큽니다.")
    try:
        parsed = json.loads(data)
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "설정 형식이 맞지 않습니다.") from error
    return parsed.get("steps") if isinstance(parsed, dict) else None


@router.get("/channels/{channel_id}/flow", response_class=HTMLResponse)
async def settings_page(
    request: Request, channel_id: int, session: SessionDep, user: CurrentUser
) -> HTMLResponse:
    channel = await managed_flow(session, channel_id, user)
    draft = await engine.draft_definition(session, channel.id)
    published = await engine.published_definition(session, channel.id)
    editing = draft or published
    people = await engine.listed_people(session, channel.id)
    rows = {
        row["id"]: row
        for row in await people_rows(
            session, sorted({one for listed in people.values() for one in listed})
        )
    }
    steps = [
        {**step, "people": [rows[one] for one in people.get(step["key"], []) if one in rows]}
        # 엑셀로 만든 채널은 초안이 없다. 기본 단계에서 시작한다.
        for step in (editing.steps if editing else default_steps())
    ]
    current = {step["key"] for step in steps}
    # 이전 판으로 진행 중인 건이 쓰는 단계. 발행하면 사라질 단계도 여기서 바꾼다.
    old_steps = {
        step["key"]: {
            "key": step["key"],
            "label": step["label"],
            "people": [rows[one] for one in people.get(step["key"], []) if one in rows],
        }
        for definition in await engine.live_definitions(session, channel.id)
        for step in definition.steps
        if has_list(step) and step["key"] not in current
    }
    chosen = await engine.approval_targets(session, channel.id)
    taken = {row.id for row, _ in chosen}
    choices = (
        await session.execute(
            select(Channel, WorkspaceGroup.name)
            .join(WorkspaceGroup, WorkspaceGroup.id == Channel.group_id)
            .where(Channel.type == "approval", WorkspaceGroup.archived_at.is_(None))
            .order_by(WorkspaceGroup.name, Channel.name)
        )
    ).all()
    return templates.TemplateResponse(
        request,
        "flow_settings.html",
        {
            "user": user,
            "active_tab": "more",
            "channel": channel,
            "draft": draft,
            "published": published,
            "editor": {
                "steps": steps,
                "stepKinds": STEP_KINDS,
                "assignKinds": ASSIGN_KINDS,
                "completeKinds": COMPLETE_KINDS,
                "fieldKinds": FIELD_KINDS,
                "limits": {
                    "steps": MAX_STEPS,
                    "fields": MAX_FIELDS,
                    "options": MAX_OPTIONS,
                    "people": MAX_PEOPLE,
                },
            },
            "old_steps": list(old_steps.values()),
            "targets": chosen,
            "target_choices": [(row, name) for row, name in choices if row.id not in taken],
            "open_items": await open_items(session, Channel.id == channel.id),
            "max_people": MAX_PEOPLE,
        },
    )


@router.post("/channels/{channel_id}/flow/draft")
async def save_draft(
    channel_id: int, session: SessionDep, user: CurrentUser, data: Annotated[str, Form()]
) -> Response:
    channel = await managed_flow(session, channel_id, user)
    try:
        await engine.save_draft(session, channel, user, parse_settings(data))
    except FlowError as error:
        raise failed(error) from error
    await session.commit()
    return Response(headers=REFRESH)


@router.post("/channels/{channel_id}/flow/publish")
async def publish_draft(
    channel_id: int, session: SessionDep, user: CurrentUser, data: Annotated[str, Form()]
) -> Response:
    channel = await managed_flow(session, channel_id, user)
    try:
        await engine.publish_draft(session, channel, user, parse_settings(data))
    except FlowError as error:
        raise failed(error) from error
    await session.commit()
    return Response(headers=REFRESH)


@router.get("/channels/{channel_id}/flow/preview", response_class=HTMLResponse)
async def preview(
    request: Request, channel_id: int, session: SessionDep, user: CurrentUser
) -> HTMLResponse:
    channel = await managed_flow(session, channel_id, user)
    definition = await engine.draft_definition(
        session, channel.id
    ) or await engine.published_definition(session, channel.id)
    if definition is None:
        raise not_found()
    steps = []
    people = await engine.listed_people(session, channel.id)
    for step in definition.steps:
        listed = await people_rows(session, people.get(step["key"], []))
        inputs = await field_inputs(session, None, step["fields"], initial_values(step["fields"]))
        steps.append((step, inputs, [row["name"] for row in listed]))
    return templates.TemplateResponse(
        request,
        "flow_preview.html",
        {
            "user": user,
            "active_tab": "more",
            "channel": channel,
            "steps": steps,
            "ASSIGN_KINDS": ASSIGN_KINDS,
            "COMPLETE_KINDS": COMPLETE_KINDS,
            "STEP_KINDS": STEP_KINDS,
            "people_url": f"/channels/{channel.id}/flow/staff",
        },
    )


@router.post("/channels/{channel_id}/flow/assignees")
async def save_assignees(
    channel_id: int,
    session: SessionDep,
    user: CurrentUser,
    step_key: Annotated[str, Form(pattern=r"^s\d{1,6}$")],
    people: Annotated[list[int] | None, Form()] = None,
) -> Response:
    """이전 판 단계의 담당자를 바꾼다."""
    channel = await managed_flow(session, channel_id, user)
    step = next(
        (
            step
            for definition in await engine.live_definitions(session, channel.id)
            for step in definition.steps
            if step["key"] == step_key and has_list(step)
        ),
        None,
    )
    if step is None:
        raise not_found()
    try:
        changed = await engine.set_people(session, channel.id, step_key, people or [])
    except FlowError as error:
        raise failed(error) from error
    if changed is not None:
        engine.audit_people(session, channel, user, step_key, step["label"], changed)
    await session.commit()
    return Response(headers=REFRESH)


@router.post("/channels/{channel_id}/flow/approval-targets")
async def save_approval_target(
    channel_id: int,
    session: SessionDep,
    user: CurrentUser,
    target_channel_id: Annotated[int, Form()],
    is_default: Annotated[bool, Form()] = False,
    remove: Annotated[bool, Form()] = False,
) -> Response:
    channel = await managed_flow(session, channel_id, user)
    target = await session.get(Channel, target_channel_id)
    if target is None or target.type != "approval" or target.group.archived_at is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "결재 채널만 지정할 수 있습니다.")
    await session.execute(select(Channel.id).where(Channel.id == channel.id).with_for_update())
    rows = {
        row.target_channel_id: row
        for row in await session.scalars(
            select(FlowApprovalTarget).where(FlowApprovalTarget.channel_id == channel.id)
        )
    }
    before = sorted(rows)
    if remove:
        if target.id in rows:
            await session.delete(rows.pop(target.id))
    else:
        if target.id not in rows:
            rows[target.id] = FlowApprovalTarget(
                channel_id=channel.id, target_channel_id=target.id, is_default=False
            )
            session.add(rows[target.id])
        if is_default:
            for row in rows.values():
                row.is_default = row.target_channel_id == target.id
    record_audit(
        session,
        actor_id=user.id,
        action="flow.approval_target",
        target_type="channel",
        target_id=channel.id,
        target_channel_id=target.id,
        is_default=is_default,
        before=before,
        after=sorted(rows),
    )
    await session.commit()
    return Response(headers=REFRESH)


@router.get("/channels/{channel_id}/flow/staff")
async def staff(
    channel_id: int, session: SessionDep, user: CurrentUser, q: Search = ""
) -> list[dict[str, object]]:
    """설정 화면의 담당자 검색. 그룹 관리자는 채널 읽기 권한이 없어도 쓸 수 있다."""
    await managed_flow(session, channel_id, user)
    return await search_people(session, q)


# 등록


async def drafting_flow(
    session: AsyncSession, authz: Authorizer, channel_id: int
) -> tuple[Channel, ChannelAccess, FlowDefinition]:
    channel, access = await channel_access(session, authz, channel_id)
    if channel.type != "flow" or not access.write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "이 채널에 등록할 수 없습니다.")
    definition = await engine.published_definition(session, channel.id)
    if definition is None:
        raise conflict("아직 준비 중인 채널입니다.")
    return channel, access, definition


def initial_values(fields: list[dict]) -> dict:
    return {item["key"]: item["template"] for item in fields if item.get("template")}


async def pick_rows(
    session: AsyncSession,
    definition: FlowDefinition,
    channel: Channel,
    picks: dict,
    kept: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    """등록 화면의 단계별 담당자. 등록자 지정이면 고르는 칸, 고정이거나 바꿀 수 없으면 이름만."""
    listed = await engine.listed_people(session, channel.id)
    rows = []
    for step in definition.steps[1:]:
        picked = is_picked(step) and step["key"] not in kept
        own = is_picked(step) or step["key"] in picks
        ids = picks.get(step["key"], []) if own else listed.get(step["key"], [])
        rows.append(
            {
                "key": step["key"],
                "label": step["label"],
                "role": ROLE_LABELS[step["kind"]],
                "complete": COMPLETE_KINDS[step["complete"]],
                "picked": picked,
                "people": await people_rows(session, ids),
            }
        )
    return rows


async def form_page(
    request: Request,
    session: AsyncSession,
    authz: Authorizer,
    channel: Channel,
    definition: FlowDefinition,
    *,
    post: Post | None = None,
    item: FlowItem | None = None,
    values: dict,
    picks: dict,
    dropped: list[str] | None = None,
    reason: str = "",
    doc: FlowDoc | None = None,
) -> HTMLResponse:
    returned = item is not None and item.status == "returned"
    fields = [row for row in definition.steps[0]["fields"] if row["key"] != "title"]
    locked = doc is not None and doc.register_locked
    if locked:
        fields = relaxed(fields, [row.values for row in doc.submissions if row.locked])
    return templates.TemplateResponse(
        request,
        "flow_form.html",
        {
            "user": authz.user,
            "navigation": await build_navigation(authz),
            "active_tab": "home",
            "channel": channel,
            "definition": definition,
            "post": post,
            "returned": returned,
            "reason": reason,
            "title_text": (post.title or "") if post else "",
            "inputs": await field_inputs(
                session,
                post.id if post else None,
                fields,
                values,
            ),
            "locked_sections": await locked_sections(session, doc) if locked else [],
            "picks": await pick_rows(
                session, definition, channel, picks, frozenset(await kept_picks(session, doc))
            ),
            "dropped": dropped or [],
            "people_url": f"/flows/{post.id}/people"
            if returned
            else f"/channels/{channel.id}/flow/people",
            "max_people": MAX_PEOPLE,
        },
    )


@router.get("/channels/{channel_id}/flows/new", response_class=HTMLResponse)
async def new_flow(
    request: Request, channel_id: int, session: SessionDep, authz: AuthzDep
) -> HTMLResponse:
    channel, _, definition = await drafting_flow(session, authz, channel_id)
    values = initial_values(definition.steps[0]["fields"])
    return await form_page(request, session, authz, channel, definition, values=values, picks={})


@dataclass
class Submitted:
    """화면에서 온 입력을 항목별로 나눈 것."""

    form: RawForm
    uploads: dict[str, list[UploadFile]]
    data: FormData


async def read_form(request: Request, fields: list[dict]) -> Submitted:
    form = await request.form()
    texts: dict[str, list[str]] = {}
    uploads: dict[str, list[UploadFile]] = {}
    kept: dict[str, list[int]] = {}
    for item in fields:
        key = item["key"]
        given = form.getlist(f"v_{key}")
        if item["kind"] == "file":
            uploads[key] = [one for one in given if isinstance(one, UploadFile) and one.filename]
            kept[key] = [
                int(one)
                for one in form.getlist(f"k_{key}")
                if isinstance(one, str) and one.isascii() and one.isdigit() and len(one) < 10
            ]
        else:
            texts[key] = [one for one in given if isinstance(one, str)]
    counts = {key: len(files) for key, files in uploads.items()}
    return Submitted(form, uploads, FormData(texts, counts, kept))


async def store_values(
    session: AsyncSession,
    post: Post,
    fields: list[dict],
    submitted: Submitted,
    previous: dict,
    *,
    require: bool,
) -> dict:
    """값을 검사하고 파일을 저장한다. 남긴 첨부는 그 항목에 있던 것만 받는다."""
    total = sum(len(files) for files in submitted.uploads.values())
    limit = get_settings().max_files_per_post
    for item in fields:
        if item["kind"] == "file":
            key = item["key"]
            own = set(previous.get(key, []))
            found = await load_files(session, post.id, own)
            submitted.data.kept[key] = [
                one for one in submitted.data.kept.get(key, []) if one in found
            ]
            total += len(submitted.data.kept[key])
    if total > limit:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"첨부는 {limit}개까지 가능합니다.")
    try:
        values, people = parse_values(fields, submitted.data, require=require)
    except FlowError as error:
        raise failed(error) from error
    if await engine.active_ids(session, people) != people:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "없거나 비활성인 사용자가 있습니다.")
    for key, files in submitted.uploads.items():
        if files:
            rows = await store_attachments(session, post, files)
            await session.flush()
            values[key] = [*values.get(key, []), *[row.id for row in rows]]
    return values


async def kept_picks(session: AsyncSession, doc: FlowDoc | None) -> dict[str, list[int]]:
    """한 번 제출한 건에서 등록자가 바꿀 수 없는 담당자.

    확정자는 반려되어 돌아와도 바꾸지 못한다. 고정 단계를 이 건에서 바꿨으면 그 사람을 둔다.
    """
    if doc is None or doc.item.submitted_at is None:
        return {}
    return {
        step["key"]: await engine.planned(session, doc, step)
        for step in doc.steps[1:]
        if step["kind"] == "confirm" or (not is_picked(step) and step["key"] in doc.picks)
    }


def read_picks(form: RawForm, definition: FlowDefinition) -> dict[str, list[int]]:
    picks = {}
    for step in definition.steps[1:]:
        if is_picked(step):
            picks[step["key"]] = [
                int(one)
                for one in form.getlist(f"p_{step['key']}")
                if isinstance(one, str) and one.isascii() and one.isdigit() and len(one) < 10
            ]
    return picks


async def save_item(
    session: AsyncSession,
    doc: FlowDoc,
    user: User,
    submitted: Submitted,
    *,
    submitting: bool,
) -> None:
    locked = doc.register_locked
    title = " ".join(str(submitted.form.get("title") or "").split())
    if locked:
        title = doc.post.title or ""
    if not title or len(title) > 200:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "제목을 200자까지 입력해 주세요.")
    fields = [row for row in doc.steps[0]["fields"] if row["key"] != "title"]
    if locked:
        fields = relaxed(fields, [row.values for row in doc.submissions if row.locked])
    old_files = {
        one
        for row in fields
        if row["kind"] == "file"
        for one in doc.item.values.get(row["key"], [])
    }
    values = await store_values(
        session, doc.post, fields, submitted, doc.item.values, require=submitting
    )
    try:
        picks = await engine.check_picks(
            session,
            doc.channel,
            user.id,
            doc.steps,
            read_picks(submitted.form, doc.definition),
            require=submitting,
            kept=await kept_picks(session, doc),
        )
    except FlowError as error:
        raise failed(error) from error
    doc.post.title = title
    doc.item.values = values
    await engine.save_picks(session, doc, picks)
    engine.touch(doc)
    if doc.item.status == "draft":
        # 제출한 적이 없으면 뺀 첨부를 남길 이력이 없다.
        kept = {
            one for row in fields if row["kind"] == "file" for one in values.get(row["key"], [])
        }
        for row in (await load_files(session, doc.post.id, old_files - kept)).values():
            row.deleted_at = doc.item.updated_at
            row.deleted_by = user.id
    engine.rebuild_body(doc)
    if not submitting:
        return
    first = doc.item.status == "draft"
    ready = await engine.submit(session, doc, user)
    await publish(
        session,
        Event("created" if first else "updated", doc.channel.id, doc.post.id, user.id),
    )
    await publish_counts(session, ready | {user.id})


@router.post("/channels/{channel_id}/flows", dependencies=[Depends(check_upload_size)])
async def create_flow(
    request: Request, channel_id: int, session: SessionDep, authz: AuthzDep
) -> Response:
    channel, _, definition = await drafting_flow(session, authz, channel_id)
    fields = definition.steps[0]["fields"]
    submitted = await read_form(request, fields)
    if submitted.form.get("definition_id") != str(definition.id):
        raise conflict("양식이 새로 발행되었습니다. 새로고침한 뒤 다시 작성해 주세요.")
    post = Post(channel_id=channel.id, author_id=authz.user.id, body_md="", title="", is_draft=True)
    session.add(post)
    await session.flush()
    item = FlowItem(
        post_id=post.id,
        definition_id=definition.id,
        registrant_id=authz.user.id,
        status="draft",
        round=1,
        current_step=0,
        values={},
    )
    session.add(item)
    await session.flush()
    doc = FlowDoc(post, channel, item, definition)
    await save_item(
        session,
        doc,
        authz.user,
        submitted,
        submitting=submitted.form.get("action") == "submit",
    )
    await session.commit()
    return await after_save(authz, doc)


async def open_doc(
    session: AsyncSession, authz: Authorizer, post_id: int, *, lock: bool = False
) -> tuple[FlowDoc, ChannelAccess]:
    """글을 먼저 잠근다. 답글 작성도 글을 잠그므로 추가 내용과 완료가 엇갈리지 않는다."""
    post, parent, channel, access = await post_context(session, authz, post_id, lock=lock)
    if parent is not None or channel.type != "flow" or post.deleted_at is not None:
        raise not_found()
    doc = await load_doc(session, post, channel, lock=lock)
    if doc is None:
        raise not_found()
    return doc, access


async def own_doc(
    session: AsyncSession, authz: Authorizer, post_id: int, *statuses: str, lock: bool = False
) -> FlowDoc:
    doc, _ = await open_doc(session, authz, post_id, lock=lock)
    if doc.item.registrant_id != authz.user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "등록자만 할 수 있습니다.")
    if doc.item.status not in statuses:
        raise conflict("지금은 할 수 없습니다. 새로고침해 주세요.")
    return doc


@router.get("/flows/{post_id}/edit", response_class=HTMLResponse)
async def edit_page(
    request: Request, post_id: int, session: SessionDep, authz: AuthzDep
) -> HTMLResponse:
    doc = await own_doc(session, authz, post_id, "draft", "returned")
    definition, values, dropped = doc.definition, doc.item.values, []
    picks = doc.picks
    if doc.item.status == "draft":
        # 제출 전에는 새로 발행된 판을 따른다.
        _, _, definition = await drafting_flow(session, authz, doc.channel.id)
        if definition.id != doc.definition.id:
            old, new = doc.steps[0]["fields"], definition.steps[0]["fields"]
            dropped = dropped_labels(old, new, values)
            values = carry_values(old, new, values)
            keys = {step["key"] for step in definition.steps}
            picks = {key: rows for key, rows in picks.items() if key in keys}
    back = [task for task in doc.tasks if task.status == "returned"]
    return await form_page(
        request,
        session,
        authz,
        doc.channel,
        definition,
        post=doc.post,
        item=doc.item,
        values=values,
        picks=picks,
        dropped=dropped,
        reason=(back[-1].comment or "") if back and doc.item.status == "returned" else "",
        doc=doc,
    )


@router.post("/flows/{post_id}/edit", dependencies=[Depends(check_upload_size)])
async def edit_flow(
    request: Request, post_id: int, session: SessionDep, authz: AuthzDep
) -> Response:
    doc = await own_doc(session, authz, post_id, "draft", "returned", lock=True)
    submitting = True
    if doc.item.status == "draft":
        _, _, definition = await drafting_flow(session, authz, doc.channel.id)
        if definition.id != doc.definition.id:
            old, new = doc.steps[0]["fields"], definition.steps[0]["fields"]
            doc.item.values = carry_values(old, new, doc.item.values)
            doc.item.definition_id = definition.id
            doc.definition = definition
    submitted = await read_form(request, doc.steps[0]["fields"])
    if submitted.form.get("definition_id") != str(doc.definition.id):
        raise conflict("양식이 새로 발행되었습니다. 새로고침한 뒤 다시 작성해 주세요.")
    if doc.item.status == "draft":
        submitting = submitted.form.get("action") == "submit"
    await save_item(session, doc, authz.user, submitted, submitting=submitting)
    await session.commit()
    return await after_save(authz, doc)


async def after_save(authz: Authorizer, doc: FlowDoc) -> Response:
    """임시저장은 건으로 돌아가 이어서 쓰고, 제출하면 채널 목록으로 돌아간다."""
    if doc.item.status == "draft":
        return Response(headers={"HX-Redirect": f"/flows/{doc.post.id}"})
    return Response(headers={"HX-Redirect": await back_to_channel(authz, doc)})


async def back_to_channel(authz: Authorizer, doc: FlowDoc) -> str:
    """채널 목록 주소. 그 건만 맡아 채널에 들어갈 수 없으면 내 할 일로 보낸다."""
    if (await authz.access(doc.channel)).can_enter:
        return f"/channels/{doc.channel.id}"
    return "/todo"


@router.post("/flows/{post_id}/delete")
async def delete_draft(post_id: int, session: SessionDep, authz: AuthzDep) -> Response:
    """임시저장만 지운다. 제출한 건은 지울 수 없다."""
    doc = await own_doc(session, authz, post_id, "draft", lock=True)
    doc.post.deleted_at = engine.touch(doc)
    doc.post.deleted_by = authz.user.id
    record_audit(
        session,
        actor_id=authz.user.id,
        action="post.delete",
        target_type="post",
        target_id=doc.post.id,
        channel_id=doc.channel.id,
    )
    await session.commit()
    return Response(headers={"HX-Redirect": f"/channels/{doc.channel.id}"})


@router.post("/flows/{post_id}/copy")
async def copy_flow(post_id: int, session: SessionDep, authz: AuthzDep) -> Response:
    """반려·회수·폐기된 건으로 새 임시저장을 만든다."""
    doc = await own_doc(session, authz, post_id, "rejected", "withdrawn", "discarded", lock=True)
    channel, _, definition = await drafting_flow(session, authz, doc.channel.id)
    old, new = doc.steps[0]["fields"], definition.steps[0]["fields"]
    written = merge_values(old, [values for values, _ in doc.registrations()])
    values = carry_values(old, new, written)
    post = Post(
        channel_id=channel.id,
        author_id=authz.user.id,
        body_md="",
        title=doc.post.title,
        is_draft=True,
    )
    session.add(post)
    await session.flush()
    for row in new:
        if row["kind"] == "file" and values.get(row["key"]):
            files = await load_files(session, doc.post.id, values[row["key"]])
            copies = await copy_files(session, list(files.values()), post.id)
            await session.flush()
            values[row["key"]] = [copies[one].id for one in values[row["key"]] if one in copies]
    item = FlowItem(
        post_id=post.id,
        definition_id=definition.id,
        registrant_id=authz.user.id,
        status="draft",
        round=1,
        current_step=0,
        values=values,
        source_post_id=doc.post.id,
    )
    session.add(item)
    await session.flush()
    copied = FlowDoc(post, channel, item, definition)
    keys = {step["key"] for step in definition.steps if is_picked(step)}
    await engine.save_picks(
        session, copied, {key: rows for key, rows in doc.picks.items() if key in keys}
    )
    engine.rebuild_body(copied)
    await session.commit()
    return Response(headers={"HX-Redirect": f"/flows/{post.id}/edit"})


@router.get("/channels/{channel_id}/flow/people")
async def pickable_people(
    channel_id: int,
    session: SessionDep,
    authz: AuthzDep,
    q: Search = "",
    step: Annotated[str, Query(max_length=10)] = "",
) -> list[dict[str, object]]:
    channel, _, definition = await drafting_flow(session, authz, channel_id)
    return await step_people(session, channel, definition, authz.user.id, step, q)


@router.get("/flows/{post_id}/people")
async def item_people(
    post_id: int,
    session: SessionDep,
    authz: AuthzDep,
    q: Search = "",
    step: Annotated[str, Query(max_length=10)] = "",
) -> list[dict[str, object]]:
    doc, _ = await open_doc(session, authz, post_id)
    own = doc.item.registrant_id == authz.user.id
    if not own and doc.my_task(authz.user.id) is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "지금은 찾을 수 없습니다.")
    return await step_people(session, doc.channel, doc.definition, doc.item.registrant_id, step, q)


async def step_people(
    session: AsyncSession,
    channel: Channel,
    definition: FlowDefinition,
    registrant_id: int,
    step_key: str,
    query: str,
) -> list[dict[str, object]]:
    """단계를 주면 그 단계의 지정 범위에서, 없으면(사람 항목) 전체에서 찾는다."""
    if not step_key:
        return await search_people(session, query)
    step = next((one for one in definition.steps if one["key"] == step_key), None)
    if step is None or not is_picked(step):
        raise not_found()
    allowed = await engine.step_candidates(session, channel, step)
    if step["kind"] != "process":
        allowed.discard(registrant_id)
    elif step["assign"] == "registrant":
        allowed.add(registrant_id)
    return await search_people(session, query, within=allowed)


# 목록과 건 화면


@router.get("/channels/{channel_id}/flows", response_class=HTMLResponse)
async def flow_rows(
    channel_id: int,
    session: SessionDep,
    authz: AuthzDep,
    tab: Annotated[str, Query(max_length=20)] = "all",
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
) -> HTMLResponse:
    channel, access = await channel_access(session, authz, channel_id)
    if channel.type != "flow":
        raise not_found()
    listing = await flow_list(
        session, channel, access, authz.user.id, tab if tab in TABS else "all", page
    )
    return fragment("partials/flow_list.html", channel=channel, listing=listing)


@router.get("/flows/{post_id}", response_class=HTMLResponse)
async def flow_page(
    request: Request, post_id: int, session: SessionDep, authz: AuthzDep
) -> HTMLResponse:
    doc, access = await open_doc(session, authz, post_id)
    (view,) = await build_views(session, [doc.post], authz, doc.channel, access)
    raise_targets = []
    if view.flow.can_raise:
        for target, _ in await engine.approval_targets(session, doc.channel.id):
            if (await authz.access(target)).write:
                raise_targets.append(target)
    source = None
    if doc.item.source_post_id is not None:
        source = await session.get(Post, doc.item.source_post_id)
    return templates.TemplateResponse(
        request,
        "flow.html",
        {
            "user": authz.user,
            "navigation": await build_navigation(authz),
            "active_tab": "home",
            "channel": doc.channel,
            "v": view,
            "flow": view.flow,
            "action": await action_view(session, doc, authz.user.id),
            "changes": await change_rows(session, doc, authz.user.id),
            "source": source,
            "raise_targets": raise_targets,
            "is_channel_manager": await authz.is_manager(doc.channel),
            "show_thread": can_view_replies(access, doc.post, authz.user.id)
            and not doc.post.is_draft,
            "people_url": f"/flows/{doc.post.id}/people",
            "live_channel": (await authz.access(doc.channel)).can_enter,
        },
    )


async def change_rows(session: AsyncSession, doc: FlowDoc, user_id: int) -> list[dict[str, object]]:
    """이 사람이 담당자를 바꿀 수 있는 단계와 지금 담당자."""
    numbers = engine.changeable_steps(doc, user_id, await engine.assigned_people(session, doc))
    return [
        {
            "step_no": number,
            "label": doc.steps[number]["label"],
            "role": ROLE_LABELS[doc.steps[number]["kind"]],
            "current": doc.item.status == "in_progress" and number == doc.item.current_step,
            "people": await people_rows(session, await engine.current_people(session, doc, number)),
        }
        for number in numbers
    ]


@router.get("/flows/{post_id}/assignee-people")
async def assignee_people(
    post_id: int,
    session: SessionDep,
    authz: AuthzDep,
    step_no: Annotated[int, Query(ge=1, le=99)],
    q: Search = "",
) -> list[dict[str, object]]:
    doc, _ = await open_doc(session, authz, post_id)
    allowed = engine.changeable_steps(
        doc, authz.user.id, await engine.assigned_people(session, doc)
    )
    if step_no not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "이 단계의 담당자는 바꿀 수 없습니다.")
    within = await engine.change_candidates(session, doc, step_no)
    within |= set(await engine.current_people(session, doc, step_no))
    return await search_people(session, q, within=within)


@router.post("/flows/{post_id}/assignees")
async def change_assignees(
    post_id: int,
    session: SessionDep,
    authz: AuthzDep,
    step_no: Annotated[int, Form(ge=1, le=99)],
    people: Annotated[list[int] | None, Form()] = None,
    reason: Reason = "",
) -> Response:
    """등록자는 처리·검토 단계를, 확정자는 자기 단계보다 앞 단계를 바꾼다."""
    doc, _ = await open_doc(session, authz, post_id, lock=True)
    try:
        changed = await engine.change_people(
            session, doc, authz.user, step_no, people or [], reason.strip()
        )
    except FlowError as error:
        raise failed(error) from error
    return await respond(session, doc, authz.user, changed)


# 단계 처리


async def respond(
    session: AsyncSession, doc: FlowDoc, actor: User, changed: set[int], to: str | None = None
) -> Response:
    """to가 없으면 건 화면을 다시 연다."""
    await publish(session, Event("updated", doc.channel.id, doc.post.id, actor.id))
    await publish_counts(session, changed | {actor.id})
    await session.commit()
    return Response(headers={"HX-Redirect": to or f"/flows/{doc.post.id}"})


async def turn(
    session: AsyncSession, authz: Authorizer, post_id: int, round_no: int, step_no: int
) -> FlowDoc:
    """화면을 연 때의 회차와 단계가 그대로이고 내 차례인지 확인한다."""
    doc, _ = await open_doc(session, authz, post_id, lock=True)
    if doc.item.status != "in_progress" or (doc.item.round, doc.item.current_step) != (
        round_no,
        step_no,
    ):
        raise conflict("이미 처리되었습니다. 새로고침해 주세요.")
    if doc.my_task(authz.user.id) is None:
        raise conflict("지금은 처리할 차례가 아닙니다.")
    return doc


def form_number(form: RawForm, name: str) -> int:
    value = form.get(name)
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 9:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "입력값을 확인해 주세요.")
    return int(value)


@router.post("/flows/{post_id}/complete", dependencies=[Depends(check_upload_size)])
async def complete(
    request: Request, post_id: int, session: SessionDep, authz: AuthzDep
) -> Response:
    form = await request.form()
    doc = await turn(
        session, authz, post_id, form_number(form, "round"), form_number(form, "step_no")
    )
    if await engine.unseen_notes(session, doc.item.id, form_number(form, "seen"), authz.user.id):
        # 화면을 새로고치지 않아도 되게 지금까지 본 것으로 바꿔 준다. 다시 누르면 진행된다.
        latest = await engine.last_note_id(session, doc.item.id)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "새 추가 내용이 있습니다. 쓰레드에서 확인한 뒤 다시 눌러 주세요.",
            headers={"HX-Trigger": json.dumps({"flow-seen": {"seen": latest}})},
        )
    task = doc.my_task(authz.user.id)
    step = doc.step
    fields = step["fields"]
    comment = str(form.get("comment") or "").strip()
    if len(comment) > 1000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "의견은 1,000자까지입니다.")
    decision = form.get("decision")
    middle = step["kind"] == "confirm" and doc.item.current_step < doc.last
    if middle and decision not in ("finish", "continue"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "마무리할지 다음 단계로 보낼지 골라 주세요."
        )
    previous: dict = {}
    for row in doc.tasks:
        # 다시 할 때는 그 단계에서 냈던 첨부를 남길 수 있다.
        if row.step_no == doc.item.current_step and row.round > doc.locked_round(row.step_no):
            for key, value in row.values.items():
                if isinstance(value, list):
                    previous[key] = [*previous.get(key, []), *value]
    # 돌려보낸 뒤에는 추가한 내용만 받는다. 잠긴 값은 화면에서 오지 않는다.
    fields = relaxed(fields, doc.written(authz.user.id))
    values = await store_values(
        session,
        doc.post,
        fields,
        await read_form(request, fields),
        previous,
        require=True,
    )
    changed = await engine.complete(
        session,
        doc,
        task,
        authz.user,
        values=values,
        comment=comment,
        finish=decision == "finish",
    )
    # 처리·검토·확정을 마치면 채널 목록으로 돌아간다.
    to = await back_to_channel(authz, doc)
    return await respond(session, doc, authz.user, changed, to)


@router.post("/flows/{post_id}/return")
async def send_back(
    post_id: int,
    session: SessionDep,
    authz: AuthzDep,
    round: Annotated[int, Form()],
    step_no: Annotated[int, Form()],
    target: Annotated[int, Form()],
    reason: Reason = "",
) -> Response:
    reason = require_reason(reason)
    doc = await turn(session, authz, post_id, round, step_no)
    try:
        changed = await engine.send_back(
            session, doc, doc.my_task(authz.user.id), authz.user, target, reason
        )
    except FlowError as error:
        raise failed(error) from error
    return await respond(session, doc, authz.user, changed, await back_to_channel(authz, doc))


@router.post("/flows/{post_id}/reject")
async def reject(
    post_id: int,
    session: SessionDep,
    authz: AuthzDep,
    round: Annotated[int, Form()],
    step_no: Annotated[int, Form()],
    reason: Reason = "",
) -> Response:
    reason = require_reason(reason)
    doc = await turn(session, authz, post_id, round, step_no)
    if doc.step["kind"] != "confirm":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "확정 단계에서만 반려할 수 있습니다.")
    changed = await engine.reject(session, doc, doc.my_task(authz.user.id), authz.user, reason)
    return await respond(session, doc, authz.user, changed, await back_to_channel(authz, doc))


@router.post("/flows/{post_id}/cancel")
async def cancel(post_id: int, session: SessionDep, authz: AuthzDep) -> Response:
    doc, _ = await open_doc(session, authz, post_id, lock=True)
    try:
        changed = await engine.cancel(session, doc, authz.user)
    except FlowError as error:
        raise failed(error) from error
    return await respond(session, doc, authz.user, changed)


@router.post("/flows/{post_id}/withdraw")
async def withdraw(post_id: int, session: SessionDep, authz: AuthzDep) -> Response:
    doc, _ = await open_doc(session, authz, post_id, lock=True)
    if not engine.can_withdraw(doc, authz.user.id):
        raise conflict("지금은 회수할 수 없습니다.")
    changed = await engine.withdraw(session, doc, authz.user)
    return await respond(session, doc, authz.user, changed)


# 관리자


async def managed_doc(
    session: AsyncSession, user: User, post_id: int, *, lock: bool = False
) -> FlowDoc:
    """그룹 관리자는 본문을 볼 권한이 없어도 담당자를 바꾸고 폐기한다."""
    post = await session.get(Post, post_id, with_for_update=lock or None)
    if post is None or post.deleted_at is not None or post.is_draft:
        raise not_found()
    channel = await managed_flow(session, post.channel_id, user)
    doc = await load_doc(session, post, channel, lock=lock)
    if doc is None:
        raise not_found()
    return doc


async def manager_response(
    session: AsyncSession, doc: FlowDoc, user: User, changed: set[int]
) -> Response:
    await publish(session, Event("updated", doc.channel.id, doc.post.id, user.id))
    await publish_counts(session, changed | {user.id})
    await session.commit()
    return Response(headers=REFRESH)


@router.get("/flows/{post_id}/reassign-people")
async def reassign_people(
    post_id: int, session: SessionDep, user: CurrentUser, q: Search = ""
) -> list[dict[str, object]]:
    doc = await managed_doc(session, user, post_id)
    if not await engine.stalled(session, doc):
        return []
    return await search_people(session, q, within=await engine.reassign_candidates(session, doc))


@router.post("/flows/{post_id}/reassign")
async def reassign(
    post_id: int,
    session: SessionDep,
    user: CurrentUser,
    new_id: Annotated[int, Form()],
    old_id: Annotated[int | Literal[""], Form()] = "",
    reason: Reason = "",
) -> Response:
    reason = require_reason(reason)
    doc = await managed_doc(session, user, post_id, lock=True)
    if doc.item.status != "in_progress":
        raise conflict("진행 중인 건만 담당자를 바꿀 수 있습니다.")
    # 담당자는 등록자와 확정자가 바꾼다. 관리자는 진행이 멈춘 건만 바꾼다.
    if not await engine.stalled(session, doc):
        raise conflict("진행이 멈춘 건만 관리자가 담당자를 바꿀 수 있습니다.")
    try:
        changed = await engine.reassign(session, doc, user, old_id or None, new_id, reason)
    except FlowError as error:
        raise failed(error) from error
    return await manager_response(session, doc, user, changed)


@router.post("/flows/{post_id}/discard")
async def discard(
    post_id: int, session: SessionDep, user: CurrentUser, reason: Reason = ""
) -> Response:
    reason = require_reason(reason)
    doc = await managed_doc(session, user, post_id, lock=True)
    try:
        changed = await engine.discard(session, doc, user, reason)
    except FlowError as error:
        raise failed(error) from error
    return await manager_response(session, doc, user, changed)
