// 알림 구독. 모든 화면에서 이미 허용한 기기의 구독을 서버에 맞추고, 알림 화면의 버튼을 맡는다.
(() => {
  const frame = document.querySelector("[data-push-user]");
  if (!frame) return;
  const user = frame.dataset.pushUser;
  const publicKey = frame.dataset.pushKey;
  const DEVICE = "baro-push-device";
  let endpoint = "";

  function supported() {
    return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  }

  function standalone() {
    return matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  }

  function isApple() {
    return /iPhone|iPad|iPod/.test(navigator.userAgent)
      || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  }

  function keyBytes(text) {
    const raw = atob(text.replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (char) => char.charCodeAt(0));
  }

  function sameKey(subscription) {
    const used = subscription.options && subscription.options.applicationServerKey;
    if (!used) return true;
    const wanted = keyBytes(publicKey);
    const bytes = new Uint8Array(used);
    return bytes.length === wanted.length && bytes.every((value, index) => value === wanted[index]);
  }

  function headers() {
    const base = JSON.parse(document.body.getAttribute("hx-headers") || "{}");
    return { ...base, "Content-Type": "application/json" };
  }

  async function post(url, body) {
    const response = await fetch(url, { method: "POST", headers: headers(), body: JSON.stringify(body) });
    // 로그인이 풀리면 로그인 화면으로 옮겨져 JSON이 아닌 답이 온다.
    if (response.redirected) throw new Error("로그인이 풀렸습니다. 다시 로그인해 주세요.");
    const data = response.status === 204 ? {} : await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "요청 오류");
    return data;
  }

  async function current() {
    if (!supported()) return null;
    const registration = await navigator.serviceWorker.getRegistration("/");
    return registration ? registration.pushManager.getSubscription() : null;
  }

  async function save(subscription) {
    const data = await post("/account/push/subscribe", subscription.toJSON());
    localStorage.setItem(DEVICE, String(data.id));
    sessionStorage.setItem(`baro-push-synced:${user}`, subscription.endpoint);
    return data.id;
  }

  // 키가 바뀌었거나 구독이 사라졌으면 다시 만든다. 다른 사람이 쓰던 기기면 이 사람 것으로 옮긴다.
  async function sync() {
    if (!supported() || Notification.permission !== "granted") return;
    let subscription = await current();
    if (subscription && !sameKey(subscription)) {
      await subscription.unsubscribe();
      subscription = null;
    }
    if (!subscription) {
      const registration = await navigator.serviceWorker.getRegistration("/");
      if (!registration) return;
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true, applicationServerKey: keyBytes(publicKey),
      });
    }
    endpoint = subscription.endpoint;
    if (sessionStorage.getItem(`baro-push-synced:${user}`) !== endpoint) await save(subscription);
  }

  // 로그아웃하면 이 기기로 이 사람의 알림이 가지 않게 서버의 구독을 지운다.
  document.body.addEventListener("htmx:configRequest", (event) => {
    if (event.detail.path !== "/logout") return;
    if (endpoint) event.detail.parameters.push_endpoint = endpoint;
    sessionStorage.removeItem(`baro-push-synced:${user}`);
  });

  const root = document.getElementById("push-settings");
  sync().catch(() => { /* 알림 화면에서 다시 시도한다. */ }).finally(() => { if (root) show(); });
  if (!root) return;

  const out = document.getElementById("push-log");
  const state = document.getElementById("push-state");
  const summary = document.getElementById("push-summary");

  function log(text) {
    out.textContent = `${new Date().toLocaleTimeString("ko-KR")} ${text}\n${out.textContent}`;
  }

  async function show() {
    const subscription = await current().catch(() => null);
    const permission = "Notification" in window ? Notification.permission : "없음";
    if (!supported()) {
      summary.textContent = isApple() && !standalone()
        ? "홈 화면에 추가한 앱에서만 알림을 받을 수 있습니다."
        : "이 브라우저는 알림을 지원하지 않습니다.";
    } else if (permission === "denied") {
      summary.textContent = "알림이 차단되어 있습니다. 브라우저나 휴대폰 설정에서 이 사이트의 알림을 허용해 주세요.";
    } else {
      summary.textContent = subscription && permission === "granted"
        ? "이 기기로 알림을 받습니다."
        : "이 기기는 알림을 받지 않습니다.";
    }
    document.getElementById("push-ios").hidden = !(isApple() && !standalone());
    const mine = localStorage.getItem(DEVICE);
    document.querySelectorAll("[data-device]").forEach((row) => {
      row.querySelector(".js-this-device").hidden = !(subscription && row.dataset.device === mine);
    });
    const rows = [
      ["접속 주소", location.origin],
      ["보안 연결(secure context)", window.isSecureContext ? "예" : "아니오"],
      ["홈 화면 앱으로 실행", standalone() ? "예" : "아니오"],
      ["서비스 워커", "serviceWorker" in navigator ? "지원" : "없음"],
      ["푸시", "PushManager" in window ? "지원" : "없음"],
      ["알림 권한", permission],
      ["구독", subscription ? new URL(subscription.endpoint).host : "없음"],
      ["브라우저", navigator.userAgent],
    ];
    state.replaceChildren(...rows.map(([name, value]) => {
      const item = document.createElement("li");
      const label = document.createElement("strong");
      label.textContent = `${name}: `;
      item.append(label, value);
      return item;
    }));
  }

  function refreshDevices() {
    return htmx.ajax("GET", "/account/push/devices", { target: "#push-devices", swap: "innerHTML" });
  }

  async function turnOn() {
    if (!supported()) throw new Error("이 브라우저는 알림을 지원하지 않습니다.");
    // iOS는 버튼을 누른 흐름 안에서만 권한을 묻는다. 다른 일보다 먼저 부른다.
    const permission = await Notification.requestPermission();
    log(`권한: ${permission}`);
    if (permission !== "granted") return;
    await navigator.serviceWorker.register("/sw.js", { scope: "/", updateViaCache: "none" });
    await navigator.serviceWorker.ready;
    sessionStorage.removeItem(`baro-push-synced:${user}`);
    await sync();
    log(`켜짐: ${new URL(endpoint).host}`);
    await refreshDevices();
  }

  async function turnOff() {
    const subscription = await current();
    if (subscription) {
      await post("/account/push/unsubscribe", { endpoint: subscription.endpoint });
      await subscription.unsubscribe();
    }
    endpoint = "";
    sessionStorage.removeItem(`baro-push-synced:${user}`);
    log("이 기기의 알림을 껐습니다.");
    await refreshDevices();
  }

  async function test(delay) {
    const subscription = await current();
    if (!subscription) throw new Error("먼저 이 기기에서 알림을 켜 주세요.");
    const data = await post("/account/push/test", { subscription: subscription.toJSON(), delay });
    if (data.scheduled) log(`${data.scheduled}초 뒤에 보냅니다. 화면을 잠그거나 앱을 닫아 보세요.`);
    else log(data.ok ? `푸시 서비스가 받음(${data.status})` : `실패 ${data.status} ${data.detail}`);
  }

  async function copy() {
    await navigator.clipboard.writeText(`${state.innerText}\n\n${out.textContent}`);
    log("결과를 복사했습니다.");
  }

  const actions = {
    on: turnOn, off: turnOff, now: () => test(0), soon: () => test(10), later: () => test(60), copy,
  };

  root.querySelectorAll("button[data-do]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await actions[button.dataset.do]();
    } catch (error) {
      log(`오류: ${error.name} ${error.message}`);
    }
    button.disabled = false;
    show();
  }));
  document.body.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail.target.id === "push-devices") show();
  });

  if (new URLSearchParams(location.search).get("opened")) log("알림을 눌러 이 화면을 열었습니다.");
})();
