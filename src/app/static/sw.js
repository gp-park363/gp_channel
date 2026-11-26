// 알림을 받는 서비스 워커. 화면 요청은 가로채지 않는다(fetch 처리 없음).
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

// 받은 푸시는 반드시 화면에 표시한다. 표시하지 않으면 iOS가 구독을 지운다.
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch { /* 내용을 읽지 못해도 기본 문구로 표시한다. */ }
  let body = data.body || "";
  if (data.sent_at) {
    const seconds = ((Date.now() - data.sent_at) / 1000).toFixed(1);
    body = `발송 ${new Date(data.sent_at).toLocaleTimeString("ko-KR")} · 도착까지 ${seconds}초`;
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "바로채널", {
      body,
      tag: data.tag || "barochannel",
      icon: "/static/brand/icon-192.png",
      lang: "ko",
      data: { url: data.url || "/" },
    }),
  );
});

async function openPage(path) {
  const url = new URL(path, self.location.origin);
  // 다른 사이트로는 보내지 않는다.
  if (url.origin !== self.location.origin) url.href = self.location.origin;
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const client of windows) {
    let moved;
    try {
      moved = await client.navigate(url.href);
    } catch {
      continue; // 옮길 수 없는 창이면 다음 창을 본다.
    }
    // 앞으로 가져오지 못해도 이미 옮겼으므로 새 창을 또 열지 않는다.
    try {
      await (moved || client).focus();
    } catch { /* 브라우저가 막으면 그대로 둔다. */ }
    return;
  }
  return self.clients.openWindow(url.href);
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(openPage(event.notification.data?.url || "/"));
});
