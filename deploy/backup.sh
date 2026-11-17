#!/usr/bin/env bash
# 바로채널 데이터(PostgreSQL + 첨부파일 MinIO)를 backups/<시각>/에 백업한다.
# 사용법: deploy/backup.sh [저장 폴더]
# 백업하는 동안 앱을 잠시 멈춰 DB와 첨부파일이 어긋나지 않게 한다.
set -euo pipefail
cd "$(dirname "$0")/.."

out="${1:-backups/$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$out"
out="$(cd "$out" && pwd)"

minio="$(docker compose ps -q minio)"
if [[ -z "$(docker compose ps -q --status running postgres)" || -z "$minio" ]]; then
  echo "postgres와 minio가 떠 있어야 합니다: docker compose up -d" >&2
  exit 1
fi

app_running="$(docker compose --profile app ps -q --status running app)"
if [[ -n "$app_running" ]]; then
  echo "앱을 잠시 멈춥니다."
  docker compose --profile app stop app >/dev/null 2>&1
  trap 'echo "앱을 다시 시작합니다."; docker compose --profile app start app >/dev/null' EXIT
fi

echo "DB 백업 중..."
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > "$out/barochannel.dump"

echo "첨부파일 백업 중..."
docker run --rm --volumes-from "$minio" -v "$out":/backup alpine \
  sh -c "tar czf /backup/minio-data.tgz -C /data . && chown $(id -u):$(id -g) /backup/minio-data.tgz"

echo "완료: $out"
ls -lh "$out"
