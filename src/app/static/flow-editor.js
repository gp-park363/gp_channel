// 워크플로우 설정의 단계·입력 항목 편집기. 값은 서버가 다시 검사한다. 문구는 textContent로만 넣는다.
(function () {
  const form = document.getElementById("flow-editor");
  if (!form) return;
  const { attachPeopleSearch, personText, iconButton, spacer } = window.baroPeople;
  const data = JSON.parse(document.getElementById("flow-data").textContent);
  const { limits } = data;
  const list = form.querySelector(".flow-editor-steps");
  let steps = data.steps;
  let dirty = false;

  function make(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    Object.assign(node, props);
    node.append(...children);
    return node;
  }

  function changed() {
    dirty = true;
  }

  function redraw() {
    changed();
    draw();
  }

  function textInput(target, name, label, props = {}) {
    const input = make("input", { value: target[name] == null ? "" : target[name], ...props });
    input.setAttribute("aria-label", label);
    if (!input.placeholder) input.placeholder = label;
    input.addEventListener("input", () => { target[name] = input.value; changed(); });
    return input;
  }

  function choice(target, name, label, options, after) {
    const select = make("select", {}, ...Object.entries(options).map(([value, text]) =>
      make("option", { value, textContent: text })));
    select.value = target[name];
    select.setAttribute("aria-label", label);
    select.addEventListener("change", () => {
      target[name] = select.value;
      changed();
      if (after) after();
    });
    return select;
  }

  function check(target, name, label) {
    const box = make("input", { type: "checkbox", checked: Boolean(target[name]) });
    box.addEventListener("change", () => { target[name] = box.checked; changed(); });
    return make("label", { className: "check" }, box, label);
  }

  function swap(rows, index, other) {
    [rows[index], rows[other]] = [rows[other], rows[index]];
    redraw();
  }

  function fieldRow(step, field, index) {
    const rows = step.fields;
    const first = step.kind === "register" ? 1 : 0;
    const row = make("li", { className: "flow-item" });
    if (field.key === "title") {
      row.append(make("div", { className: "flow-item-head" },
        make("strong", { textContent: "제목" }), make("span", { className: "muted", textContent: "한 줄 글 · 필수 · 뺄 수 없음" })));
      return row;
    }
    row.append(make("div", { className: "flow-item-head" },
      textInput(field, "label", "항목 이름", { maxLength: 50 }),
      choice(field, "kind", "항목 종류", data.fieldKinds, redraw),
      check(field, "required", "필수"),
      iconButton("▲", `${field.label} 위로`, index <= first, () => swap(rows, index, index - 1)),
      iconButton("▼", `${field.label} 아래로`, index === rows.length - 1, () => swap(rows, index, index + 1)),
      iconButton("빼기", `${field.label} 빼기`, false, () => { rows.splice(index, 1); redraw(); }),
    ));
    const more = make("div", { className: "flow-item-more" });
    more.append(textInput(field, "help", "도움말 (선택)", { maxLength: 200, className: "wide" }));
    if (field.kind === "select" || field.kind === "multiselect") {
      const area = make("textarea", { rows: 3, value: (field.options || []).join("\n"), placeholder: "선택지를 한 줄에 하나씩", className: "wide" });
      area.setAttribute("aria-label", "선택지");
      area.addEventListener("input", () => { field.options = area.value.split("\n").map((one) => one.trim()).filter(Boolean); changed(); });
      more.append(area);
    } else if (field.kind === "number") {
      more.append(
        textInput(field, "unit", "단위", { maxLength: 20 }),
        textInput(field, "min", "최소", { inputMode: "decimal" }),
        textInput(field, "max", "최대", { inputMode: "decimal" }),
        textInput(field, "decimals", "소수 자릿수", { type: "number", min: 0, max: 6 }),
      );
    } else if (field.kind === "person") {
      more.append(check(field, "multiple", "여러 명 허용"));
    } else if (field.kind === "markdown") {
      const area = make("textarea", { rows: 2, value: field.template || "", placeholder: "기본 형식 (선택)", className: "wide" });
      area.setAttribute("aria-label", "기본 형식");
      area.addEventListener("input", () => { field.template = area.value; changed(); });
      more.append(area);
    }
    row.append(more);
    return row;
  }

  function peopleBox(step) {
    step.people = step.people || [];
    const rows = make("ul", { className: "line-steps" });
    const input = make("input", { type: "search", autocomplete: "off", placeholder: "이름, 이메일, 소속으로 추가" });
    input.setAttribute("aria-label", `${step.label} 사람 추가`);
    const search = make("div", { className: "line-search" }, input);
    function drawPeople() {
      rows.replaceChildren(...step.people.map((person, index) => make("li", {}, ...personText(person), spacer(),
        iconButton("빼기", `${person.name} 빼기`, false, () => { step.people.splice(index, 1); changed(); drawPeople(); }))));
    }
    attachPeopleSearch(search, form.dataset.people, () => step.people, (person) => {
      if (step.people.length < limits.people) step.people.push(person);
      changed();
      drawPeople();
    });
    drawPeople();
    return make("div", { className: "people-picker" }, rows, search);
  }

  function stepRow(step, index) {
    const last = index === steps.length - 1;
    const middle = index > 0 && !last;
    const head = make("div", { className: "flow-step-head" },
      make("b", { textContent: String(index + 1) }),
      make("span", { className: "tag blue", textContent: data.stepKinds[step.kind] }),
      textInput(step, "label", "단계 이름", { maxLength: 50 }));
    if (middle) {
      head.append(
        iconButton("▲", `${step.label} 위로`, index <= 1, () => swap(steps, index, index - 1)),
        iconButton("▼", `${step.label} 아래로`, index >= steps.length - 2, () => swap(steps, index, index + 1)),
        iconButton("빼기", `${step.label} 빼기`, false, () => { steps.splice(index, 1); redraw(); }));
    } else {
      head.append(make("span", { className: "muted", textContent: index === 0 ? "맨 처음 고정" : "맨 마지막 고정" }));
    }
    const row = make("li", { className: "flow-step" }, head);
    if (index > 0) {
      const controls = make("div", { className: "row" }, choice(step, "assign", "담당자를 정하는 방법", data.assignKinds, redraw));
      if (step.kind === "confirm" && middle) {
        controls.append(make("span", { className: "muted", textContent: "중간 확정: 먼저 판단한 한 명이 마무리하거나 다음 단계로 보냅니다." }));
      } else {
        controls.append(choice(step, "complete", "완료 조건", { any: "한 명이 완료", all: "전원이 완료" }));
      }
      row.append(controls);
      if (step.assign === "fixed" || step.assign === "listed") row.append(peopleBox(step));
      if (step.kind === "confirm" && step.assign === "registrant") {
        row.append(make("p", { className: "flow-warning", textContent: "등록자가 채널 사용자 누구에게나 확정을 받을 수 있습니다. 확정자가 정해져 있으면 명단이나 고정 지정을 쓰세요." }));
      }
    }
    if (step.kind !== "confirm") {
      row.append(make("ul", { className: "flow-items" }, ...step.fields.map((field, at) => fieldRow(step, field, at))));
      const add = make("button", { type: "button", className: "link", textContent: "+ 항목 추가" });
      add.disabled = step.fields.length >= limits.fields;
      add.addEventListener("click", () => {
        step.fields.push({ kind: "text", label: "", required: false, help: "" });
        redraw();
        const names = list.children[index].querySelectorAll(".flow-item-head input:not([type=checkbox])");
        names[names.length - 1].focus();
      });
      row.append(make("div", {}, add));
    }
    return row;
  }

  function draw() {
    list.replaceChildren(...steps.map(stepRow));
    form.querySelectorAll("[data-add-step]").forEach((button) => { button.disabled = steps.length >= limits.steps; });
  }

  form.querySelectorAll("[data-add-step]").forEach((button) => {
    button.addEventListener("click", () => {
      const kind = button.dataset.addStep;
      const label = kind === "confirm" ? "중간 확정" : data.stepKinds[kind];
      const same = steps.slice(0, -1).filter((step) => step.kind === kind).length;
      steps.splice(steps.length - 1, 0, {
        kind, label: same ? `${label} ${same + 1}` : label, assign: "registrant", complete: "any", fields: [], people: [],
      });
      redraw();
    });
  });

  // 저장과 발행은 편집 내용을 한 덩어리로 보낸다.
  document.body.addEventListener("htmx:configRequest", (event) => {
    if (!form.contains(event.detail.elt)) return;
    const payload = steps.map((step) => ({ ...step, people: (step.people || []).map((person) => person.id) }));
    event.detail.parameters.set
      ? event.detail.parameters.set("data", JSON.stringify({ steps: payload }))
      : (event.detail.parameters.data = JSON.stringify({ steps: payload }));
  });
  document.body.addEventListener("htmx:afterRequest", (event) => {
    if (form.contains(event.detail.elt) && event.detail.successful) dirty = false;
  });
  form.addEventListener("submit", (event) => event.preventDefault());
  window.addEventListener("beforeunload", (event) => { if (dirty) event.preventDefault(); });
  draw();
})();
