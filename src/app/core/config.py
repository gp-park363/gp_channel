from functools import lru_cache
from typing import Annotated
from urllib.parse import quote

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEV_SECRET_KEY = "dev-only-secret-key"
DEFAULT_EXTENSIONS = frozenset(
    "png jpg jpeg gif webp pdf txt csv zip hwp hwpx doc docx xls xlsx ppt pptx".split()
)


class Settings(BaseSettings):
    """환경 차이는 코드가 아니라 BARO_ 환경 변수나 .env 파일로 분리한다."""

    model_config = SettingsConfigDict(env_prefix="BARO_", env_file=".env", extra="ignore")

    # 보통은 BARO_DB_* 값으로 접속 주소를 만든다. BARO_DATABASE_URL을 주면 그 값을 그대로 쓴다.
    # 계정과 비밀번호를 한 곳(.env)에만 두어 docker-compose의 PostgreSQL과 어긋나지 않게 한다.
    database_url: str = ""
    db_user: str = "barochannel"
    db_password: str = "barochannel"
    db_name: str = "barochannel"
    db_host: str = "localhost"
    db_port: int = 5432
    db_null_pool: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False
    secret_key: str = DEV_SECRET_KEY
    session_max_age_seconds: int = 12 * 60 * 60
    # 세션 쿠키를 HTTPS에서만 보낼지. 비우면 운영 모드(debug=false)에서 켠다.
    # 사내망에서 IP와 HTTP로만 운영할 때(QNAP 1차 배포)만 false로 둔다.
    secure_cookies: bool | None = None

    # 시스템 관리자의 로그인 정보는 화면이나 DB가 아니라 환경 변수로만 관리한다.
    admin_email: str = ""
    admin_password: str = ""

    # 화면 왼쪽 위 로고 옆에 보이는 이름.
    company_name: str = "바로채널"

    s3_endpoint_url: str = "http://127.0.0.1:9000"
    s3_access_key: str = "barochannel"
    s3_secret_key: str = "barochannel"
    s3_bucket: str = "barochannel"
    s3_region: str = "us-east-1"

    # 기안자가 상신 후 기안을 취소할 수 있는 시간.
    withdraw_hours: int = Field(default=24, gt=0)

    max_upload_mb: int = 20
    max_files_per_post: int = 10
    # 쉼표로 구분한다. 환경 변수 값은 JSON이 아니라 validator가 해석한다.
    allowed_extensions: Annotated[frozenset[str], NoDecode] = DEFAULT_EXTENSIONS

    # 업무 채널이 부를 수 있는 외부 시스템. "호스트" 또는 "호스트:포트"를 쉼표로 구분한다.
    # 그룹 관리자가 넣은 주소로 서버가 내부망을 마음대로 부르지 않게 한다(F-83).
    integration_allowed_hosts: Annotated[frozenset[str], NoDecode] = frozenset()
    integration_timeout_seconds: float = Field(default=5, gt=0, le=30)

    @field_validator("admin_email")
    @classmethod
    def normalize_admin_email(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("admin_password")
    @classmethod
    def check_admin_password(cls, value: str) -> str:
        if value and len(value) < 8:
            raise ValueError("BARO_ADMIN_PASSWORD는 8자 이상이어야 합니다.")
        return value

    @field_validator("integration_allowed_hosts", mode="before")
    @classmethod
    def parse_hosts(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return frozenset(item.strip().lower() for item in value.split(",")) - {""}

    @field_validator("allowed_extensions", mode="before")
    @classmethod
    def parse_extensions(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        items = {item.strip().lower().removeprefix(".") for item in value.split(",")} - {""}
        if not items or not all(item.isalnum() for item in items):
            raise ValueError("BARO_ALLOWED_EXTENSIONS는 쉼표로 구분한 확장자여야 합니다.")
        return frozenset(items)

    @model_validator(mode="after")
    def derive_defaults(self) -> "Settings":
        if not self.database_url:
            # 비밀번호에 특수문자가 있어도 URL이 깨지지 않게 인코딩한다.
            self.database_url = (
                f"postgresql+asyncpg://{quote(self.db_user, safe='')}:"
                f"{quote(self.db_password, safe='')}@{self.db_host}:{self.db_port}/"
                f"{quote(self.db_name, safe='')}"
            )
        if self.secure_cookies is None:
            self.secure_cookies = not self.debug
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
