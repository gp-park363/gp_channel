(() => {
  const root = document.getElementById("push-test");
  if (!root) return;
  const out = document.getElementById("push-log");
  const state = document.getElementById("push-state");
  const buttons = root.querySelectorAll("button[data-do]");

  function log(text) {
    out.textContent = `${new Date().toLocaleTimeString("ko-KR")} ${text}\n${out.textContent}`;
  }

  function standalone() {
    return matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  }

  function isApple() {
    return /iPhone|iPad|iPod/.test(navigator.userAgent)
      || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  }

  function supported() {
    return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  }

  async function currentSubscription() {
    if (!supported()) return null;
    const registration = await navigator.serviceWorker.getRegistration("/");
    return registration ? registration.pushManager.getSubscription() : null;
  }

  async function showState() {
    const subscription = await currentSubscription();
    const rows = [
      ["접속 주소", location.origin],
      ["보안 연결(secure context)", window.isSecureContext ? "예" : "아니오"],
      ["홈 화면 앱으로 실행", standalone() ? "예" : "아니오"],
      ["서비스 워커", "serviceWorker" in navigator ? "지원" : "없음"],
      ["푸시", "PushManager" in window ? "지원" : "없음"],
      ["알림 권한", "Notification" in window ? Notification.permission : "없음"],
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
    document.getElementById("push-ios").hidden = !(isApple() && !standalone());
  }

  function keyBytes(text) {
    const raw = atob(text.replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (char) => char.charCodeAt(0));
  }

  function sameKey(subscription) {
    const used = subscription.options && subscription.options.applicationServerKey;
    if (!used) return true;
    const wanted = keyBytes(root.dataset.key);
    const bytes = new Uint8Array(used);
    return bytes.length === wanted.length && bytes.every((value, index) => value === wanted[index]);
  }

  async function subscribe() {
    if (!supported()) throw new Error("이 브라우저는 웹 푸시를 지원하지 않습니다.");
    // iOS는 버튼을 누른 흐름 안에서만 권한을 묻는다. 다른 일보다 먼저 부른다.
    const permission = await Notification.requestPermission();
    log(`권한: ${permission}`);
    if (permission !== "granted") return;
    await navigator.serviceWorker.register("/sw.js", { scope: "/", updateViaCache: "none" });
    const registration = await navigator.serviceWorker.ready;
    let subscription = await registration.pushManager.getSubscription();
    if (subscription && !sameKey(subscription)) {
      await subscription.unsubscribe();
      subscription = null;
    }
    subscription = subscription || await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: keyBytes(root.dataset.key),
    });
    log(`구독됨: ${new URL(subscription.endpoint).host}`);
  }

  async function unsubscribe() {
    const subscription = await currentSubscription();
    if (subscription) await subscription.unsubscribe();
    log("구독을 해제했습니다.");
  }

  async function send(delay) {
    const subscription = await currentSubscription();
    if (!subscription) throw new Error("먼저 알림을 켜 주세요.");
    const headers = JSON.parse(document.body.getAttribute("hx-headers") || "{}");
    const response = await fetch("/push-test/send", {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ subscription: subscription.toJSON(), delay }),
    });
    // 로그인이 풀리면 로그인 화면으로 옮겨져 JSON이 아닌 답이 온다.
    if (response.redirected) throw new Error("로그인이 풀렸습니다. 다시 로그인해 주세요.");
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "요청 오류");
    if (data.scheduled) log(`${data.scheduled}초 뒤에 보냅니다. 화면을 잠그거나 앱을 닫아 보세요.`);
    else log(data.ok ? `푸시 서비스가 받음(${data.status})` : `실패 ${data.status} ${data.detail}`);
  }

  async function copy() {
    await navigator.clipboard.writeText(`${state.innerText}\n\n${out.textContent}`);
    log("결과를 복사했습니다.");
  }

  const actions = {
    on: subscribe,
    off: unsubscribe,
    now: () => send(0),
    soon: () => send(10),
    later: () => send(60),
    copy,
  };

  buttons.forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await actions[button.dataset.do]();
    } catch (error) {
      log(`오류: ${error.name} ${error.message}`);
    }
    button.disabled = false;
    showState();
  }));

  const opened = new URLSearchParams(location.search).get("opened");
  if (opened) log("알림을 눌러 이 화면을 열었습니다.");
  showState();
})();
