#!/usr/bin/env bash
# QNAP NAS 배포 도우미. 이 PC의 프로젝트 폴더에서 실행하고, NAS에는 SSH로 명령을 보낸다.
# 절차와 설명은 deploy_qnap.md를 본다. 인자 없이 실행하면 명령 목록을 보여 준다.
#
# 바꿀 수 있는 값(환경 변수):
#   NAS_HOST  SSH 접속 대상      (기본 gp@192.168.0.114)
#   NAS_DIR   NAS의 설치 폴더    (기본 /share/barochannel)
#   NAS_URL   확인용 접속 주소   (기본 http://<NAS IP>:8000)
#   NAS_SSH   ssh 명령           (기본 ssh)
set -euo pipefail
cd "$(dirname "$0")/.."

NAS_HOST="${NAS_HOST:-gp@192.168.0.114}"
NAS_DIR="${NAS_DIR:-/share/barochannel}"
NAS_URL="${NAS_URL:-http://${NAS_HOST#*@}:8000}"
NAS_SSH="${NAS_SSH:-ssh}"
LAN_COMPOSE_FILE="docker-compose.yml:deploy/compose.lan.yml"
MIN_COMPOSE="2.24.0" # deploy/compose.lan.yml의 !override 태그

# 비밀번호로 접속해도 명령마다 묻지 않도록 연결 하나를 2분 동안 다시 쓴다.
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=/tmp/baro-nas-%C" -o ControlPersist=120)

# NAS에서 원격 스크립트 앞에 붙는 공통 부분. Container Station의 docker가
# 비대화형 SSH의 PATH에 없을 수 있어 설치 경로를 찾아 붙인다.
PRELUDE='set -e
DIR="$1"; shift
CS=$(getcfg container-station Install_Path -f /etc/config/qpkg.conf 2>/dev/null || true)
if [ -n "$CS" ]; then PATH="$PATH:$CS/bin:$CS/usr/bin:$CS/usr/local/bin"; fi
export PATH
# Container Station의 docker는 사용자 설정을 .qpkg/container-station/homes/<사용자>에 만들려 하는데,
# admin이 아닌 계정은 그곳에 쓸 수 없어 빌드(buildx)가 멈춘다. 설치 폴더 안으로 돌린다.
if [ -z "${DOCKER_CONFIG:-}" ] && [ -d "$DIR" ] && [ -w "$DIR" ]; then
  DOCKER_CONFIG="$DIR/.docker"; mkdir -p "$DOCKER_CONFIG"; export DOCKER_CONFIG
fi
'

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '오류: %s\n' "$*" >&2; exit 1; }

# 표준 입력으로 받은 sh 스크립트를 NAS에서 돌린다. 첫 인자는 설치 폴더($DIR)가 된다.
remote_sh() {
  local args="'$NAS_DIR'" a
  for a in "$@"; do args+=" '$a'"; done
  local rc=0
  { printf '%s\n' "$PRELUDE"; cat; } | "$NAS_SSH" "${SSH_OPTS[@]}" "$NAS_HOST" "sh -s -- $args" || rc=$?
  ssh_check "$rc"
  return "$rc"
}

# 한 줄 명령을 NAS에서 돌린다(표준 입력을 넘길 때 쓴다).
remote_cmd() {
  local rc=0
  "$NAS_SSH" "${SSH_OPTS[@]}" "$NAS_HOST" "$1" || rc=$?
  ssh_check "$rc"
  return "$rc"
}

# ssh는 접속이나 로그인에 실패하면 255로 끝난다. 원격 명령의 실패와 구분해 바로 알린다.
ssh_check() {
  (($1 == 255)) || return 0
  die "SSH로 $NAS_HOST 에 접속하지 못했습니다. 비밀번호를 확인하세요.
  여러 번 틀리면 QTS가 이 PC의 IP를 잠시 차단할 수 있습니다(제어판 → 보안 → IP 접근 보호)."
}

usage() {
  cat <<EOF
사용법: deploy/nas.sh <명령>

처음 한 번:
  key        이 PC의 SSH 키를 NAS에 등록한다(비밀번호를 매번 묻지 않게)
  check      NAS 환경을 점검한다(docker, compose, bash, 포트, 메모리)
  env        NAS에 .env를 만든다(관리자 계정을 묻고 비밀번호류는 임의 생성, 1차 설정)
  migrate    이 PC의 DB와 첨부파일을 NAS로 옮긴다(NAS 쪽 데이터는 지워진다)
  cron       NAS에 매일 03:00 백업을 등록한다

배포할 때마다:
  deploy     push + up (커밋된 코드를 보내고 다시 빌드해 띄운다)
  push       커밋된 코드(HEAD)만 NAS로 보낸다
  up         NAS에서 백업 후 빌드하고 띄운 뒤 접속을 확인한다
  ship       push + 앱 이미지를 이 PC에서 빌드해 NAS로 보내고 띄운다
             (NAS에서 빌드가 실패할 때. 예: invalid tar header)

운영:
  status     배포된 커밋, 컨테이너 상태, 접속 확인
  logs [서비스] [줄수]   로그 보기 (기본 app 100줄)
  backup     NAS에서 지금 백업한다

대상: $NAS_HOST:$NAS_DIR  확인 주소: $NAS_URL
EOF
}

# 설치 폴더는 QTS 공유 폴더로 미리 만들어 둔다. /share 바로 아래에 mkdir로 만들면
# 데이터 디스크가 아니라 시스템 영역에 생겨 재부팅 때 사라질 수 있어 스크립트가 만들지 않는다.
require_dir() {
  remote_cmd "test -d '$NAS_DIR' && test -w '$NAS_DIR'" ||
    die "$NAS_DIR 이 없거나 쓸 수 없습니다. QTS에서 공유 폴더를 만들고 권한을 주세요(deploy_qnap.md 2.5)."
}

cmd_key() {
  say "SSH 키 등록 ($NAS_HOST)"
  [[ -f ~/.ssh/id_ed25519.pub || -f ~/.ssh/id_rsa.pub ]] || ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519
  if ! ssh-copy-id "$NAS_HOST"; then
    # QTS는 홈 폴더 서비스가 꺼져 있으면 /share/homes/<사용자>가 없어 키를 둘 곳이 없다.
    cat >&2 <<EOF

키를 등록하지 못했습니다. "Could not chdir to home directory"가 보였다면 NAS에 홈 폴더가 없는 것입니다.
  QTS 제어판 → 권한 → 사용자 → 홈 폴더 → "모든 사용자에 대해 홈 폴더 서비스 활성화" → 적용
그 뒤 deploy/nas.sh key를 다시 실행합니다. 키 없이도 나머지 명령은 비밀번호로 쓸 수 있습니다.
EOF
    exit 1
  fi
  echo "다음부터 비밀번호 없이 들어가는지 확인: ssh $NAS_HOST true"
  echo "계속 비밀번호를 묻으면 NAS 홈 폴더 권한을 좁힌다:"
  echo "  ssh $NAS_HOST 'chmod 755 ~ && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys'"
}

cmd_check() {
  say "NAS 점검 ($NAS_HOST)"
  local out compose
  out="$(remote_sh <<'EOF'
echo "arch=$(uname -m)"
echo "qts=$(getcfg system version -f /etc/config/uLinux.conf 2>/dev/null || echo '?')"
if command -v bash >/dev/null; then echo "bash=있음"; else echo "bash=없음"; fi
if command -v docker >/dev/null; then
  echo "docker=$(docker version --format '{{.Server.Version}}' 2>&1 | head -1)"
  echo "compose=$(docker compose version --short 2>&1 | head -1)"
else
  echo "docker=없음"
fi
echo "mem_avail_mb=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)"
if netstat -tln 2>/dev/null | grep -q '[:.]8000 '; then echo "port8000=사용중"; else echo "port8000=비어있음"; fi
if [ ! -d "$DIR" ]; then echo "dir=없음"; elif [ -w "$DIR" ]; then echo "dir=있음"; else echo "dir=쓰기권한없음"; fi
if [ -f "$DIR/.env" ]; then echo "env=있음"; else echo "env=없음"; fi
if [ -f "$DIR/.deployed" ]; then echo "deployed=$(cat "$DIR/.deployed")"; fi
EOF
)"
  printf '%s\n' "$out" | sed 's/=/: /'
  compose="$(sed -n 's/^compose=v\{0,1\}//p' <<<"$out" | sed 's/-.*//')"
  grep -q '^docker=없음' <<<"$out" && die "docker를 찾지 못했습니다. Container Station 설치와 SSH 계정 권한을 확인하세요."
  grep -q '^bash=없음' <<<"$out" && echo "주의: bash가 없어 deploy/backup.sh, restore.sh를 쓸 수 없습니다."
  grep -q '^dir=없음' <<<"$out" && die "$NAS_DIR 이 없습니다. QTS에서 공유 폴더를 먼저 만드세요(deploy_qnap.md 2.5)."
  grep -q '^dir=쓰기권한없음' <<<"$out" && die "$NAS_DIR 에 쓸 수 없습니다. 공유 폴더 권한에서 ${NAS_HOST%@*} 계정에 읽기/쓰기를 주세요."
  if [[ "$(printf '%s\n%s\n' "$MIN_COMPOSE" "$compose" | sort -V | head -1)" != "$MIN_COMPOSE" ]]; then
    die "Docker Compose $MIN_COMPOSE 이상이 필요합니다(현재 $compose). Container Station을 업데이트하세요."
  fi
  if grep -q '^port8000=사용중' <<<"$out" && ! grep -q '^deployed=' <<<"$out"; then
    echo "주의: NAS에서 8000 포트를 다른 프로그램이 쓰고 있습니다. .env의 BARO_APP_PORT와 NAS_URL을 바꾸세요."
  fi
  echo "점검 끝."
}

cmd_env() {
  say "NAS .env 만들기 ($NAS_HOST:$NAS_DIR/.env)"
  require_dir
  if remote_cmd "test -e '$NAS_DIR/.env'"; then
    die "NAS에 .env가 이미 있습니다. 덮어쓰지 않습니다. 고치려면 NAS에서 직접 편집하세요."
  fi
  local email password password2 company tmp
  read -rp "시스템 관리자 이메일: " email
  [[ "$email" == *@* ]] || die "이메일 형식이 아닙니다."
  read -rsp "관리자 비밀번호(8자 이상): " password; echo
  read -rsp "한 번 더: " password2; echo
  [[ "$password" == "$password2" ]] || die "두 비밀번호가 다릅니다."
  ((${#password} >= 8)) || die "8자 이상이어야 합니다."
  # .env는 compose가 읽으므로 $·따옴표·역슬래시·공백은 값이 바뀌거나 깨진다.
  [[ "$password" =~ [\$\"\'\\[:space:]] ]] && die "비밀번호에 \$, 따옴표, 역슬래시, 공백은 쓸 수 없습니다."
  read -rp "회사명 [좋은상자]: " company
  company="${company:-좋은상자}"

  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' RETURN
  chmod 600 "$tmp"
  # NAS로 보내는 코드와 같은 버전의 .env.example을 바탕으로 만든다.
  git show HEAD:.env.example >"$tmp"
  ADMIN_EMAIL="$email" ADMIN_PASSWORD="$password" COMPANY="$company" \
    LAN_COMPOSE_FILE="$LAN_COMPOSE_FILE" python3 - "$tmp" <<'PY'
import os, re, secrets, sys

path = sys.argv[1]
values = {
    "BARO_DEBUG": "false",
    "BARO_SECRET_KEY": secrets.token_urlsafe(48),
    "BARO_ADMIN_EMAIL": os.environ["ADMIN_EMAIL"],
    "BARO_ADMIN_PASSWORD": os.environ["ADMIN_PASSWORD"],
    "BARO_COMPANY_NAME": os.environ["COMPANY"],
    "BARO_DB_PASSWORD": secrets.token_hex(24),
    "BARO_S3_SECRET_KEY": secrets.token_hex(24),
    # 예시 파일의 샘플 할 일 시스템 주소는 운영에 필요 없다.
    "BARO_INTEGRATION_ALLOWED_HOSTS": "",
    # 1차: 사내망에서 IP와 HTTP로 접속한다(deploy/compose.lan.yml).
    "COMPOSE_FILE": os.environ["LAN_COMPOSE_FILE"],
    "BARO_APP_PORT": "8000",
}
lines, seen = [], set()
for line in open(path, encoding="utf-8").read().splitlines():
    m = re.match(r"^#?\s?([A-Z][A-Z0-9_]*)=", line)
    if m and m[1] in values and m[1] not in seen:
        seen.add(m[1])
        line = f"{m[1]}={values[m[1]]}"
    lines.append(line)
missing = set(values) - seen
if missing:
    sys.exit(f".env.example에 없는 키: {', '.join(sorted(missing))}")
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
  # 덮어쓰지 않도록 NAS에서 한 번 더 확인하고, 다른 사람이 못 읽게 600으로 만든다.
  remote_cmd "cd '$NAS_DIR' && umask 077 && [ ! -e .env ] && cat > .env" <"$tmp"
  echo "만들었습니다. DB·MinIO 비밀번호와 SECRET_KEY는 임의 값으로 들어갔습니다(NAS의 .env에만 있음)."
  echo "1차 설정: $LAN_COMPOSE_FILE, 포트 8000"
}

cmd_push() {
  say "코드 보내기 → $NAS_HOST:$NAS_DIR"
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    git status --short --untracked-files=no
    die "커밋되지 않은 변경이 있습니다. 커밋한 뒤 다시 실행하세요(보내는 것은 커밋된 HEAD뿐입니다)."
  fi
  local untracked rev
  untracked="$(git ls-files --others --exclude-standard)"
  if [[ -n "$untracked" ]]; then
    echo "참고: git에 없는 파일은 보내지 않습니다:"
    sed 's/^/  /' <<<"$untracked" | head -10
  fi
  rev="$(git log -1 --format='%h %ad %s' --date=format:'%Y-%m-%d %H:%M')"
  require_dir
  git archive HEAD | remote_cmd "tar xf - -C '$NAS_DIR'"
  printf '%s\n' "$rev" | remote_cmd "cat > '$NAS_DIR/.deployed'"
  echo "보냄: $rev"
}

# $1: build(기본, NAS에서 빌드) 또는 image(ship으로 보낸 이미지를 그대로 쓴다)
cmd_up() {
  local mode="${1:-build}"
  say "NAS에서 띄우기 ($mode)"
  remote_sh "$mode" <<'EOF'
cd "$DIR"
[ -f .env ] || { echo ".env가 없습니다. 먼저 deploy/nas.sh env" >&2; exit 1; }
if [ -n "$(docker compose --profile app ps -q --status running app 2>/dev/null)" ]; then
  echo "-- 띄우기 전 백업"
  bash deploy/backup.sh
fi
if [ "$1" = image ]; then
  echo "-- 이 PC에서 보낸 이미지로 띄우기"
  docker compose --profile app up -d --no-build
else
  echo "-- 빌드하고 띄우기 (처음에는 몇 분 걸린다)"
  docker compose --profile app up -d --build
fi
echo "-- 앱이 준비되기를 기다리는 중"
id=$(docker compose --profile app ps -q app)
i=0; state=""
while [ $i -lt 60 ]; do
  state=$(docker inspect -f '{{.State.Health.Status}}' "$id" 2>/dev/null || echo "?")
  [ "$state" = healthy ] && break
  i=$((i + 1)); sleep 3
done
docker compose --profile app ps
if [ "$state" != healthy ]; then
  echo "앱이 준비되지 않았습니다($state). 로그: deploy/nas.sh logs app" >&2
  exit 1
fi
EOF
  check_url
}

check_url() {
  local body
  if body="$(curl -fsS -m 5 "$NAS_URL/healthz" 2>&1)"; then
    echo "접속 확인: $NAS_URL/healthz → $body"
  else
    echo "이 PC에서 $NAS_URL 에 접속하지 못했습니다: $body" >&2
    echo "NAS 방화벽(QuFirewall)과 .env의 BARO_APP_PORT를 확인하세요." >&2
    return 1
  fi
}

cmd_deploy() { cmd_push; cmd_up; }

# NAS에서 이미지를 받다가 깨지는 경우(failed to register layer: invalid tar header 등)를 피해
# 이 PC에서 커밋된 HEAD로 앱 이미지를 만들어 보낸다. compose가 찾는 이름(<프로젝트>-app)으로 붙인다.
cmd_ship() {
  cmd_push
  local project image tag
  project="$(basename "$NAS_DIR" | tr '[:upper:]' '[:lower:]')"
  image="$project-app"
  tag="$image:nas-$(git rev-parse --short HEAD)"
  say "이 PC에서 앱 이미지 빌드 ($tag)"
  git archive HEAD | docker build --platform linux/amd64 -t "$tag" -
  say "NAS로 이미지 보내기 ($(docker image inspect -f '{{.Size}}' "$tag" | awk '{printf "%d MB", $1/1048576}'), 압축해서 전송)"
  docker save "$tag" | gzip -1 |
    remote_cmd "cd '$NAS_DIR' && DOCKER_CONFIG='$NAS_DIR/.docker' && export DOCKER_CONFIG && mkdir -p \"\$DOCKER_CONFIG\" && docker load && docker tag '$tag' '$image:latest'"
  cmd_up image
}

cmd_status() {
  say "NAS 상태"
  remote_sh <<'EOF'
cd "$DIR"
echo "배포된 커밋: $(cat .deployed 2>/dev/null || echo '없음')"
docker compose --profile app ps
EOF
  check_url || true
}

cmd_logs() {
  local service="${1:-app}" lines="${2:-100}"
  [[ "$service" =~ ^[a-z]+$ && "$lines" =~ ^[0-9]+$ ]] || die "사용법: deploy/nas.sh logs [서비스] [줄수]"
  remote_sh "$service" "$lines" <<'EOF'
cd "$DIR"
docker compose --profile app logs --tail="$2" "$1"
EOF
}

cmd_backup() {
  say "NAS 백업"
  remote_sh <<'EOF'
cd "$DIR"
bash deploy/backup.sh
EOF
}

cmd_migrate() {
  say "이 PC의 데이터 → NAS"
  echo "NAS의 현재 DB와 첨부파일을 지우고 이 PC의 데이터로 바꿉니다."
  local answer name
  read -rp "계속하려면 yes를 입력하세요: " answer
  [[ "$answer" == yes ]] || die "취소했습니다."
  remote_cmd "test -f '$NAS_DIR/.env' && test -f '$NAS_DIR/deploy/restore.sh'" ||
    die "NAS에 코드와 .env가 있어야 합니다. 먼저 push, env를 하세요."

  name="to-nas-$(date +%Y%m%d-%H%M%S)"
  echo "-- 이 PC에서 백업 (backups/$name)"
  deploy/backup.sh "backups/$name"
  echo "-- NAS로 복사"
  tar -C backups -cf - "$name" | remote_cmd "mkdir -p '$NAS_DIR/backups' && tar xf - -C '$NAS_DIR/backups'"
  echo "-- NAS에서 복원"
  remote_sh "$name" <<'EOF'
cd "$DIR"
if [ -n "$(docker compose --profile app ps -q app 2>/dev/null)" ]; then
  echo "-- 복원 전 NAS 데이터 백업"
  bash deploy/backup.sh
fi
bash deploy/restore.sh "backups/$1" -y
docker compose --profile app up -d --build
EOF
  check_url
  echo "복원했습니다. 첨부파일이 있는 글을 열어 내려받기가 되는지 확인하세요."
}

cmd_cron() {
  say "NAS 매일 백업 등록 (03:00)"
  local line="0 3 * * * cd $NAS_DIR && bash deploy/backup.sh >> backups/backup.log 2>&1"
  # QTS는 /etc/config/crontab만 재부팅 후에도 남는다. 관리자 비밀번호를 물을 수 있다.
  "$NAS_SSH" -t "${SSH_OPTS[@]}" "$NAS_HOST" "
    S=''; [ \"\$(id -u)\" = 0 ] || S=sudo
    if grep -qF '$NAS_DIR && bash deploy/backup.sh' /etc/config/crontab; then
      echo '이미 등록되어 있습니다.'
    else
      echo '$line' | \$S tee -a /etc/config/crontab >/dev/null &&
      \$S crontab /etc/config/crontab && \$S /etc/init.d/crond.sh restart >/dev/null &&
      echo '등록했습니다.'
    fi
    grep -F 'deploy/backup.sh' /etc/config/crontab"
}

case "${1:-}" in
  key) cmd_key ;;
  check) cmd_check ;;
  env) cmd_env ;;
  push) cmd_push ;;
  up) cmd_up ;;
  deploy) cmd_deploy ;;
  ship) cmd_ship ;;
  status) cmd_status ;;
  logs) shift; cmd_logs "$@" ;;
  backup) cmd_backup ;;
  migrate) cmd_migrate ;;
  cron) cmd_cron ;;
  *) usage; [[ -z "${1:-}" ]] || exit 1 ;;
esac
