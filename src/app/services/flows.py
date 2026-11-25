"""워크플로우의 정의 저장과 진행 처리. 화면과 서버 처리가 같은 판정 함수를 쓴다."""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Approval,
    Channel,
    FlowApproval,
    FlowApprovalTarget,
    FlowAssignee,
    FlowDefinition,
    FlowItem,
    FlowPick,
    FlowReply,
    FlowSubmission,
    FlowTask,
    Notification,
    Post,
    User,
)
from app.models.flow import FLOW_OPEN
from app.services.audit import record_audit
from app.services.flow_rules import (
    DONE_LABELS,
    MAX_PEOPLE,
    FlowError,
    Keys,
    Step,
    check_publish,
    clean_steps,
    default_steps,
    has_list,
    is_middle_confirm,
    is_picked,
)
from app.services.permissions import access_for_users
from app.services.work import MARKDOWN_SPECIAL

ACTIVE_TASKS = ("waiting", "done", "returned", "rejected", "skipped")
# 결재가 살아 있으면 같은 건에서 다시 올릴 수 없다.
LIVE_APPROVALS = ("draft", "in_progress", "approved", "receiving")


# 정의


async def published_definition(session: AsyncSession, channel_id: int) -> FlowDefinition | None:
    return await session.scalar(
        select(FlowDefinition)
        .where(FlowDefinition.channel_id == channel_id, FlowDefinition.version.is_not(None))
        .order_by(FlowDefinition.version.desc())
        .limit(1)
    )


async def draft_definition(session: AsyncSession, channel_id: int) -> FlowDefinition | None:
    return await session.scalar(
        select(FlowDefinition).where(
            FlowDefinition.channel_id == channel_id, FlowDefinition.version.is_(None)
        )
    )


async def listed_people(session: AsyncSession, channel_id: int) -> dict[str, list[int]]:
    """단계 번호별로 채널에 등록된 사람. 비활성 사용자도 담는다."""
    rows = await session.execute(
        select(FlowAssignee.step_key, FlowAssignee.user_id)
        .join(User, User.id == FlowAssignee.user_id)
        .where(FlowAssignee.channel_id == channel_id)
        .order_by(User.name, User.id)
    )
    people: dict[str, list[int]] = defaultdict(list)
    for key, user_id in rows:
        people[key].append(user_id)
    return dict(people)


async def active_ids(session: AsyncSession, user_ids: Iterable[int]) -> set[int]:
    ids = set(user_ids)
    if not ids:
        return set()
    return set(
        await session.scalars(select(User.id).where(User.id.in_(ids), User.status == "active"))
    )


async def set_people(
    session: AsyncSession, channel_id: int, step_key: str, user_ids: Sequence[int]
) -> tuple[list[int], list[int]] | None:
    """단계의 사람을 바꾸고 전후를 돌려준다. 같으면 None이다."""
    if len(user_ids) > MAX_PEOPLE:
        raise FlowError(f"담당자는 {MAX_PEOPLE}명까지입니다.")
    before = sorted(
        await session.scalars(
            select(FlowAssignee.user_id).where(
                FlowAssignee.channel_id == channel_id, FlowAssignee.step_key == step_key
            )
        )
    )
    after = sorted(set(user_ids))
    if before == after:
        return None
    added = set(after) - set(before)
    if await active_ids(session, added) != added:
        raise FlowError("없거나 비활성인 사용자가 있습니다.")
    await session.execute(
        delete(FlowAssignee).where(
            FlowAssignee.channel_id == channel_id,
            FlowAssignee.step_key == step_key,
            FlowAssignee.user_id.not_in(after),
        )
    )
    session.add_all(
        FlowAssignee(channel_id=channel_id, step_key=step_key, user_id=user_id)
        for user_id in sorted(added)
    )
    await session.flush()
    return before, after


async def live_definitions(session: AsyncSession, channel_id: int) -> list[FlowDefinition]:
    """지금 쓰는 판과 초안, 아직 끝나지 않은 건이 쓰는 이전 판."""
    latest = await published_definition(session, channel_id)
    open_versions = (
        select(FlowItem.definition_id)
        .join(Post, Post.id == FlowItem.post_id)
        .where(Post.channel_id == channel_id, FlowItem.status.in_(FLOW_OPEN))
    )
    rows = await session.scalars(
        select(FlowDefinition)
        .where(
            FlowDefinition.channel_id == channel_id,
            or_(
                FlowDefinition.version.is_(None),
                FlowDefinition.id == (latest.id if latest else 0),
                FlowDefinition.id.in_(open_versions),
            ),
        )
        .order_by(FlowDefinition.id)
    )
    return list(rows)


async def prune_people(session: AsyncSession, channel_id: int) -> None:
    """어느 판에서도 쓰지 않는 단계의 사람을 지운다. 남겨 두면 채널 권한이 계속 열린다."""
    await session.flush()
    keys = {
        step["key"]
        for definition in await live_definitions(session, channel_id)
        for step in definition.steps
        if has_list(step)
    }
    await session.execute(
        delete(FlowAssignee).where(
            FlowAssignee.channel_id == channel_id, FlowAssignee.step_key.not_in(keys)
        )
    )


def start_draft(session: AsyncSession, channel: Channel, user: User) -> None:
    session.add(FlowDefinition(channel_id=channel.id, steps=default_steps(), created_by=user.id))


async def save_draft(
    session: AsyncSession, channel: Channel, user: User, raw: object
) -> FlowDefinition:
    """초안을 저장한다. 사람은 발행과 상관없이 바로 적용한다."""
    await session.execute(select(Channel.id).where(Channel.id == channel.id).with_for_update())
    definitions = list(
        await session.scalars(select(FlowDefinition).where(FlowDefinition.channel_id == channel.id))
    )
    draft = next((row for row in definitions if row.version is None), None)
    published = max(
        (row for row in definitions if row.version is not None),
        key=lambda row: row.version,
        default=None,
    )
    keys = Keys(row.steps for row in definitions)
    steps, people = clean_steps(raw, keys, published.steps if published else None)
    for key, user_ids in people.items():
        changed = await set_people(session, channel.id, key, user_ids)
        if changed is not None:
            audit_people(session, channel, user, key, _label(steps, key), changed)
    if draft is None:
        draft = FlowDefinition(channel_id=channel.id, steps=steps, created_by=user.id)
        session.add(draft)
    else:
        draft.steps = steps
    if published is not None and steps == published.steps:
        # 사람만 바꿨으면 초안을 남기지 않는다.
        await session.flush()
        await session.delete(draft)
        draft = published
    await prune_people(session, channel.id)
    return draft


def _label(steps: Sequence[Step], key: str) -> str:
    return next((step["label"] for step in steps if step["key"] == key), key)


def audit_people(
    session: AsyncSession,
    channel: Channel,
    user: User,
    key: str,
    label: str,
    changed: tuple[list[int], list[int]],
) -> None:
    record_audit(
        session,
        actor_id=user.id,
        action="flow.assignees",
        target_type="channel",
        target_id=channel.id,
        step=label,
        before=changed[0],
        after=changed[1],
    )


async def publish_draft(
    session: AsyncSession, channel: Channel, user: User, raw: object
) -> FlowDefinition:
    draft = await save_draft(session, channel, user, raw)
    people = await listed_people(session, channel.id)
    active = await active_ids(session, (one for rows in people.values() for one in rows))
    check_publish(
        draft.steps,
        {key: [one for one in rows if one in active] for key, rows in people.items()},
    )
    if draft.version is not None:
        raise FlowError("바뀐 내용이 없습니다. 담당자는 저장만 하면 바로 적용됩니다.")
    before = await published_definition(session, channel.id)
    draft.version = (before.version if before else 0) + 1
    draft.published_by = user.id
    draft.published_at = datetime.now(UTC)
    record_audit(
        session,
        actor_id=user.id,
        action="flow.publish",
        target_type="channel",
        target_id=channel.id,
        version=draft.version,
        before=before.steps if before else None,
        after=draft.steps,
    )
    await prune_people(session, channel.id)
    return draft


# 지정할 수 있는 사람


async def channel_readers(session: AsyncSession, channel: Channel) -> set[int]:
    users = list(await session.scalars(select(User.id).where(User.status == "active")))
    access = await access_for_users(session, channel, users)
    return {user_id for user_id in users if access[user_id].read}


async def step_candidates(session: AsyncSession, channel: Channel, step: Step) -> set[int]:
    """그 단계를 맡을 수 있는 활성 사용자. 등록자 제외는 건마다 따로 한다."""
    if has_list(step):
        people = await listed_people(session, channel.id)
        return await active_ids(session, people.get(step["key"], []))
    return await channel_readers(session, channel)


# 건


@dataclass
class FlowDoc:
    post: Post
    channel: Channel
    item: FlowItem
    definition: FlowDefinition
    tasks: list[FlowTask] = field(default_factory=list)
    # 단계 번호별로 등록자가 지정한 사람.
    picks: dict[str, list[int]] = field(default_factory=dict)
    submissions: list[FlowSubmission] = field(default_factory=list)

    @property
    def steps(self) -> list[Step]:
        return self.definition.steps

    @property
    def step(self) -> Step:
        return self.steps[self.item.current_step]

    @property
    def last(self) -> int:
        return len(self.steps) - 1

    @property
    def current_tasks(self) -> list[FlowTask]:
        """지금 단계에서 이번 회차에 만든 담당. 바뀌거나 무효가 된 것은 뺀다."""
        if self.item.status != "in_progress":
            return []
        return [
            task
            for task in self.tasks
            if task.round == self.item.round
            and task.step_no == self.item.current_step
            and task.status in ACTIVE_TASKS
        ]

    def my_task(self, user_id: int) -> FlowTask | None:
        return next(
            (
                task
                for task in self.current_tasks
                if task.assignee_id == user_id and task.status == "waiting"
            ),
            None,
        )

    def completions(self) -> dict[int, list[FlowTask]]:
        """지금도 유효한 완료. 되돌아가 다시 한 단계는 마지막 회차만 유효하다.

        확정 단계가 돌려보내 잠긴 완료는 그대로 남고, 다시 한 완료는 그 뒤에 추가된다.
        """
        item = self.item
        found: dict[int, list[FlowTask]] = {}
        for number in range(1, len(self.steps)):
            if item.status == "done" and number > self.finished:
                continue
            rows = self.locked_tasks(number)
            since = self.locked_round(number)
            redone = [
                task
                for task in self.tasks
                if task.step_no == number
                and task.status == "done"
                and number <= self.finished
                and task.round > since
            ]
            newest = max((task.round for task in redone), default=0)
            rows += [task for task in redone if task.round == newest]
            if rows:
                found[number] = rows
        return found

    @property
    def finished(self) -> int:
        """끝난 마지막 단계."""
        if self.item.status == "done" and self.item.closed_step is not None:
            return self.item.closed_step
        return self.item.current_step - 1

    def locked_tasks(self, step_no: int) -> list[FlowTask]:
        return [task for task in self.tasks if task.locked and task.step_no == step_no]

    def locked_round(self, step_no: int) -> int:
        """이 회차까지의 작성 내용은 고칠 수 없다. 0이면 잠긴 적이 없다."""
        return max((task.round for task in self.locked_tasks(step_no)), default=0)

    def registrations(self) -> list[tuple[dict, datetime | None]]:
        """유효한 등록 값. 잠긴 제출 뒤에 추가한 내용이 이어진다."""
        blocks = self.locked_registrations()
        if not blocks:
            return [(self.item.values, self.item.submitted_at)]
        if self.item.values:
            latest = self.submissions[-1]
            blocks.append((self.item.values, None if latest.locked else latest.submitted_at))
        return blocks

    def locked_registrations(self) -> list[tuple[dict, datetime | None]]:
        """잠긴 등록 값. 추가한 것이 없는 제출은 뺀다."""
        rows = [row for row in self.submissions if row.locked]
        return [
            (row.values, row.submitted_at)
            for number, row in enumerate(rows)
            if number == 0 or row.values
        ]

    @property
    def register_locked(self) -> bool:
        return any(row.locked for row in self.submissions)

    def written(self, user_id: int) -> list[dict]:
        """지금 단계를 추가만 할 수 있을 때 이미 적은 값. 전원 완료면 본인 것만 본다."""
        rows = self.locked_tasks(self.item.current_step)
        if self.step.get("complete") == "all":
            rows = [task for task in rows if task.assignee_id == user_id]
        return [task.values for task in rows]

    def excluded(self, step_no: int) -> set[int]:
        """등록자는 검토·확정을, 처리한 사람은 뒤의 검토를 하지 않는다."""
        kind = self.steps[step_no]["kind"]
        if kind == "process":
            return set()
        result = {self.item.registrant_id}
        if kind == "review":
            result |= {
                task.assignee_id
                for number, rows in self.completions().items()
                if number < step_no and self.steps[number]["kind"] == "process"
                for task in rows
            }
        return result


async def load_doc(
    session: AsyncSession, post: Post, channel: Channel, *, lock: bool = False
) -> FlowDoc | None:
    query = select(FlowItem).where(FlowItem.post_id == post.id)
    if lock:
        # 동시 요청은 앞선 처리가 끝난 상태를 보고 판정한다.
        query = query.with_for_update()
    item = await session.scalar(query.execution_options(populate_existing=True))
    if item is None:
        return None
    definition = await session.get(FlowDefinition, item.definition_id)
    tasks = await session.scalars(
        select(FlowTask)
        .where(FlowTask.item_id == item.id)
        .order_by(FlowTask.id)
        .execution_options(populate_existing=True)
    )
    submissions = await session.scalars(
        select(FlowSubmission)
        .where(FlowSubmission.item_id == item.id)
        .order_by(FlowSubmission.round)
        .execution_options(populate_existing=True)
    )
    return FlowDoc(
        post,
        channel,
        item,
        definition,
        list(tasks),
        await load_picks(session, item.id),
        list(submissions),
    )


async def load_picks(session: AsyncSession, item_id: int) -> dict[str, list[int]]:
    picks: dict[str, list[int]] = defaultdict(list)
    for key, user_id in await session.execute(
        select(FlowPick.step_key, FlowPick.user_id)
        .join(User, User.id == FlowPick.user_id)
        .where(FlowPick.item_id == item_id)
        .order_by(User.name, User.id)
    ):
        picks[key].append(user_id)
    return dict(picks)


async def save_picks(
    session: AsyncSession, doc: FlowDoc, picks: Mapping[str, Sequence[int]]
) -> None:
    await session.execute(delete(FlowPick).where(FlowPick.item_id == doc.item.id))
    session.add_all(
        FlowPick(item_id=doc.item.id, step_key=key, user_id=user_id)
        for key, user_ids in picks.items()
        for user_id in user_ids
    )
    await session.flush()
    doc.picks = {key: list(user_ids) for key, user_ids in picks.items() if user_ids}


async def check_picks(
    session: AsyncSession,
    channel: Channel,
    registrant_id: int,
    steps: Sequence[Step],
    given: Mapping[str, Sequence[int]],
    *,
    require: bool,
) -> dict[str, list[int]]:
    """지정 범위를 서버에서 다시 검사한다. require면 단계를 끝까지 맡을 사람이 있는지도 본다."""
    picks: dict[str, list[int]] = {}
    assigned: dict[int, set[int]] = {}
    listed = await listed_people(session, channel.id)
    readers: set[int] | None = None
    everyone = {one for rows in listed.values() for one in rows}
    everyone |= {one for rows in given.values() for one in rows}
    active = await active_ids(session, everyone)
    for number, step in enumerate(steps):
        if step["kind"] == "register":
            continue
        label = step["label"]
        if not is_picked(step):
            assigned[number] = set(listed.get(step["key"], [])) & active
            continue
        chosen = list(dict.fromkeys(given.get(step["key"], [])))
        if len(chosen) > MAX_PEOPLE:
            raise FlowError(f"{label}: 담당자는 {MAX_PEOPLE}명까지입니다.")
        if step["assign"] == "listed":
            allowed = set(listed.get(step["key"], []))
        else:
            if readers is None:
                readers = await channel_readers(session, channel)
            allowed = set(readers)
            if step["kind"] == "process":
                # 쓰기만 있는 등록자도 자기 건의 처리는 맡을 수 있다.
                allowed.add(registrant_id)
        allowed &= active
        if not set(chosen) <= allowed:
            raise FlowError(f"{label}: 지정할 수 없는 사람이 있습니다. 다시 골라 주세요.")
        if step["kind"] != "process" and registrant_id in chosen:
            raise FlowError(f"{label}: 등록자는 자기 건의 검토와 확정을 맡을 수 없습니다.")
        if require and not chosen:
            raise FlowError(f"{label}: 담당자를 지정해 주세요.")
        picks[step["key"]] = chosen
        assigned[number] = set(chosen)
    if not require:
        return picks
    for number, people in assigned.items():
        step = steps[number]
        remaining = set(people)
        if step["kind"] != "process":
            remaining.discard(registrant_id)
            if not remaining and people:
                raise FlowError(
                    f"{step['label']}: 담당자가 등록자뿐이라 제출할 수 없습니다. "
                    "그룹 관리자에게 문의해 주세요."
                )
        if step["kind"] == "review":
            for before in range(1, number):
                if steps[before]["kind"] != "process":
                    continue
                doers = assigned[before]
                if steps[before]["complete"] == "all" or len(doers) == 1:
                    remaining -= doers
            if not remaining and people:
                raise FlowError(
                    f"{step['label']}: 처리한 사람은 검토할 수 없습니다. 다른 사람이 필요합니다."
                )
        if not remaining:
            raise FlowError(f"{step['label']}: 담당자가 없습니다. 그룹 관리자에게 문의해 주세요.")
    return picks


async def start_step(session: AsyncSession, doc: FlowDoc, step_no: int) -> set[int]:
    """단계를 시작하고 차례가 온 사람을 돌려준다. 맡을 사람이 없으면 건이 멈춘다."""
    step = doc.steps[step_no]
    doc.item.current_step = step_no
    if is_picked(step):
        people = doc.picks.get(step["key"], [])
    else:
        people = (await listed_people(session, doc.channel.id)).get(step["key"], [])
    ready = await active_ids(session, people) - doc.excluded(step_no)
    tasks = [
        FlowTask(
            item_id=doc.item.id,
            round=doc.item.round,
            step_no=step_no,
            assignee_id=user_id,
            status="waiting",
            values={},
        )
        for user_id in sorted(ready)
    ]
    session.add_all(tasks)
    await session.flush()
    doc.tasks += tasks
    return ready


def audit(session: AsyncSession, doc: FlowDoc, actor: User, action: str, **detail: object) -> None:
    record_audit(
        session,
        actor_id=actor.id,
        action=f"flow.{action}",
        target_type="flow",
        target_id=doc.item.id,
        post_id=doc.post.id,
        round=doc.item.round,
        **detail,
    )


def touch(doc: FlowDoc) -> datetime:
    now = datetime.now(UTC)
    doc.item.updated_at = now
    return now


def waiting_ids(doc: FlowDoc) -> set[int]:
    return {task.assignee_id for task in doc.current_tasks if task.status == "waiting"}


async def close(
    session: AsyncSession, doc: FlowDoc, status: str, actor: User, reason: str | None = None
) -> None:
    now = touch(doc)
    item = doc.item
    item.status = status
    item.closed_at = now
    item.close_reason = reason
    if status != "withdrawn" and item.registrant_id != actor.id:
        session.add(
            Notification(user_id=item.registrant_id, type="flow_result", ref_id=doc.post.id)
        )
    await prune_people(session, doc.channel.id)


async def submit(session: AsyncSession, doc: FlowDoc, actor: User) -> set[int]:
    now = touch(doc)
    item = doc.item
    item.status = "in_progress"
    if item.submitted_at is None:
        item.submitted_at = now
    doc.post.is_draft = False
    submission = FlowSubmission(
        item_id=item.id,
        round=item.round,
        title=doc.post.title or "",
        values=item.values,
        picks=doc.picks,
        locked=False,
        submitted_at=now,
    )
    session.add(submission)
    doc.submissions.append(submission)
    # 처음 제출할 때와 다시 제출할 때를 구분해 이력에 보여준다.
    audit(
        session,
        doc,
        actor,
        "submit",
        version=doc.definition.version,
        picks=doc.picks,
    )
    ready = await start_step(session, doc, 1)
    rebuild_body(doc)
    return ready


async def complete(
    session: AsyncSession,
    doc: FlowDoc,
    task: FlowTask,
    actor: User,
    *,
    values: dict,
    comment: str,
    finish: bool,
) -> set[int]:
    """단계를 완료하고 개수가 바뀐 사람을 돌려준다."""
    now = touch(doc)
    step_no = doc.item.current_step
    step = doc.step
    middle = is_middle_confirm(doc.steps, step_no)
    if step["kind"] != "confirm":
        task.values = values
    task.status = "done"
    task.comment = comment or None
    task.acted_at = now
    task.decision = ("finish" if finish else "continue") if middle else None
    changed = {actor.id}
    audit(
        session,
        doc,
        actor,
        "complete",
        step=step["label"],
        label=_done_label(step, middle, finish),
        comment=comment,
        decision=task.decision,
    )
    others = [row for row in doc.current_tasks if row.status == "waiting"]
    if step["complete"] == "all" and others:
        rebuild_body(doc)
        return changed
    for row in others:
        row.status = "skipped"
        row.acted_at = now
        changed.add(row.assignee_id)
    if step_no == doc.last or (middle and finish):
        doc.item.closed_step = step_no
        await close(session, doc, "done", actor)
        changed.add(doc.item.registrant_id)
    else:
        # 다음 담당자를 정할 때 이번 완료도 봐야 하므로 먼저 단계를 옮긴다.
        doc.item.current_step = step_no + 1
        changed |= await start_step(session, doc, step_no + 1)
    rebuild_body(doc)
    return changed


def _done_label(step: Step, middle: bool, finish: bool) -> str:
    if middle:
        return "확정하고 마무리" if finish else "확정하고 다음 단계로"
    return DONE_LABELS[step["kind"]]


def return_targets(doc: FlowDoc) -> list[int]:
    """돌려보낼 수 있는 앞 단계. 기본값(바로 앞의 처리, 없으면 등록)을 맨 앞에 둔다."""
    before = [
        number for number in range(doc.item.current_step) if doc.steps[number]["kind"] != "confirm"
    ]
    process = [number for number in before if doc.steps[number]["kind"] == "process"]
    default = process[-1] if process else 0
    return [default, *[number for number in before if number != default]]


def lock_written(doc: FlowDoc) -> None:
    """확정 단계가 돌려보낼 때 앞 단계에 적은 내용을 모두 잠근다.

    돌려보낸 단계만 잠그면 그 단계에서 더 앞으로 돌려보내 고칠 수 있다.
    """
    for number, rows in doc.completions().items():
        if number < doc.item.current_step and doc.steps[number]["kind"] != "confirm":
            for task in rows:
                task.locked = True
    if not doc.submissions[-1].locked:
        doc.submissions[-1].locked = True
        # 이제부터는 추가한 내용만 담는다.
        doc.item.values = {}


async def send_back(
    session: AsyncSession, doc: FlowDoc, task: FlowTask, actor: User, target: int, reason: str
) -> set[int]:
    if target not in return_targets(doc):
        raise FlowError("돌려보낼 단계를 확인해 주세요.")
    now = touch(doc)
    changed = waiting_ids(doc) | {doc.item.registrant_id}
    locking = doc.step["kind"] == "confirm"
    audit(
        session,
        doc,
        actor,
        "return",
        step=doc.step["label"],
        return_to=doc.steps[target]["label"],
        reason=reason,
        locked=locking,
    )
    if locking:
        lock_written(doc)
    for row in doc.current_tasks:
        if row.status == "waiting":
            row.status = "returned" if row is task else "skipped"
            row.acted_at = now
    task.return_to = target
    task.comment = reason
    doc.item.round += 1
    if target == 0:
        doc.item.status = "returned"
        doc.item.current_step = 0
    else:
        doc.item.current_step = target
        changed |= await start_step(session, doc, target)
    rebuild_body(doc)
    return changed


async def reject(
    session: AsyncSession, doc: FlowDoc, task: FlowTask, actor: User, reason: str
) -> set[int]:
    now = touch(doc)
    changed = waiting_ids(doc) | {doc.item.registrant_id}
    audit(session, doc, actor, "reject", step=doc.step["label"], reason=reason)
    for row in doc.current_tasks:
        if row.status == "waiting":
            row.status = "rejected" if row is task else "skipped"
            row.acted_at = now
    task.comment = reason
    await close(session, doc, "rejected", actor, reason)
    return changed


def cancellable(doc: FlowDoc, user_id: int) -> FlowTask | None:
    """다음 단계에서 아무도 처리하지 않았을 때만 본인 완료를 취소한다."""
    item = doc.item
    if item.status != "in_progress":
        return None
    current = doc.current_tasks
    mine = next(
        (row for row in current if row.assignee_id == user_id and row.status == "done"), None
    )
    if mine is not None:
        return mine
    if any(row.status != "waiting" for row in current):
        return None
    # 담당자 변경이 있었으면 다음 단계를 손댄 것이므로 되돌리지 않는다.
    if any(
        row.status == "replaced" and row.round == item.round and row.step_no == item.current_step
        for row in doc.tasks
    ):
        return None
    return next(
        (
            row
            for row in doc.tasks
            if row.round == item.round
            and row.step_no == item.current_step - 1
            and row.assignee_id == user_id
            and row.status == "done"
        ),
        None,
    )


async def cancel(session: AsyncSession, doc: FlowDoc, actor: User) -> set[int]:
    task = cancellable(doc, actor.id)
    if task is None:
        raise FlowError("처리를 취소할 수 없습니다.", stale=True)
    touch(doc)
    changed = waiting_ids(doc) | {actor.id}
    if task.step_no < doc.item.current_step:
        for row in doc.current_tasks:
            row.status = "voided"
        doc.item.current_step = task.step_no
    task.status = "waiting"
    task.decision = None
    task.acted_at = None
    for row in doc.current_tasks:
        # 먼저 완료한 사람 때문에 빠졌던 담당자도 다시 차례를 받는다.
        if row.status == "skipped":
            row.status = "waiting"
            row.acted_at = None
            changed.add(row.assignee_id)
    audit(session, doc, actor, "cancel", step=doc.steps[task.step_no]["label"])
    await session.flush()
    rebuild_body(doc)
    return changed


def can_withdraw(doc: FlowDoc, user_id: int) -> bool:
    if doc.item.registrant_id != user_id:
        return False
    if doc.item.status == "returned":
        return True
    handled = any(row.status in ("done", "returned", "rejected") for row in doc.tasks)
    return doc.item.status == "in_progress" and not handled


async def withdraw(session: AsyncSession, doc: FlowDoc, actor: User) -> set[int]:
    changed = waiting_ids(doc) | {actor.id}
    now = datetime.now(UTC)
    for row in doc.current_tasks:
        if row.status == "waiting":
            row.status = "skipped"
            row.acted_at = now
    audit(session, doc, actor, "withdraw")
    await close(session, doc, "withdrawn", actor)
    return changed


async def stalled(session: AsyncSession, doc: FlowDoc) -> bool:
    """등록자나 담당자 때문에 진행할 수 없는 건."""
    if doc.item.status == "returned":
        return not await active_ids(session, [doc.item.registrant_id])
    if doc.item.status != "in_progress":
        return False
    waiting = waiting_ids(doc)
    active = await active_ids(session, waiting)
    return waiting != active if doc.step["complete"] == "all" and waiting else not active


async def discard(session: AsyncSession, doc: FlowDoc, actor: User, reason: str) -> set[int]:
    if not await stalled(session, doc):
        raise FlowError("진행할 수 있는 건은 폐기할 수 없습니다.", stale=True)
    changed = waiting_ids(doc) | {doc.item.registrant_id}
    now = datetime.now(UTC)
    for row in doc.current_tasks:
        if row.status == "waiting":
            row.status = "skipped"
            row.acted_at = now
    audit(session, doc, actor, "discard", reason=reason)
    await close(session, doc, "discarded", actor, reason)
    return changed


async def reassign_candidates(session: AsyncSession, doc: FlowDoc) -> set[int]:
    if doc.item.status != "in_progress":
        return set()
    taken = {row.assignee_id for row in doc.current_tasks}
    allowed = await step_candidates(session, doc.channel, doc.step)
    return allowed - taken - doc.excluded(doc.item.current_step)


async def reassign(
    session: AsyncSession,
    doc: FlowDoc,
    actor: User,
    old_id: int | None,
    new_id: int,
    reason: str,
) -> set[int]:
    """아직 처리하지 않은 담당자를 바꾼다. old_id가 없으면 더한다."""
    if new_id not in await reassign_candidates(session, doc):
        raise FlowError("그 단계를 맡을 수 없는 사람입니다.")
    now = touch(doc)
    changed = {new_id}
    old = None
    if old_id is not None:
        old = doc.my_task(old_id)
        if old is None:
            raise FlowError("이미 처리했거나 담당자가 아닙니다.", stale=True)
        old.status = "replaced"
        old.acted_at = now
        old.comment = reason
        changed.add(old_id)
        await session.flush()
    elif not any(row.status == "waiting" for row in doc.current_tasks) and any(
        row.status == "done" for row in doc.current_tasks
    ):
        raise FlowError("이미 끝난 단계입니다.", stale=True)
    task = FlowTask(
        item_id=doc.item.id,
        round=doc.item.round,
        step_no=doc.item.current_step,
        assignee_id=new_id,
        status="waiting",
        values={},
    )
    session.add(task)
    doc.tasks.append(task)
    if is_picked(doc.step):
        # 되돌아와 다시 할 때도 새 담당자가 맡는다.
        key = doc.step["key"]
        people = [one for one in doc.picks.get(key, []) if one != old_id]
        await save_picks(session, doc, {**doc.picks, key: [*people, new_id]})
    audit(
        session,
        doc,
        actor,
        "reassign",
        step=doc.step["label"],
        before=[old_id] if old_id else [],
        after=[new_id],
        reason=reason,
    )
    await session.flush()
    return changed


# 쓰레드의 추가 내용


async def participants(session: AsyncSession, doc: FlowDoc) -> set[int]:
    """등록자와 담당자. 차례가 오기 전과 이전 회차 담당자도 든다."""
    people = {doc.item.registrant_id}
    people |= {row.assignee_id for row in doc.tasks if row.status != "replaced"}
    people |= {one for rows in doc.picks.values() for one in rows}
    listed = await listed_people(session, doc.channel.id)
    for step in doc.steps:
        if step.get("assign") == "fixed":
            people |= set(listed.get(step["key"], []))
    return people


async def last_note_id(session: AsyncSession, item_id: int) -> int:
    return (
        await session.scalar(
            select(func.max(FlowReply.post_id)).where(FlowReply.item_id == item_id)
        )
    ) or 0


async def unseen_notes(session: AsyncSession, item_id: int, seen: int, user_id: int) -> bool:
    found = await session.scalar(
        select(FlowReply.post_id)
        .join(Post, Post.id == FlowReply.post_id)
        .where(FlowReply.item_id == item_id, FlowReply.post_id > seen, Post.author_id != user_id)
        .limit(1)
    )
    return found is not None


def add_note(session: AsyncSession, doc: FlowDoc, reply: Post, actor: User) -> None:
    session.add(
        FlowReply(
            post_id=reply.id,
            item_id=doc.item.id,
            round=doc.item.round,
            step_no=doc.item.current_step,
            item_status=doc.item.status,
        )
    )
    touch(doc)
    audit(session, doc, actor, "note", step=doc.step["label"], reply_id=reply.id)


# 결재 연계


def raisers(doc: FlowDoc) -> set[int]:
    """최종 확정된 건의 등록자와, 단계를 실제로 완료한 처리자·검토자·확정자."""
    if doc.item.status != "done":
        return set()
    return {doc.item.registrant_id} | {
        task.assignee_id for rows in doc.completions().values() for task in rows
    }


async def live_approval(session: AsyncSession, item_id: int) -> bool:
    found = await session.scalar(
        select(FlowApproval.approval_id)
        .join(Approval, Approval.id == FlowApproval.approval_id)
        .join(Post, Post.id == Approval.post_id)
        .where(
            FlowApproval.item_id == item_id,
            Approval.status.in_(LIVE_APPROVALS),
            Post.deleted_at.is_(None),
        )
        .limit(1)
    )
    return found is not None


async def approval_targets(session: AsyncSession, channel_id: int) -> list[tuple[Channel, bool]]:
    """설정된 결재 채널. 기본 채널이 맨 앞이다."""
    rows = await session.execute(
        select(Channel, FlowApprovalTarget.is_default)
        .join(FlowApprovalTarget, FlowApprovalTarget.target_channel_id == Channel.id)
        .where(FlowApprovalTarget.channel_id == channel_id)
        .order_by(FlowApprovalTarget.is_default.desc(), Channel.name)
    )
    return [(channel, default) for channel, default in rows]


async def check_raise(
    session: AsyncSession, doc: FlowDoc, user_id: int, target_channel_id: int
) -> None:
    if user_id not in raisers(doc):
        raise FlowError("이 건을 결재로 올릴 수 없습니다.")
    if await session.get(FlowApprovalTarget, (doc.channel.id, target_channel_id)) is None:
        raise FlowError("이 채널에서 결재를 올릴 수 없는 결재 채널입니다.")
    if await live_approval(session, doc.item.id):
        raise FlowError("이미 올린 결재가 있습니다.", stale=True)


# 글


def rebuild_body(doc: FlowDoc) -> None:
    """검색에 쓰는 본문. 마크다운으로 해석되지 않게 특수 문자를 막는다."""
    parts: list[str] = []

    def collect(fields: Iterable[dict], values: Mapping) -> None:
        for item in fields:
            value = values.get(item["key"])
            if item["kind"] in ("text", "markdown", "select", "number", "date") and value:
                parts.append(f"{item['label']}: {value}")
            elif item["kind"] == "multiselect" and value:
                parts.append(f"{item['label']}: {', '.join(value)}")

    register = [item for item in doc.steps[0]["fields"] if item["key"] != "title"]
    for values, _ in doc.registrations():
        collect(register, values)
    for number, rows in sorted(doc.completions().items()):
        for task in rows:
            collect(doc.steps[number]["fields"], task.values)
    doc.post.body_md = MARKDOWN_SPECIAL.sub(
        r"\\\1", " · ".join(" ".join(part.split()) for part in parts)
    )
