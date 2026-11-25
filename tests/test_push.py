import base64
import json
from pathlib import Path
from urllib.parse import urlsplit

import http_ece
import httpx2
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02

from app.core.config import get_settings
from app.routers import push_test
from app.services import push
from tests.conftest import Browser, add_user

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc"


class Device:
    """브라우저의 구독을 흉내 낸다. 받은 내용을 개인키로 풀 수 있다."""

    def __init__(self, endpoint: str = ENDPOINT) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = b"0123456789abcdef"
        self.endpoint = endpoint

    def json(self) -> dict:
        point = self.key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        keys = {"p256dh": push.b64encode(point), "auth": push.b64encode(self.auth)}
        return {"endpoint": self.endpoint, "expirationTime": None, "keys": keys}

    def open(self, body: bytes) -> dict:
        return json.loads(
            http_ece.decrypt(body, private_key=self.key, auth_secret=self.auth, version="aes128gcm")
        )


@pytest.fixture
def vapid(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_private_key", push.generate_private_key())
    monkeypatch.setattr(settings, "vapid_subject", "mailto:push@example.com")
    push_test.limiter.counts.clear()
    return settings


@pytest.fixture
def service(monkeypatch):
    """푸시 서비스. 받은 요청을 모으고 정한 상태로 답한다."""

    class Service:
        status, text, requests = 201, "", []

    fake = Service()
    fake.requests = []

    def answer(request):
        fake.requests.append(request)
        return httpx2.Response(fake.status, text=fake.text, headers={"Location": "http://a.test"})

    monkeypatch.setattr(push, "transport", httpx2.MockTransport(answer))
    return fake


def signed_in(name: str = "tester") -> Browser:
    return Browser().login(add_user(name).email)


def send(browser: Browser, subscription: dict, delay: int = 0):
    return browser.post("/push-test/send", json={"subscription": subscription, "delay": delay})


def test_message_is_encrypted_for_the_device_and_signed_with_our_key(vapid, service) -> None:
    device = Device()
    response = send(signed_in(), device.json())
    assert response.json() == {"ok": True, "status": 201, "detail": ""}
    (request,) = service.requests
    payload = device.open(request.content)
    assert payload["title"] == "바로채널 시험 알림"
    assert payload["url"].startswith("/push-test?")
    assert request.headers["content-encoding"] == "aes128gcm"
    assert request.headers["ttl"] == "86400"

    scheme, _, rest = request.headers["authorization"].partition(" ")
    fields = dict(part.split("=", 1) for part in rest.split(","))
    assert scheme == "vapid" and fields["k"] == push.public_key()
    claims = json.loads(push.b64decode(fields["t"].split(".")[1]))
    assert claims["aud"] == "https://fcm.googleapis.com"
    assert claims["sub"] == "mailto:push@example.com"
    assert Vapid02.verify(request.headers["authorization"])


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/x",
        "https://127.0.0.1/x",
        "https://fcm.googleapis.com.evil.test/x",
        "https://evilpush.apple.com.test/x",
        "https://fcm.googleapis.com:8443/x",
        "https://user@fcm.googleapis.com/x",
        "https://fcm.googleapis.com@evil.test/x",
        "https://fcm.googleapis.com:bad/x",
        "not a url",
    ],
)
def test_other_hosts_are_refused_without_calling_them(vapid, service, endpoint) -> None:
    assert send(signed_in(), Device(endpoint).json()).status_code == 400
    assert service.requests == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://jmt17.google.com/fcm/send/abc",
        "https://web.push.apple.com/QAbc",
        "https://updates.push.services.mozilla.com/wpush/v2/a",
        "https://sg2p.notify.windows.com/w/?token=a",
    ],
)
def test_known_push_services_are_allowed(vapid, service, endpoint) -> None:
    assert send(signed_in(), Device(endpoint).json()).json()["ok"]
    assert service.requests[0].url.host == urlsplit(endpoint).hostname


def test_bad_keys_are_refused(vapid, service) -> None:
    browser = signed_in()
    for p256dh, auth in [
        ("AAAA", "AAAA"),
        ("!!!", "!!!"),
        (push.b64encode(b"\x04" + b"\x01" * 64), push.b64encode(b"a" * 16)),
        (Device().json()["keys"]["p256dh"], "AAAA"),
    ]:
        row = {"endpoint": ENDPOINT, "keys": {"p256dh": p256dh, "auth": auth}}
        assert send(browser, row).status_code == 400
    assert service.requests == []


def test_failures_and_redirects_are_reported_not_followed(vapid, service) -> None:
    browser = signed_in()
    for code in (302, 403, 410):
        service.status, service.text = code, "reason"
        answer = send(browser, Device().json()).json()
        assert answer == {"ok": False, "status": code, "detail": "reason"}
    assert len(service.requests) == 3


def test_unreachable_service_is_reported(vapid, monkeypatch) -> None:
    def broken(request):
        raise httpx2.ConnectError("down", request=request)

    monkeypatch.setattr(push, "transport", httpx2.MockTransport(broken))
    answer = send(signed_in(), Device().json()).json()
    assert answer["ok"] is False and answer["status"] == 0


def test_login_csrf_rate_and_delay_limits(vapid, service, client) -> None:
    browser = signed_in()
    row = {"subscription": Device().json()}
    assert client.get("/push-test").status_code == 303
    assert browser.http.post("/push-test/send", json=row).status_code == 403
    assert send(browser, Device().json(), delay=121).status_code == 422
    for _ in range(push_test.SENDS_PER_MINUTE):
        assert send(browser, Device().json()).status_code == 200
    assert send(browser, Device().json()).status_code == 429


def test_page_and_menu_follow_the_setting(vapid, monkeypatch) -> None:
    browser = signed_in()
    page = browser.get("/push-test")
    assert page.status_code == 200 and push.public_key() in page.text
    assert vapid.vapid_private_key not in page.text
    assert "/push-test" in browser.get("/more").text
    monkeypatch.setattr(vapid, "vapid_private_key", "")
    assert browser.get("/push-test").status_code == 404
    assert send(browser, Device().json()).status_code == 404
    assert "/push-test" not in browser.get("/more").text


def test_service_worker_is_served_at_the_root_without_cache(client) -> None:
    response = client.get("/sw.js")
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "no-cache"
    assert "showNotification" in response.text


def test_wrong_settings_are_reported_without_the_key(vapid, monkeypatch) -> None:
    assert push.config_problem() is None
    monkeypatch.setattr(vapid, "vapid_subject", "admin@example.com")
    assert "BARO_VAPID_SUBJECT" in push.config_problem()
    for wrong in ("secret-value!", push.b64encode(b"short"), base64.b64encode(b"\0" * 32).decode()):
        monkeypatch.setattr(vapid, "vapid_private_key", wrong)
        problem = push.config_problem()
        assert "BARO_VAPID_PRIVATE_KEY" in problem and wrong not in problem


def test_compose_passes_the_settings_to_the_container() -> None:
    text = Path("docker-compose.yml").read_text()
    assert "BARO_VAPID_PRIVATE_KEY" in text and "BARO_VAPID_SUBJECT" in text
