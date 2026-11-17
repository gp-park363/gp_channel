# 바로채널 앱 이미지. HTTPS는 앞단의 Caddy가 맡고, 앱은 내부망에서 HTTP로만 받는다.
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
# 의존성 층을 먼저 만들어 소스만 바뀌면 다시 받지 않게 한다.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm
RUN useradd --system --uid 10001 --home-dir /app baro
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    BARO_HOST=0.0.0.0 \
    BARO_PORT=8000 \
    BARO_DEBUG=false
USER baro
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
# 시작할 때마다 마이그레이션을 맞춘 뒤 서버를 띄운다.
CMD ["sh", "-c", "alembic upgrade head && exec barochannel"]
