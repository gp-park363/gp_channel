"""워크플로우 정의(단계와 입력 항목)의 규칙과 입력 값 검사. DB를 쓰지 않는다."""

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

STEP_KINDS = {"register": "등록", "process": "처리", "review": "검토", "confirm": "확정"}
ASSIGN_KINDS = {
    "fixed": "고정 지정",
    "registrant": "등록자 지정(채널 사용자)",
    "listed": "등록자 지정(명단)",
}
COMPLETE_KINDS = {"any": "한 명", "all": "전원"}
FIELD_KINDS = {
    "text": "한 줄 글",
    "markdown": "여러 줄 글",
    "number": "숫자",
    "date": "날짜",
    "select": "선택",
    "multiselect": "여러 개 선택",
    "check": "체크",
    "file": "파일",
    "person": "사람",
}
DONE_LABELS = {"process": "처리 완료", "review": "검토 완료", "confirm": "확정"}
ROLE_LABELS = {"process": "처리자", "review": "검토자", "confirm": "확정자"}

MAX_STEPS = 12
MAX_FIELDS = 20
MAX_OPTIONS = 50
MAX_PEOPLE = 20
MAX_LABEL = 50
MAX_HELP = 200
TEXT_LENGTH = 200
MARKDOWN_LENGTH = 10_000
NUMBER_DIGITS = 15
KEY = re.compile(r"^[sf][0-9]{1,6}$")
TITLE_KEY = "title"

Step = dict
Values = dict


class FlowError(ValueError):
    """사용자에게 그대로 보여 줄 문구를 담는다. stale은 그 사이 상황이 바뀐 경우다."""

    def __init__(self, message: str, stale: bool = False) -> None:
        super().__init__(message)
        self.stale = stale


def default_steps() -> list[Step]:
    return [
        {
            "key": "s1",
            "kind": "register",
            "label": "등록",
            "fields": [
                {"key": TITLE_KEY, "kind": "text", "label": "제목", "required": True},
                {"key": "f1", "kind": "markdown", "label": "내용", "required": True},
                {"key": "f2", "kind": "file", "label": "첨부", "required": False},
            ],
        },
        {
            "key": "s2",
            "kind": "process",
            "label": "처리",
            "assign": "registrant",
            "complete": "any",
            "fields": [
                {"key": "f3", "kind": "markdown", "label": "처리 내용", "required": True},
                {"key": "f4", "kind": "file", "label": "결과 파일", "required": False},
            ],
        },
        {
            "key": "s3",
            "kind": "review",
            "label": "검토",
            "assign": "registrant",
            "complete": "all",
            "fields": [
                {"key": "f5", "kind": "markdown", "label": "검토 의견", "required": True},
            ],
        },
        {
            "key": "s4",
            "kind": "confirm",
            "label": "확정",
            "assign": "registrant",
            "complete": "any",
            "fields": [],
        },
    ]


def has_list(step: Step) -> bool:
    """채널에 사람을 등록해 두는 단계인지."""
    return step.get("assign") in ("fixed", "listed")


def is_picked(step: Step) -> bool:
    """등록자가 담당자를 지정하는 단계인지."""
    return step.get("assign") in ("registrant", "listed")


def is_middle_confirm(steps: Sequence[Step], step_no: int) -> bool:
    return steps[step_no]["kind"] == "confirm" and step_no < len(steps) - 1


class Keys:
    """항목과 단계의 번호. 한 번 쓴 번호는 다른 뜻으로 다시 쓰지 않는다."""

    def __init__(self, known: Iterable[Sequence[Step]]) -> None:
        self.steps: dict[str, Step] = {}
        self.fields: dict[str, dict] = {}
        self._last = 0
        for steps in known:
            for step in steps:
                self.steps[step["key"]] = step
                self._seen(step["key"])
                for item in step.get("fields", []):
                    self.fields[item["key"]] = item
                    self._seen(item["key"])

    def _seen(self, key: str) -> None:
        if KEY.match(key):
            self._last = max(self._last, int(key[1:]))

    def new(self, prefix: str) -> str:
        self._last += 1
        return f"{prefix}{self._last}"


def _text(raw: Mapping, name: str, limit: int, what: str) -> str:
    value = raw.get(name) or ""
    if not isinstance(value, str):
        raise FlowError("설정 형식이 맞지 않습니다.")
    value = " ".join(value.split())
    if len(value) > limit:
        raise FlowError(f"{what}: {limit}자까지 쓸 수 있습니다.")
    return value


def _number(value: object, what: str) -> str | None:
    """숫자는 소수 오차가 없게 글자로 둔다."""
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as error:
        raise FlowError(f"{what}: 숫자로 적어 주세요.") from error
    if not number.is_finite() or len(number.as_tuple().digits) > NUMBER_DIGITS:
        raise FlowError(f"{what}: 숫자가 너무 큽니다.")
    if abs(number.adjusted()) > NUMBER_DIGITS:
        raise FlowError(f"{what}: 숫자가 너무 큽니다.")
    return format(number, "f")


def _clean_field(raw: object, keys: Keys, used: set[str], where: str) -> dict:
    if not isinstance(raw, Mapping) or raw.get("kind") not in FIELD_KINDS:
        raise FlowError("설정 형식이 맞지 않습니다.")
    kind = raw["kind"]
    label = _text(raw, "label", MAX_LABEL, f"{where} 항목 이름")
    if not label:
        raise FlowError(f"{where}: 항목 이름을 적어 주세요.")
    where = f"{where} '{label}'"
    key = raw.get("key")
    known = keys.fields.get(key) if isinstance(key, str) and key != TITLE_KEY else None
    # 종류가 바뀌면 예전 값을 이어받지 않도록 새 번호를 준다.
    if known is None or known["kind"] != kind or key in used:
        key = keys.new("f")
    used.add(key)
    result: dict = {
        "key": key,
        "kind": kind,
        "label": label,
        "required": bool(raw.get("required")),
        "help": _text(raw, "help", MAX_HELP, f"{where} 도움말"),
    }
    if kind in ("select", "multiselect"):
        options = raw.get("options") or []
        if not isinstance(options, list):
            raise FlowError("설정 형식이 맞지 않습니다.")
        cleaned = list(dict.fromkeys(" ".join(str(item).split()) for item in options))
        cleaned = [item for item in cleaned if item]
        if len(cleaned) > MAX_OPTIONS or any(len(item) > TEXT_LENGTH for item in cleaned):
            raise FlowError(f"{where}: 선택지는 {MAX_OPTIONS}개, {TEXT_LENGTH}자까지입니다.")
        result["options"] = cleaned
    elif kind == "number":
        result["unit"] = _text(raw, "unit", 20, f"{where} 단위")
        low = _number(raw.get("min"), f"{where} 최소")
        high = _number(raw.get("max"), f"{where} 최대")
        if low is not None and high is not None and Decimal(low) > Decimal(high):
            raise FlowError(f"{where}: 최소가 최대보다 큽니다.")
        try:
            decimals = int(raw.get("decimals") or 0)
        except (TypeError, ValueError) as error:
            raise FlowError(f"{where}: 소수 자릿수를 확인해 주세요.") from error
        if not 0 <= decimals <= 6:
            raise FlowError(f"{where}: 소수 자릿수는 6까지입니다.")
        result |= {"min": low, "max": high, "decimals": decimals}
    elif kind == "person":
        result["multiple"] = bool(raw.get("multiple"))
    elif kind == "markdown":
        template = raw.get("template") or ""
        if not isinstance(template, str) or len(template) > MARKDOWN_LENGTH:
            raise FlowError(f"{where}: 기본 형식이 너무 깁니다.")
        result["template"] = template.replace("\r\n", "\n")
    return result


def clean_steps(
    raw: object, keys: Keys, published: Sequence[Step] | None = None
) -> tuple[list[Step], dict[str, list[int]]]:
    """화면에서 온 정의를 정리한다. 단계별 담당자(명단)는 따로 돌려준다.

    담당자를 정하는 방법이나 종류를 바꾼 단계에는 새 번호를 준다.
    이전 판으로 진행 중인 건의 담당자와 섞이지 않게 하기 위해서다.
    """
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_STEPS:
        raise FlowError(f"단계는 등록과 확정을 포함해 2~{MAX_STEPS}개입니다.")
    if not all(isinstance(item, Mapping) and item.get("kind") in STEP_KINDS for item in raw):
        raise FlowError("설정 형식이 맞지 않습니다.")
    if raw[0]["kind"] != "register" or any(item["kind"] == "register" for item in raw[1:]):
        raise FlowError("등록은 맨 처음에 하나만 둡니다.")
    if raw[-1]["kind"] != "confirm":
        raise FlowError("마지막 단계는 확정이어야 합니다.")
    live = {step["key"]: step for step in published or []}
    used_steps: set[str] = set()
    used_fields: set[str] = set()
    steps: list[Step] = []
    people: dict[str, list[int]] = {}
    last = len(raw) - 1
    for number, item in enumerate(raw):
        kind = item["kind"]
        label = _text(item, "label", MAX_LABEL, "단계 이름") or STEP_KINDS[kind]
        key = item.get("key")
        known = keys.steps.get(key) if isinstance(key, str) else None
        step: Step = {"key": "", "kind": kind, "label": label, "fields": []}
        if kind != "register":
            assign = item.get("assign")
            complete = item.get("complete")
            if assign not in ASSIGN_KINDS or complete not in COMPLETE_KINDS:
                raise FlowError("설정 형식이 맞지 않습니다.")
            # 중간 확정은 먼저 판단한 한 사람의 선택으로 정한다.
            if kind == "confirm" and number < last:
                complete = "any"
            step |= {"assign": assign, "complete": complete}
        changed = key in live and (
            live[key]["kind"] != kind or live[key].get("assign") != step.get("assign")
        )
        if known is None or known["kind"] != kind or key in used_steps or changed:
            key = keys.new("s")
        used_steps.add(key)
        step["key"] = key
        fields = item.get("fields") or []
        if not isinstance(fields, list):
            raise FlowError("설정 형식이 맞지 않습니다.")
        if kind == "confirm":
            fields = []
        if len(fields) > MAX_FIELDS:
            raise FlowError(f"{label}: 항목은 {MAX_FIELDS}개까지입니다.")
        if kind == "register":
            others = [
                row
                for row in fields
                if not (isinstance(row, Mapping) and row.get("key") == TITLE_KEY)
            ]
            step["fields"].append(
                {"key": TITLE_KEY, "kind": "text", "label": "제목", "required": True}
            )
            fields = others
        step["fields"] += [_clean_field(row, keys, used_fields, label) for row in fields]
        steps.append(step)
        if kind != "register" and has_list(step):
            chosen = item.get("people") or []
            if not isinstance(chosen, list) or not all(type(one) is int for one in chosen):
                raise FlowError("설정 형식이 맞지 않습니다.")
            chosen = list(dict.fromkeys(chosen))
            if len(chosen) > MAX_PEOPLE:
                raise FlowError(f"{label}: 담당자는 {MAX_PEOPLE}명까지입니다.")
            people[key] = chosen
    return steps, people


def check_publish(steps: Sequence[Step], active: Mapping[str, Sequence[int]]) -> None:
    """active는 단계 번호별로 채널에 등록된 활성 사용자다."""
    for step in steps:
        for item in step["fields"]:
            if item["kind"] in ("select", "multiselect") and not item["options"]:
                raise FlowError(f"{step['label']} '{item['label']}': 선택지를 적어 주세요.")
        if has_list(step) and not active.get(step["key"]):
            what = "담당자" if step["assign"] == "fixed" else "명단에 사람"
            particle = "를" if what == "담당자" else "을"
            raise FlowError(f"{step['label']}: {what}{particle} 한 명 이상 등록해 주세요.")


# 입력 값


@dataclass
class FormData:
    """화면에서 온 입력. uploads는 새로 올린 파일 수, kept는 남긴 첨부 번호다."""

    texts: Mapping[str, list[str]]
    uploads: Mapping[str, int] = field(default_factory=dict)
    kept: Mapping[str, list[int]] = field(default_factory=dict)


def _ids(values: Iterable[str], what: str) -> list[int]:
    items = list(dict.fromkeys(value for value in values if value))
    if not all(value.isascii() and value.isdigit() and len(value) < 10 for value in items):
        raise FlowError(f"{what}: 입력값을 확인해 주세요.")
    return [int(value) for value in items]


def parse_values(
    fields: Sequence[dict], data: FormData, *, require: bool
) -> tuple[Values, set[int]]:
    """항목의 값과 고른 사람 번호를 돌려준다. 파일 값은 남긴 첨부 번호까지만 담는다.

    require가 False면(임시저장) 필수 검사만 건너뛰고 형식은 똑같이 검사한다.
    """
    values: Values = {}
    people: set[int] = set()
    for item in fields:
        key, kind, label = item["key"], item["kind"], item["label"]
        given = [value.strip() for value in data.texts.get(key, [])]
        first = given[0] if given else ""
        empty = not first
        value: object = None
        if kind == "text":
            value = " ".join(first.split())
            if len(value) > TEXT_LENGTH:
                raise FlowError(f"{label}: {TEXT_LENGTH}자까지 쓸 수 있습니다.")
        elif kind == "markdown":
            value = first.replace("\r\n", "\n")
            if len(value) > MARKDOWN_LENGTH:
                raise FlowError(f"{label}: {MARKDOWN_LENGTH:,}자까지 쓸 수 있습니다.")
        elif kind == "number":
            value = _number(first.replace(",", ""), label)
            if value is not None:
                number = Decimal(value)
                if -number.normalize().as_tuple().exponent > item.get("decimals", 0):
                    raise FlowError(f"{label}: 소수는 {item.get('decimals', 0)}자리까지입니다.")
                if item.get("min") is not None and number < Decimal(item["min"]):
                    raise FlowError(f"{label}: {item['min']} 이상이어야 합니다.")
                if item.get("max") is not None and number > Decimal(item["max"]):
                    raise FlowError(f"{label}: {item['max']} 이하여야 합니다.")
        elif kind == "date":
            if first:
                try:
                    value = date.fromisoformat(first).isoformat()
                except ValueError as error:
                    raise FlowError(f"{label}: 날짜를 확인해 주세요.") from error
        elif kind == "select":
            if first and first not in item["options"]:
                raise FlowError(f"{label}: 선택지에서 골라 주세요.")
            value = first
        elif kind == "multiselect":
            chosen = list(dict.fromkeys(one for one in given if one))
            if any(one not in item["options"] for one in chosen):
                raise FlowError(f"{label}: 선택지에서 골라 주세요.")
            value, empty = chosen, not chosen
        elif kind == "check":
            value, empty = bool(first), not first
        elif kind == "person":
            chosen_people = _ids(given, label)
            limit = MAX_PEOPLE if item.get("multiple") else 1
            if len(chosen_people) > limit:
                raise FlowError(f"{label}: {limit}명까지 고를 수 있습니다.")
            people |= set(chosen_people)
            value, empty = chosen_people, not chosen_people
        elif kind == "file":
            kept = list(dict.fromkeys(data.kept.get(key, [])))
            value, empty = kept, not kept and not data.uploads.get(key)
        if require and item["required"] and empty:
            raise FlowError(f"'{label}'은(는) 필수 항목입니다.")
        if value not in (None, "", [], False):
            values[key] = value
    return values, people


def carry_values(old: Sequence[dict], new: Sequence[dict], values: Values) -> Values:
    """판이 바뀌어도 번호와 종류가 같은 항목의 값은 이어 쓴다."""
    kinds = {item["key"]: item["kind"] for item in old}
    return {
        item["key"]: values[item["key"]]
        for item in new
        if item["key"] in values and kinds.get(item["key"]) == item["kind"]
    }


def dropped_labels(old: Sequence[dict], new: Sequence[dict], values: Values) -> list[str]:
    kept = carry_values(old, new, values)
    return [item["label"] for item in old if item["key"] in values and item["key"] not in kept]


def pages(total: int, size: int) -> int:
    return max(math.ceil(total / size), 1)
