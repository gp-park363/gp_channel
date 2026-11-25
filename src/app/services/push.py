"""웹 푸시 발송. 구독 하나에 메시지 하나를 암호화해 보낸다(RFC 8291, 8292)."""

import base64
import json
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

import http_ece
import httpx2
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02

from app.core.config import get_settings

# 구독 주소는 브라우저가 보내는 값이다. 서버가 아무 곳이나 부르지 않게 푸시 서비스만 허용한다(SSRF).
# jmt17.google.com은 Chromium 계열이 fcm.googleapis.com 대신 쓰는 주소다(Chromium 159에서 확인).
PUSH_HOSTS = frozenset(
    {"fcm.googleapis.com", "jmt17.google.com", "updates.push.services.mozilla.com"}
)
PUSH_HOST_SUFFIXES = (".push.apple.com", ".notify.windows.com")
MAX_ENDPOINT = 2000
TIMEOUT_SECONDS = 10
TTL_SECONDS = 24 * 60 * 60
# Apple은 24시간보다 긴 토큰을 거부한다.
TOKEN_SECONDS = 12 * 60 * 60

logger = logging.getLogger(__name__)

# 시험에서만 바꾼다.
transport: httpx2.AsyncBaseTransport | None = None


class PushError(ValueError):
    """사용자에게 그대로 보여줄 수 있는 문구를 담는다."""


@dataclass(frozen=True)
class Subscription:
    endpoint: str
    p256dh: bytes
    auth: bytes


@dataclass(frozen=True)
class Result:
    ok: bool
    # 0이면 푸시 서비스에 닿지 못했다.
    status: int
    detail: str


def b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def b64decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def generate_private_key() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return b64encode(key.private_numbers().private_value.to_bytes(32))


@lru_cache(maxsize=4)
def load_vapid(private_key: str) -> Vapid02:
    raw = b64decode(private_key)
    if len(raw) != 32:
        raise ValueError("개인키는 32바이트여야 합니다.")
    return Vapid02.from_raw(b64encode(raw).encode())


def config_problem() -> str | None:
    """시작할 때 확인한다. 개인키가 로그에 남지 않도록 값은 문구에 넣지 않는다."""
    settings = get_settings()
    if not settings.vapid_private_key:
        return None
    try:
        load_vapid(settings.vapid_private_key)
    except ValueError:
        return "BARO_VAPID_PRIVATE_KEY가 올바르지 않습니다. barochannel-admin vapid-key로 만드세요."
    if not settings.vapid_subject.startswith(("mailto:", "https://")):
        return "BARO_VAPID_SUBJECT는 mailto: 또는 https://로 시작해야 합니다."
    return None


def enabled() -> bool:
    return bool(get_settings().vapid_private_key)


def public_key() -> str:
    key = load_vapid(get_settings().vapid_private_key).public_key
    return b64encode(key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))


def check_endpoint(endpoint: str) -> str:
    """허용한 푸시 서비스의 호스트를 돌려준다."""
    try:
        parts = urlsplit(endpoint)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError as error:
        raise PushError("구독 주소가 올바르지 않습니다.") from error
    plain = parts.username is None and parts.password is None and port in (None, 443)
    if len(endpoint) > MAX_ENDPOINT or parts.scheme != "https" or not plain:
        raise PushError("구독 주소가 올바르지 않습니다.")
    if host not in PUSH_HOSTS and not host.endswith(PUSH_HOST_SUFFIXES):
        raise PushError(f"허용하지 않은 푸시 서비스입니다: {host[:100]}")
    return host


def parse_subscription(endpoint: str, p256dh: str, auth: str) -> Subscription:
    check_endpoint(endpoint)
    try:
        key, secret = b64decode(p256dh), b64decode(auth)
        # 곡선 위의 점이 아니면 ValueError다.
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), key)
    except ValueError as error:
        raise PushError("구독 키가 올바르지 않습니다.") from error
    if len(key) != 65 or len(secret) != 16:
        raise PushError("구독 키가 올바르지 않습니다.")
    return Subscription(endpoint, key, secret)


def build_request(subscription: Subscription, payload: dict) -> tuple[dict[str, str], bytes]:
    settings = get_settings()
    parts = urlsplit(subscription.endpoint)
    claims = {
        "aud": f"https://{parts.hostname}",
        "exp": int(time.time()) + TOKEN_SECONDS,
        "sub": settings.vapid_subject,
    }
    body = http_ece.encrypt(
        json.dumps(payload, ensure_ascii=False).encode(),
        private_key=ec.generate_private_key(ec.SECP256R1()),
        dh=subscription.p256dh,
        auth_secret=subscription.auth,
        version="aes128gcm",
    )
    headers = {
        **load_vapid(settings.vapid_private_key).sign(claims),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(TTL_SECONDS),
        "Urgency": "high",
    }
    return headers, body


async def send(subscription: Subscription, payload: dict) -> Result:
    headers, body = build_request(subscription, payload)
    try:
        # 프록시 환경 변수를 따르지 않고 리다이렉트도 따라가지 않는다.
        async with httpx2.AsyncClient(
            transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
        ) as client:
            response = await client.post(subscription.endpoint, headers=headers, content=body)
    except httpx2.HTTPError as error:
        # 예외 문구에는 구독 주소가 섞일 수 있어 종류만 남긴다.
        logger.warning("푸시 서비스에 연결하지 못했습니다: %s", type(error).__name__)
        return Result(False, 0, "푸시 서비스에 연결하지 못했습니다.")
    ok = response.status_code in (200, 201, 202, 204)
    detail = "" if ok else response.text[:200]
    if not ok:
        host = urlsplit(subscription.endpoint).hostname
        logger.warning("푸시 발송 실패 %s: %s %s", host, response.status_code, detail)
    return Result(ok, response.status_code, detail)
