"""워크플로우 화면에 필요한 값을 만든다. 일반 응답과 SSE가 같은 판정을 쓴다."""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.markdown import render_markdown
from app.core.templating import format_time
from app.models import (
    Approval,
    Attachment,
    AuditLog,
    Channel,
    FlowApproval,
    FlowDefinition,
    FlowItem,
    FlowReply,
    FlowSubmission,
    FlowTask,
    Notification,
    Post,
    User,
    WorkspaceGroup,
)
from app.models.approval import STATUS_LABELS
from app.models.flow import FLOW_OPEN, FLOW_STATUS_LABELS
from app.services.flow_rules import (
    COMPLETE_KINDS,
    DONE_LABELS,
    is_middle_confirm,
    pages,
    relaxed,
)
from app.services.flows import (
    FlowDoc,
    approval_targets,
    can_withdraw,
    cancellable,
    last_note_id,
    listed_people,
    live_approval,
    load_doc,
    participants,
    raisers,
    return_targets,
    stalled,
)
from app.services.permissions import ChannelAccess

PAGE_SIZE = 50
LIST_LIMIT = 30
TABS = {
    "all": "전체",
    "open": "진행 중",
    "mine": "내 차례",
    "registered": "내가 등록",
    "closed": "끝난 건",
}


@dataclass
class ValueView:
    label: str
    kind: str
    text: str = ""
    html: str = ""
    files: list[Attachment] = field(default_factory=list)


@dataclass
class Section:
    """한 단계에서 한 사람이 적은 값."""

    title: str
    by: str
    at: datetime | None
    values: list[ValueView]
    comment: str = ""


@dataclass
class StepChip:
    label: str
    # done, current, waiting, skipped, rejected
    state: str
    names: list[str]
    at: datetime | None = None
    note: str = ""


@dataclass
class NoteView:
    post_id: int
    tag: str
    by: str
    at: datetime
    html: str
    files: list[Attachment]


@dataclass
class EventView:
    at: datetime
    by: str
    text: str
    comment: str = ""


@dataclass
class LinkedApproval:
    post_id: int
    channel_name: str
    status_label: str


@dataclass
class FlowView:
    doc: FlowDoc
    status_label: str
    registrant: str
    chips: list[StepChip]
    sections: list[Section] = field(default_factory=list)
    old_rounds: list[tuple[int, list[Section]]] = field(default_factory=list)
    notes: list[NoteView] = field(default_factory=list)
    events: list[EventView] = field(default_factory=list)
    approvals: list[LinkedApproval] = field(default_factory=list)
    reason: str = ""
    stalled: bool = False
    can_note: bool = False
    can_cancel: bool = False
    can_withdraw: bool = False
    can_edit: bool = False
    can_copy: bool = False
    can_raise: bool = False

    @property
    def item(self) -> FlowItem:
        return self.doc.item


async def names_of(session: AsyncSession, user_ids: Iterable[int]) -> dict[int, str]:
    ids = set(user_ids)
    if not ids:
        return {}
    rows = await session.execute(select(User.id, User.name).where(User.id.in_(ids)))
    return dict(rows.all())


def _file_ids(fields: Sequence[dict], values: Mapping) -> set[int]:
    return {one for item in fields if item["kind"] == "file" for one in values.get(item["key"], [])}


def _person_ids(fields: Sequence[dict], values: Mapping) -> set[int]:
    return {
        one for item in fields if item["kind"] == "person" for one in values.get(item["key"], [])
    }


def value_views(
    fields: Sequence[dict],
    values: Mapping,
    files: Mapping[int, Attachment],
    names: Mapping[int, str],
) -> list[ValueView]:
    views = []
    for item in fields:
        value = values.get(item["key"])
        if item["key"] == "title" or (value in (None, "", []) and item["kind"] != "check"):
            continue
        view = ValueView(item["label"], item["kind"])
        kind = item["kind"]
        if kind == "markdown":
            view.html = render_markdown(value)
        elif kind == "file":
            view.files = [files[one] for one in value if one in files]
        elif kind == "person":
            view.text = ", ".join(names.get(one, "") for one in value)
        elif kind == "multiselect":
            view.text = ", ".join(value)
        elif kind == "check":
            view.text = "예" if value else "아니오"
        elif kind == "number":
            view.text = f"{value}{' ' + item['unit'] if item.get('unit') else ''}"
        else:
            view.text = str(value)
        views.append(view)
    return views


async def load_files(
    session: AsyncSession, post_id: int, ids: Iterable[int]
) -> dict[int, Attachment]:
    wanted = set(ids)
    if not wanted:
        return {}
    rows = await session.scalars(
        select(Attachment).where(
            Attachment.id.in_(wanted),
            Attachment.post_id == post_id,
            Attachment.deleted_at.is_(None),
        )
    )
    return {row.id: row for row in rows}


def step_chips(
    doc: FlowDoc, names: Mapping[int, str], listed: Mapping[str, Sequence[int]]
) -> list[StepChip]:
    item = doc.item
    completions = doc.completions()
    chips = []
    for number, step in enumerate(doc.steps):
        if number == 0:
            current = item.status in ("draft", "returned")
            chips.append(
                StepChip(
                    step["label"],
                    "current" if current else "done",
                    [names.get(item.registrant_id, "")],
                    None if current else item.submitted_at,
                )
            )
            continue
        done = completions.get(number) if number <= doc.finished else None
        if done:
            # 추가한 내용이 있으면 마지막에 한 사람과 시각을 보여준다.
            newest = max(task.round for task in done)
            done = [task for task in done if task.round == newest]
        active = item.status == "in_progress" and number == item.current_step
        if done and not active:
            people = [names.get(task.assignee_id, "") for task in done]
            note = "마무리" if done[0].decision == "finish" else ""
            chips.append(
                StepChip(step["label"], "done", people, max(t.acted_at for t in done), note)
            )
        elif active:
            tasks = doc.current_tasks
            people = [names.get(t.assignee_id, "") for t in tasks if t.status == "waiting"]
            finished = sum(t.status == "done" for t in tasks)
            note = f"{finished}/{len(tasks)} 완료" if finished else ""
            chips.append(StepChip(step["label"], "current", people, note=note))
        elif item.status == "done":
            chips.append(StepChip(step["label"], "skipped", [], note="생략"))
        elif item.status == "rejected" and number == item.current_step:
            people = [names.get(t.assignee_id, "") for t in doc.tasks if t.status == "rejected"]
            chips.append(StepChip(step["label"], "rejected", people[-1:], item.closed_at, "반려"))
        else:
            fixed = step.get("assign") == "fixed" and step["key"] not in doc.picks
            planned = listed.get(step["key"], []) if fixed else doc.picks.get(step["key"], [])
            chips.append(
                StepChip(step["label"], "waiting", [names.get(one, "") for one in planned])
            )
    return chips


def note_tag(steps: Sequence[dict], note: FlowReply, author: str) -> str:
    if note.item_status in FLOW_OPEN:
        where = f"{steps[note.step_no]['label']} 단계"
    else:
        where = f"{FLOW_STATUS_LABELS[note.item_status]} 후 추가"
    return f"{where} · {note.round}회차 · {author}"


EVENT_LABELS = {
    "flow.submit": "제출",
    "flow.return": "보완 요청",
    "flow.reject": "반려",
    "flow.cancel": "처리 취소",
    "flow.withdraw": "회수",
    "flow.discard": "폐기",
    "flow.reassign": "담당자 변경",
    "flow.note": "추가 내용",
    "flow.approval": "결재 올리기",
}


async def _events(session: AsyncSession, doc: FlowDoc) -> list[EventView]:
    """처리 이력은 감사 로그에서 읽는다. 따로 두지 않아 어긋나지 않는다."""
    rows = list(
        await session.scalars(
            select(AuditLog)
            .where(AuditLog.target_type == "flow", AuditLog.target_id == doc.item.id)
            .order_by(AuditLog.id)
        )
    )
    ids = {row.actor_id for row in rows if row.actor_id}
    for row in rows:
        if row.action == "flow.reassign":
            ids |= set(row.detail["before"]) | set(row.detail["after"])
    names = await names_of(session, ids)
    events = []
    for row in rows:
        detail = row.detail
        label = detail.get("label") or EVENT_LABELS.get(row.action)
        if label is None:
            continue
        text = f"{detail['step']} · {label}" if detail.get("step") else label
        if row.action == "flow.submit":
            text = f"{'다시 제출' if detail['round'] > 1 else '제출'} · {detail['round']}회차"
        elif row.action == "flow.return":
            text += f" → {detail['return_to']}"
            if detail.get("locked"):
                text += " · 작성 내용 잠금"
        elif row.action == "flow.reassign":
            before = ", ".join(names.get(one, "") for one in detail["before"])
            after = ", ".join(names.get(one, "") for one in detail["after"])
            text += f" {before} → {after}" if before else f" {after} 추가"
        comment = detail.get("reason") or detail.get("comment") or ""
        events.append(EventView(row.created_at, names.get(row.actor_id, ""), text, comment))
    return events


async def linked_approvals(
    session: AsyncSession, item_ids: Sequence[int]
) -> dict[int, list[LinkedApproval]]:
    rows = await session.execute(
        select(FlowApproval.item_id, Approval.post_id, Channel.name, Approval.status)
        .join(Approval, Approval.id == FlowApproval.approval_id)
        .join(Post, Post.id == Approval.post_id)
        .join(Channel, Channel.id == Post.channel_id)
        .where(FlowApproval.item_id.in_(item_ids), Post.deleted_at.is_(None))
        .order_by(FlowApproval.approval_id)
    )
    found: dict[int, list[LinkedApproval]] = defaultdict(list)
    for item_id, post_id, channel_name, state in rows:
        label = "결재 준비 중" if state == "draft" else STATUS_LABELS[state]
        found[item_id].append(LinkedApproval(post_id, channel_name, label))
    return found


async def load_notes(
    session: AsyncSession, doc: FlowDoc
) -> list[tuple[FlowReply, Post, str, list[Attachment]]]:
    rows = (
        await session.execute(
            select(FlowReply, Post, User.name)
            .join(Post, Post.id == FlowReply.post_id)
            .join(User, User.id == Post.author_id)
            .where(FlowReply.item_id == doc.item.id)
            .order_by(FlowReply.post_id)
        )
    ).all()
    files: dict[int, list[Attachment]] = defaultdict(list)
    for row in await session.scalars(
        select(Attachment)
        .where(
            Attachment.post_id.in_([post.id for _, post, _ in rows]),
            Attachment.deleted_at.is_(None),
        )
        .order_by(Attachment.id)
    ):
        files[row.post_id].append(row)
    return [(note, post, name, files[post.id]) for note, post, name in rows]


def sections_of(
    doc: FlowDoc,
    files: Mapping[int, Attachment],
    names: Mapping[int, str],
) -> list[Section]:
    """지금 유효한 값. 등록 값과 완료한 단계의 값이다."""
    item = doc.item
    sections = register_sections(doc, files, names)
    shown = dict(doc.completions())
    # 전원 완료 단계에서 먼저 낸 값도 보여준다.
    current = [task for task in doc.current_tasks if task.status == "done"]
    if current:
        shown[item.current_step] = [*doc.locked_tasks(item.current_step), *current]
    for _, rows in sorted(shown.items()):
        # 입력도 의견도 없는 확정은 단계 표시로 충분하다.
        found = [_task_section(doc, task, files, names) for task in rows]
        sections += [row for row in found if row.values or row.comment]
    return sections


def register_sections(
    doc: FlowDoc, files: Mapping[int, Attachment], names: Mapping[int, str]
) -> list[Section]:
    label = doc.steps[0]["label"]
    return [
        Section(
            f"{label} 추가" if number else label,
            names.get(doc.item.registrant_id, ""),
            at,
            value_views(doc.steps[0]["fields"], values, files, names),
        )
        for number, (values, at) in enumerate(doc.registrations())
    ]


async def locked_sections(session: AsyncSession, doc: FlowDoc) -> list[Section]:
    """보완 화면에서 고칠 수 없게 보여주는 등록 내용."""
    blocks = [values for values, _ in doc.locked_registrations()]
    register = doc.steps[0]["fields"]
    people = {doc.item.registrant_id}.union(*(_person_ids(register, one) for one in blocks))
    files = await load_files(
        session, doc.post.id, set().union(*(_file_ids(register, one) for one in blocks))
    )
    return register_sections(doc, files, await names_of(session, people))[: len(blocks)]


def _task_section(
    doc: FlowDoc, task: FlowTask, files: Mapping[int, Attachment], names: Mapping[int, str]
) -> Section:
    step = doc.steps[task.step_no]
    first = min((row.round for row in doc.locked_tasks(task.step_no)), default=task.round)
    return Section(
        f"{step['label']} 추가" if task.round > first else step["label"],
        names.get(task.assignee_id, ""),
        task.acted_at,
        value_views(step["fields"], task.values, files, names),
        task.comment or "",
    )


def _old_rounds(
    doc: FlowDoc,
    submissions: Sequence[FlowSubmission],
    files: Mapping[int, Attachment],
    names: Mapping[int, str],
) -> list[tuple[int, list[Section]]]:
    """지금은 유효하지 않은 이전 회차의 제출과 처리."""
    valid = {task.id for rows in doc.completions().values() for task in rows}
    latest = max((row.round for row in submissions), default=0)
    found: dict[int, list[Section]] = defaultdict(list)
    for row in submissions:
        if row.round < latest and not row.locked:
            section = Section(
                f"{doc.steps[0]['label']}: {row.title}",
                names.get(doc.item.registrant_id, ""),
                row.submitted_at,
                value_views(doc.steps[0]["fields"], row.values, files, names),
            )
            found[row.round].append(section)
    for task in doc.tasks:
        ended = doc.item.status not in FLOW_OPEN
        old = task.round < doc.item.round or (ended and task.status == "done")
        if task.status == "done" and old and task.id not in valid:
            found[task.round].append(_task_section(doc, task, files, names))
    return sorted(found.items())


async def flow_views(
    session: AsyncSession, posts: Sequence[Post], channel: Channel, user_id: int
) -> dict[int, FlowView]:
    """건 화면과 글 카드에 쓴다. 목록은 flow_rows를 쓴다."""
    views = {}
    for post in posts:
        doc = await load_doc(session, post, channel)
        if doc is not None:
            views[post.id] = await flow_view(session, doc, user_id)
    return views


async def flow_view(session: AsyncSession, doc: FlowDoc, user_id: int) -> FlowView:
    item = doc.item
    submissions = doc.submissions
    register = doc.steps[0]["fields"]
    blocks = [(register, item.values), *[(register, row.values) for row in submissions]]
    blocks += [(doc.steps[task.step_no]["fields"], task.values) for task in doc.tasks]
    file_ids, people = set(), {item.registrant_id}
    for fields, values in blocks:
        file_ids |= _file_ids(fields, values)
        people |= _person_ids(fields, values)
    listed = await listed_people(session, doc.channel.id)
    people |= {task.assignee_id for task in doc.tasks}
    people |= {one for rows in doc.picks.values() for one in rows}
    people |= {one for rows in listed.values() for one in rows}
    names = await names_of(session, people)
    files = await load_files(session, doc.post.id, file_ids)
    view = FlowView(
        doc,
        FLOW_STATUS_LABELS[item.status],
        names.get(item.registrant_id, ""),
        step_chips(doc, names, listed),
    )
    if item.status == "done" and item.closed_step is not None and item.closed_step < doc.last:
        view.status_label = f"완료 ({doc.steps[item.closed_step]['label']}에서 마무리)"
    elif item.status in FLOW_OPEN and item.round > 1:
        view.status_label += f" {item.round}회차"
    own = item.registrant_id == user_id
    view.sections = sections_of(doc, files, names)
    view.can_edit = own and item.status in ("draft", "returned")
    if item.status == "draft":
        return view
    view.old_rounds = _old_rounds(doc, submissions, files, names)
    view.notes = [
        NoteView(
            post.id,
            note_tag(doc.steps, note, name),
            name,
            post.created_at,
            render_markdown(post.body_md),
            attached,
        )
        for note, post, name, attached in await load_notes(session, doc)
    ]
    view.events = await _events(session, doc)
    view.approvals = (await linked_approvals(session, [item.id])).get(item.id, [])
    if item.status in ("rejected", "discarded"):
        view.reason = item.close_reason or ""
    elif item.status in FLOW_OPEN and item.round > 1:
        back = [task for task in doc.tasks if task.status == "returned"]
        view.reason = (back[-1].comment or "") if back else ""
    view.stalled = await stalled(session, doc)
    view.can_note = user_id in await participants(session, doc)
    view.can_cancel = cancellable(doc, user_id) is not None
    view.can_withdraw = can_withdraw(doc, user_id)
    view.can_copy = own and item.status in ("rejected", "withdrawn", "discarded")
    if user_id in raisers(doc) and not await live_approval(session, item.id):
        view.can_raise = bool(await approval_targets(session, doc.channel.id))
    return view


# 처리 화면


@dataclass
class FieldInput:
    """입력 칸 하나의 초기값."""

    spec: dict
    value: object = None
    files: list[Attachment] = field(default_factory=list)
    people: list[dict[str, object]] = field(default_factory=list)


@dataclass
class ActionView:
    step: dict
    inputs: list[FieldInput]
    comment: str
    done_label: str
    middle: bool
    can_reject: bool
    # (단계 번호, 이름). 첫 번째가 기본값이다.
    targets: list[tuple[int, str]]
    # 확정하고 마무리하면 생략되는 단계.
    skipped: list[str]
    round: int
    step_no: int
    seen: int
    complete_label: str = ""
    # 돌려보내 이 단계에 추가만 할 수 있다.
    add_only: bool = False


async def field_inputs(
    session: AsyncSession, post_id: int | None, fields: Sequence[dict], values: Mapping
) -> list[FieldInput]:
    from app.services.approvals import people_rows

    files = await load_files(session, post_id, _file_ids(fields, values)) if post_id else {}
    inputs = []
    for spec in fields:
        value = values.get(spec["key"])
        row = FieldInput(spec, value)
        if spec["kind"] == "file":
            row.files = [files[one] for one in value or [] if one in files]
        elif spec["kind"] == "person":
            row.people = await people_rows(session, value or [])
        inputs.append(row)
    return inputs


def previous_task(doc: FlowDoc, user_id: int) -> FlowTask | None:
    """같은 단계를 다시 할 때 채워 둘 이전 값. 본인 값이 없으면 마지막으로 완료한 값이다."""
    task = doc.my_task(user_id)
    if task is not None and task.values:
        return task
    old = [
        row
        for row in doc.tasks
        if row.step_no == doc.item.current_step
        and row.values
        and row.status != "waiting"
        and row.round > doc.locked_round(doc.item.current_step)
    ]
    own = [row for row in old if row.assignee_id == user_id]
    return (own or old or [None])[-1]


async def action_view(session: AsyncSession, doc: FlowDoc, user_id: int) -> ActionView | None:
    task = doc.my_task(user_id)
    if task is None:
        return None
    step = doc.step
    number = doc.item.current_step
    middle = is_middle_confirm(doc.steps, number)
    before = previous_task(doc, user_id)
    add_only = doc.locked_round(number) > 0
    if before is not None:
        values = before.values
    elif add_only:
        values = {}
    else:
        values = {row["key"]: row["template"] for row in step["fields"] if row.get("template")}
    return ActionView(
        step=step,
        inputs=await field_inputs(
            session, doc.post.id, relaxed(step["fields"], doc.written(user_id)), values
        ),
        comment=task.comment or "",
        done_label=DONE_LABELS[step["kind"]],
        middle=middle,
        can_reject=step["kind"] == "confirm",
        targets=[(one, doc.steps[one]["label"]) for one in return_targets(doc)],
        skipped=[row["label"] for row in doc.steps[number + 1 :]],
        round=doc.item.round,
        step_no=number,
        seen=await last_note_id(session, doc.item.id),
        complete_label=COMPLETE_KINDS[step["complete"]],
        add_only=add_only,
    )


# 목록


@dataclass
class FlowRow:
    item: FlowItem
    post_id: int
    title: str
    status_label: str
    registrant: str
    chips: list[StepChip]
    people: list[str]
    note_count: int
    approval: LinkedApproval | None
    channel_name: str = ""


def _visible(access: ChannelAccess, user_id: int):
    """임시저장은 본인만, 쓰기만 있는 사람은 자기 건만 본다."""
    own = FlowItem.registrant_id == user_id
    if access.read:
        return or_(FlowItem.status != "draft", own)
    return own if access.write else false()


def _my_turn(user_id: int):
    return (
        select(FlowTask.id)
        .where(
            FlowTask.item_id == FlowItem.id,
            FlowTask.assignee_id == user_id,
            FlowTask.status == "waiting",
        )
        .exists()
    )


def _tab(tab: str, user_id: int):
    return {
        "all": and_(True),
        "open": FlowItem.status.in_(FLOW_OPEN),
        "mine": and_(FlowItem.status == "in_progress", _my_turn(user_id)),
        "registered": FlowItem.registrant_id == user_id,
        "closed": FlowItem.status.in_(("done", "rejected", "withdrawn", "discarded")),
    }[tab]


async def rows_for(
    session: AsyncSession, items: Sequence[tuple[FlowItem, Post, Channel]]
) -> list[FlowRow]:
    if not items:
        return []
    ids = [item.id for item, _, _ in items]
    definitions = {
        row.id: row
        for row in await session.scalars(
            select(FlowDefinition).where(
                FlowDefinition.id.in_({item.definition_id for item, _, _ in items})
            )
        )
    }
    tasks: dict[int, list[FlowTask]] = defaultdict(list)
    for task in await session.scalars(
        select(FlowTask).where(FlowTask.item_id.in_(ids)).order_by(FlowTask.id)
    ):
        tasks[task.item_id].append(task)
    counts = dict(
        (
            await session.execute(
                select(FlowReply.item_id, func.count())
                .where(FlowReply.item_id.in_(ids))
                .group_by(FlowReply.item_id)
            )
        ).all()
    )
    approvals = await linked_approvals(session, ids)
    people = {item.registrant_id for item, _, _ in items}
    people |= {task.assignee_id for rows in tasks.values() for task in rows}
    names = await names_of(session, people)
    result = []
    for item, post, channel in items:
        doc = FlowDoc(post, channel, item, definitions[item.definition_id], tasks[item.id])
        label = FLOW_STATUS_LABELS[item.status]
        if item.status in FLOW_OPEN and item.round > 1:
            label += f" {item.round}회차"
        elif item.status == "done" and item.closed_step is not None and item.closed_step < doc.last:
            label += f" ({doc.steps[item.closed_step]['label']})"
        if item.status == "returned":
            waiting = [names.get(item.registrant_id, "")]
        else:
            waiting = [
                names.get(task.assignee_id, "")
                for task in doc.current_tasks
                if task.status == "waiting"
            ]
        linked = approvals.get(item.id, [])
        result.append(
            FlowRow(
                item,
                post.id,
                post.title or "",
                label,
                names.get(item.registrant_id, ""),
                step_chips(doc, names, {}),
                waiting,
                counts.get(item.id, 0),
                linked[-1] if linked else None,
                channel.name,
            )
        )
    return result


@dataclass
class FlowList:
    tab: str
    tabs: list[tuple[str, str, int]]
    rows: list[FlowRow]
    page: int
    pages: int
    ready: bool


async def flow_list(
    session: AsyncSession,
    channel: Channel,
    access: ChannelAccess,
    user_id: int,
    tab: str = "all",
    page: int = 1,
) -> FlowList:
    base = (
        select(FlowItem)
        .join(Post, Post.id == FlowItem.post_id)
        .where(
            Post.channel_id == channel.id,
            Post.deleted_at.is_(None),
            _visible(access, user_id),
        )
    )
    counts = {
        key: await session.scalar(
            select(func.count()).select_from(base.where(_tab(key, user_id)).subquery())
        )
        for key in TABS
    }
    last = pages(counts[tab], PAGE_SIZE)
    page = min(max(page, 1), last)
    rows = (
        await session.execute(
            base.where(_tab(tab, user_id))
            .with_only_columns(FlowItem, Post)
            .order_by(FlowItem.updated_at.desc(), FlowItem.id.desc())
            .limit(PAGE_SIZE)
            .offset((page - 1) * PAGE_SIZE)
        )
    ).all()
    ready = await session.scalar(
        select(FlowDefinition.id)
        .where(FlowDefinition.channel_id == channel.id, FlowDefinition.version.is_not(None))
        .limit(1)
    )
    return FlowList(
        tab,
        [(key, label, counts[key]) for key, label in TABS.items()],
        await rows_for(session, [(item, post, channel) for item, post in rows]),
        page,
        last,
        ready is not None,
    )


# 내 할 일


def _todo_query():
    return (
        select(FlowItem, Post, Channel)
        .join(Post, Post.id == FlowItem.post_id)
        .join(Channel, Channel.id == Post.channel_id)
        .join(WorkspaceGroup, WorkspaceGroup.id == Channel.group_id)
        .where(
            Post.deleted_at.is_(None),
            WorkspaceGroup.archived_at.is_(None),
            Channel.type.not_in(get_settings().disabled_channel_types),
        )
    )


def _turn_query(user_id: int):
    return _todo_query().where(FlowItem.status == "in_progress", _my_turn(user_id))


def _returned_query(user_id: int):
    return _todo_query().where(FlowItem.status == "returned", FlowItem.registrant_id == user_id)


async def _todo_rows(session: AsyncSession, query) -> list[FlowRow]:
    rows = (await session.execute(query)).all()
    return await rows_for(session, [(item, post, channel) for item, post, channel in rows])


async def my_flow_turns(session: AsyncSession, user_id: int) -> list[FlowRow]:
    return await _todo_rows(session, _turn_query(user_id).order_by(FlowItem.updated_at))


async def my_flow_returned(session: AsyncSession, user_id: int) -> list[FlowRow]:
    return await _todo_rows(session, _returned_query(user_id).order_by(FlowItem.updated_at))


async def my_flow_registered(session: AsyncSession, user_id: int) -> list[FlowRow]:
    return await _todo_rows(
        session,
        _todo_query()
        .where(FlowItem.registrant_id == user_id)
        .order_by(FlowItem.updated_at.desc())
        .limit(LIST_LIMIT),
    )


async def my_flow_handled(session: AsyncSession, user_id: int) -> list[FlowRow]:
    handled = (
        select(FlowTask.id)
        .where(
            FlowTask.item_id == FlowItem.id,
            FlowTask.assignee_id == user_id,
            FlowTask.status.in_(("done", "returned", "rejected")),
        )
        .exists()
    )
    return await _todo_rows(
        session,
        _todo_query().where(handled).order_by(FlowItem.updated_at.desc()).limit(LIST_LIMIT),
    )


@dataclass(frozen=True)
class FlowNotice:
    id: int
    post_id: int
    title: str
    status_label: str


async def flow_notices(session: AsyncSession, user_id: int) -> list[FlowNotice]:
    if "flow" in get_settings().disabled_channel_types:
        return []
    rows = await session.execute(
        select(Notification, FlowItem, Post)
        .join(FlowItem, FlowItem.post_id == Notification.ref_id)
        .join(Post, Post.id == FlowItem.post_id)
        .where(
            Notification.user_id == user_id,
            Notification.type == "flow_result",
            Notification.read_at.is_(None),
        )
        .order_by(Notification.id.desc())
    )
    return [
        FlowNotice(notice.id, post.id, post.title or "", FLOW_STATUS_LABELS[item.status])
        for notice, item, post in rows
    ]


async def flow_todo_count(session: AsyncSession, user_id: int) -> int:
    turns = select(func.count()).select_from(_turn_query(user_id).subquery())
    returned = select(func.count()).select_from(_returned_query(user_id).subquery())
    return (
        (await session.scalar(turns) or 0)
        + (await session.scalar(returned) or 0)
        + len(await flow_notices(session, user_id))
    )


@dataclass
class OpenItem:
    """관리자가 보는 진행 중인 건. 본문은 싣지 않는다."""

    post_id: int
    title: str
    channel_id: int
    channel_name: str
    registrant: str
    step_label: str
    # (사용자 번호, 이름, 활성)
    waiting: list[tuple[int, str, bool]]
    stalled: bool
    can_reassign: bool


async def open_items(
    session: AsyncSession, *conditions, only_stalled: bool = False
) -> list[OpenItem]:
    """관리자의 담당자 변경과 멈춘 건 표시에 쓴다."""
    active_waiting = (
        select(FlowTask.id)
        .join(User, User.id == FlowTask.assignee_id)
        .where(
            FlowTask.item_id == FlowItem.id,
            FlowTask.status == "waiting",
            User.status == "active",
        )
        .exists()
    )
    inactive_waiting = (
        select(FlowTask.id)
        .join(User, User.id == FlowTask.assignee_id)
        .where(
            FlowTask.item_id == FlowItem.id,
            FlowTask.status == "waiting",
            User.status != "active",
        )
        .exists()
    )
    registrant_gone = (
        select(User.id).where(User.id == FlowItem.registrant_id, User.status != "active").exists()
    )
    query = _todo_query().where(FlowItem.status.in_(FLOW_OPEN), *conditions)
    if only_stalled:
        query = query.where(
            or_(
                and_(
                    FlowItem.status == "in_progress",
                    or_(~active_waiting, inactive_waiting),
                ),
                and_(FlowItem.status == "returned", registrant_gone),
            )
        )
    rows = (await session.execute(query.order_by(FlowItem.updated_at).limit(200))).all()
    result = []
    for item, post, channel in rows:
        doc = await load_doc(session, post, channel)
        halted = await stalled(session, doc)
        if only_stalled and not halted:
            continue
        waiting_ids = [task.assignee_id for task in doc.current_tasks if task.status == "waiting"]
        people = {
            user.id: user
            for user in await session.scalars(
                select(User).where(User.id.in_({*waiting_ids, item.registrant_id}))
            )
        }
        result.append(
            OpenItem(
                post.id,
                post.title or "",
                channel.id,
                channel.name,
                people[item.registrant_id].name,
                doc.step["label"],
                [(one, people[one].name, people[one].is_active) for one in waiting_ids],
                halted,
                item.status == "in_progress",
            )
        )
    return sorted(result, key=lambda row: not row.stalled)


async def stalled_flows(session: AsyncSession, group_ids: Iterable[int]) -> list[OpenItem]:
    groups = set(group_ids)
    if not groups:
        return []
    return await open_items(session, Channel.group_id.in_(groups), only_stalled=True)


# 결재로 올릴 때의 사본

BODY_LIMIT = 50_000
LONG_VALUE = 300


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


async def approval_body(
    session: AsyncSession, doc: FlowDoc, template: str, names: Mapping[int, str]
) -> str:
    """결재 채널의 기본 형식 뒤에 건의 사본을 붙인다. 너무 길면 긴 값부터 줄인다."""
    notes = [
        (note_tag(doc.steps, note, name), post.body_md, [row.file_name for row in attached])
        for note, post, name, attached in await load_notes(session, doc)
    ]
    blocks = [
        (doc.steps[0], doc.item.registrant_id, at, values, "") for values, at in doc.registrations()
    ]
    for number, rows in sorted(doc.completions().items()):
        blocks += [
            (doc.steps[number], task.assignee_id, task.acted_at, task.values, task.comment or "")
            for task in rows
        ]
    files = await load_files(
        session,
        doc.post.id,
        {one for step, _, _, values, _ in blocks for one in _file_ids(step["fields"], values)},
    )

    def build(short: bool) -> str:
        def cut(text: str) -> str:
            if short and len(text) > LONG_VALUE:
                return text[:LONG_VALUE] + " … (원본 참고)"
            return text

        lines = [template.rstrip(), "", "---", ""] if template.strip() else []
        lines += [f"## {doc.post.title}", ""]
        for step, user_id, at, values, comment in blocks:
            who = f"{names.get(user_id, '')} ({format_time(at)})"
            lines += [f"### {step['label']} · {who}", ""]
            for item in step["fields"]:
                value = values.get(item["key"])
                if item["key"] == "title" or value in (None, "", []):
                    continue
                if item["kind"] == "markdown":
                    lines += [f"**{item['label']}**", "", cut(value), ""]
                    continue
                if item["kind"] == "file":
                    text = ", ".join(files[one].file_name for one in value if one in files)
                elif item["kind"] == "person":
                    text = ", ".join(names.get(one, "") for one in value)
                elif item["kind"] == "multiselect":
                    text = ", ".join(value)
                elif item["kind"] == "check":
                    text = "예"
                elif item["kind"] == "number" and item.get("unit"):
                    text = f"{value} {item['unit']}"
                else:
                    text = str(value)
                lines += [f"- **{item['label']}**: {_cell(text)}"]
            if comment:
                lines += [f"- **의견**: {_cell(cut(comment))}"]
            lines.append("")
        if notes:
            lines += ["### 추가 내용", ""]
            for tag, body, attached in notes:
                lines += [f"**{tag}**", "", cut(body)]
                if attached:
                    lines += ["", "첨부: " + ", ".join(attached)]
                lines.append("")
        lines += ["---", "", f"[원본 워크플로우](/flows/{doc.post.id})"]
        return "\n".join(lines)

    body = build(False)
    if len(body) > BODY_LIMIT:
        body = build(True)
    if len(body) > BODY_LIMIT:
        tail = f"\n\n… (원본 참고)\n\n[원본 워크플로우](/flows/{doc.post.id})"
        body = body[: BODY_LIMIT - len(tail)] + tail
    return body


async def approval_files(session: AsyncSession, doc: FlowDoc) -> list[Attachment]:
    """사본에 들어가는 값과 추가 내용의 첨부. 이전 회차의 첨부는 뺀다."""
    ids = set()
    for values, _ in doc.registrations():
        ids |= _file_ids(doc.steps[0]["fields"], values)
    for number, rows in doc.completions().items():
        for task in rows:
            ids |= _file_ids(doc.steps[number]["fields"], task.values)
    files = list((await load_files(session, doc.post.id, ids)).values())
    for _, _, _, attached in await load_notes(session, doc):
        files += attached
    return sorted(files, key=lambda row: row.id)


async def body_names(session: AsyncSession, doc: FlowDoc) -> dict[int, str]:
    people = {doc.item.registrant_id}
    for values, _ in doc.registrations():
        people |= _person_ids(doc.steps[0]["fields"], values)
    for number, rows in doc.completions().items():
        for task in rows:
            people.add(task.assignee_id)
            people |= _person_ids(doc.steps[number]["fields"], task.values)
    return await names_of(session, people)


async def reply_notes(session: AsyncSession, post_ids: Sequence[int]) -> dict[int, str]:
    """쓰레드에서 추가 내용 답글에 붙일 표시."""
    rows = await session.execute(
        select(FlowReply, FlowDefinition.steps, User.name)
        .join(FlowItem, FlowItem.id == FlowReply.item_id)
        .join(FlowDefinition, FlowDefinition.id == FlowItem.definition_id)
        .join(Post, Post.id == FlowReply.post_id)
        .join(User, User.id == Post.author_id)
        .where(FlowReply.post_id.in_(post_ids))
    )
    return {note.post_id: note_tag(steps, note, name) for note, steps, name in rows}
