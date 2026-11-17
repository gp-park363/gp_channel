#!/usr/bin/env bash
# backup.sh로 만든 백업을 복원한다. 현재 DB와 첨부파일은 모두 지워진다.
# 사용법: deploy/restore.sh <백업 폴더> [-y]
set -euo pipefail
cd "$(dirname "$0")/.."

dir="${1:?사용법: deploy/restore.sh <백업 폴더> [-y]}"
dir="$(cd "$dir" && pwd)"
for f in barochannel.dump minio-data.tgz; do
  [[ -f "$dir/$f" ]] || { echo "$dir/$f 가 없습니다." >&2; exit 1; }
done

if [[ "${2:-}" != "-y" ]]; then
  read -r -p "현재 DB와 첨부파일을 지우고 $dir 로 복원합니다. 계속할까요? [y/N] " answer
  [[ "$answer" == [yY] ]] || exit 1
fi

app_running="$(docker compose --profile app ps -q --status running app)"
docker compose --profile app stop app >/dev/null 2>&1 || true

docker compose up -d --wait postgres minio >/dev/null 2>&1

echo "DB 복원 중..."
# 계정과 DB 이름은 컨테이너 환경 변수(.env의 BARO_DB_*)를 따른다.
# createdb는 접속한 계정을 소유자로 만들고, 다른 계정으로 만든 백업도 --no-owner로 받아들인다.
docker compose exec -T postgres sh -c '
  dropdb -U "$POSTGRES_USER" -d postgres --if-exists --force "$POSTGRES_DB" &&
  createdb -U "$POSTGRES_USER" -d postgres "$POSTGRES_DB"'
docker compose exec -T postgres sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' \
  < "$dir/barochannel.dump"

echo "첨부파일 복원 중..."
minio="$(docker compose ps -q minio)"
docker compose stop minio >/dev/null 2>&1
docker run --rm --volumes-from "$minio" -v "$dir":/backup:ro alpine \
  sh -c 'find /data -mindepth 1 -delete && tar xzf /backup/minio-data.tgz -C /data'
docker compose start minio >/dev/null 2>&1

if [[ -n "$app_running" ]]; then
  echo "앱을 다시 시작합니다."
  docker compose --profile app start app >/dev/null 2>&1
fi
echo "복원 완료."
