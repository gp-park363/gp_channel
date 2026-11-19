#!/usr/bin/env bash
# 클라우드 서버(iwinv 등 Ubuntu 24.04) 배포 도우미. 이 PC의 프로젝트 폴더에서 실행하고,
# 서버에는 SSH로 명령을 보낸다. 절차와 설명은 deploy_iwinv.md를 본다.
# 인자 없이 실행하면 명령 목록을 보여 준다.
#
# 접속 대상은 프로젝트 폴더의 .deploy 파일(git에 올리지 않음)이나 환경 변수로 정한다.
#   DEPLOY_HOST  SSH 접속 대상      예: deploy@203.0.113.10 (처음 setup 때는 root@…)
#   DEPLOY_DIR   서버의 설치 폴더   (기본 /srv/barochannel)
#   DEPLOY_URL   확인용 접속 주소   (기본 https://<서버 .env의 BARO_DOMAIN>)
set -euo pipefail
cd "$(dirname "$0")/.."

# .deploy는 셸 스크립트로 실행하지 않고 KEY=값 줄만 읽는다. 안내문의 <서버 IP>처럼 꺾쇠나
# 공백을 같이 적어도 지운다. 환경 변수로 준 값이 있으면 그것이 먼저다.
if [[ -f .deploy ]]; then
  while IFS='=' read -r key value || [[ -n "$key" ]]; do
    key="${key//[[:space:]]/}"
    [[ "$key" =~ ^(DEPLOY_HOST|DEPLOY_DIR|DEPLOY_URL)$ ]] || continue
    value="${value//[<>\"\'[:space:]]/}"
    [[ -n "${!key:-}" ]] || printf -v "$key" '%s' "$value"
  done <.deploy
fi
DEPLOY_HOST="${DEPLOY_HOST:-}"
DEPLOY_DIR="${DEPLOY_DIR:-/srv/barochannel}"
DEPLOY_URL="${DEPLOY_URL:-}"
DEPLOY_SSH="${DEPLOY_SSH:-ssh}" # 시험할 때 바꿔 끼운다
DEPLOY_USER="deploy"
PUBLIC_COMPOSE_FILE="docker-compose.yml:deploy/compose.public.yml"

# 비밀번호로 접속해도 명령마다 묻지 않도록 연결 하나를 2분 동안 다시 쓴다.
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=/tmp/baro-deploy-%C" -o ControlPersist=120)

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '오류: %s\n' "$*" >&2; exit 1; }

need_host() {
  [[ -n "$DEPLOY_HOST" ]] ||
    die "접속 대상이 없습니다. 예: echo 'DEPLOY_HOST=root@<서버 IP>' > .deploy (deploy_iwinv.md 3장)"
}

# ssh는 접속이나 로그인에 실패하면 255로 끝난다. 원격 명령의 실패와 구분해 바로 알린다.
ssh_check() {
  (($1 == 255)) || return 0
  die "SSH로 $DEPLOY_HOST 에 접속하지 못했습니다. 주소, 계정, 키(또는 비밀번호)를 확인하세요."
}

# 한 줄 명령을 서버에서 돌린다(표준 입력을 넘길 때도 쓴다).
remote_cmd() {
  local rc=0
  "$DEPLOY_SSH" "${SSH_OPTS[@]}" "$DEPLOY_HOST" "$1" || rc=$?
  ssh_check "$rc"
  return "$rc"
}

# 표준 입력으로 받은 sh 스크립트를 서버에서 돌린다. $DIR은 설치 폴더, $1…은 넘긴 인자다.
# 스크립트를 끝까지 읽은 뒤 실행한다(중간의 docker compose exec가 남은 스크립트를 삼키지 않게).
remote_sh() {
  local args="'$DEPLOY_DIR'" a rc=0
  for a in "$@"; do args+=" '$a'"; done
  { printf 'set -e\nDIR="$1"; shift\n'; cat; } |
    "$DEPLOY_SSH" "${SSH_OPTS[@]}" "$DEPLOY_HOST" "sh -c 'eval \"\$(cat)\"' sh $args" || rc=$?
  ssh_check "$rc"
  return "$rc"
}

usage() {
  cat <<EOF
사용법: deploy/server.sh <명령>

처음 한 번 (deploy_iwinv.md 순서대로):
  key        이 PC의 SSH 키를 서버에 등록한다
  setup      (root로) docker 설치, $DEPLOY_USER 계정, 방화벽(22·80·443), 스왑, 설치 폴더 준비
  harden     (root로) $DEPLOY_USER 키 접속을 확인한 뒤 root 로그인과 비밀번호 로그인을 막는다
  check      서버 점검(docker, 메모리, 디스크, 포트, 도메인 DNS)
  env        서버에 .env를 만든다(관리자 계정·도메인을 묻고 비밀번호류는 임의 생성)
  migrate    이 PC의 DB와 첨부파일을 서버로 옮긴다(서버 쪽 데이터는 지워진다)
  cron       서버에 매일 03:00 백업을 등록한다

배포할 때마다:
  deploy     push + up
  push       커밋된 코드(HEAD)만 서버로 보낸다
  up         서버에서 백업 후 빌드하고 띄운 뒤 HTTPS 접속을 확인한다

운영:
  status     배포된 커밋, 컨테이너 상태, 접속 확인
  logs [서비스] [줄수]   로그 보기 (기본 app 100줄, 서비스: app caddy postgres minio)
  backup     서버에서 지금 백업한다
  fetch      서버의 가장 최근 백업을 이 PC의 backups/server/로 받아 온다

대상: ${DEPLOY_HOST:-(미정)}:$DEPLOY_DIR
EOF
}

cmd_key() {
  need_host
  say "SSH 키 등록 ($DEPLOY_HOST)"
  [[ -f ~/.ssh/id_ed25519.pub || -f ~/.ssh/id_rsa.pub ]] || ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519
  ssh-copy-id "$DEPLOY_HOST"
  echo "확인: ssh $DEPLOY_HOST true 가 비밀번호 없이 끝나면 됩니다."
}

cmd_setup() {
  need_host
  [[ "$DEPLOY_HOST" == root@* ]] || die "setup은 root로 한다. .deploy를 DEPLOY_HOST=root@<서버 IP>로 바꾸세요."
  say "서버 준비 ($DEPLOY_HOST)"
  remote_sh "$DEPLOY_USER" <<'EOF'
user="$1"
. /etc/os-release
echo "-- 운영체제: $PRETTY_NAME"
[ "$ID" = ubuntu ] || echo "주의: Ubuntu가 아닙니다. 패키지 이름이 다를 수 있습니다."
export DEBIAN_FRONTEND=noninteractive

echo "-- 시간대 Asia/Seoul"
timedatectl set-timezone Asia/Seoul 2>/dev/null || true

echo "-- docker 설치 (Ubuntu 패키지: docker.io, compose, buildx)"
apt-get update -q
apt-get install -y -q docker.io docker-compose-v2 docker-buildx >/dev/null
systemctl enable --now docker >/dev/null
docker version --format 'docker {{.Server.Version}}'
docker compose version

mem=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$mem" -lt 3000 ] && ! swapon --show | grep -q .; then
  echo "-- 메모리 ${mem}MB: 스왑 2GB 만들기 (빌드와 PostgreSQL에 여유)"
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
fi

echo "-- 배포 계정 $user (docker 사용 가능, root의 SSH 키 복사)"
id "$user" >/dev/null 2>&1 || useradd -m -s /bin/bash "$user"
usermod -aG docker "$user"
install -d -m 700 -o "$user" -g "$user" "/home/$user/.ssh"
if [ -f /root/.ssh/authorized_keys ]; then
  cat /root/.ssh/authorized_keys >>"/home/$user/.ssh/authorized_keys"
  sort -u -o "/home/$user/.ssh/authorized_keys" "/home/$user/.ssh/authorized_keys"
  chown "$user:$user" "/home/$user/.ssh/authorized_keys" && chmod 600 "/home/$user/.ssh/authorized_keys"
else
  echo "주의: root에 등록된 SSH 키가 없습니다. 먼저 deploy/server.sh key를 하세요."
fi

echo "-- 설치 폴더 $DIR"
install -d -m 750 -o "$user" -g "$user" "$DIR"

echo "-- 방화벽(ufw): SSH 22, HTTP 80, HTTPS 443만 연다"
apt-get install -y -q ufw >/dev/null
ufw allow 22/tcp >/dev/null && ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null && ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null && ufw status | sed -n '1,12p'
echo "준비 끝."
EOF
  local ip="${DEPLOY_HOST#*@}"
  cat <<EOF

다음:
  1) .deploy를 배포 계정으로 바꾼다:  echo 'DEPLOY_HOST=$DEPLOY_USER@$ip' > .deploy
  2) ssh $DEPLOY_USER@$ip true 가 비밀번호 없이 끝나는지 확인
  3) deploy/server.sh harden (선택이지만 권장, root 로그인·비밀번호 로그인 막기)
EOF
}

cmd_harden() {
  need_host
  local ip="${DEPLOY_HOST#*@}"
  say "SSH 잠그기 ($ip)"
  # 배포 계정으로 키 접속이 되는지 먼저 확인한다(안 되면 잠그지 않는다. 서버에 못 들어가게 된다).
  # 키로만 시도한다(비밀번호 로그인은 허용하지 않음). 키에 암호(passphrase)가 있으면 여기서 묻는다.
  ssh -o PreferredAuthentications=publickey -o PasswordAuthentication=no \
    -o KbdInteractiveAuthentication=no -o ConnectTimeout=10 "$DEPLOY_USER@$ip" true ||
    die "$DEPLOY_USER@$ip 로 키 접속이 되지 않습니다. 잠그지 않았습니다(setup과 key를 확인하세요)."
  echo "$DEPLOY_USER@$ip 키 접속 확인. root 로그인과 비밀번호 로그인을 막습니다."
  remote_cmd_as_root "$ip" <<'EOF'
cat >/etc/ssh/sshd_config.d/99-barochannel.conf <<'CONF'
# deploy/server.sh harden: 키로만, root는 막는다
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
CONF
sshd -t && (systemctl reload ssh 2>/dev/null || systemctl reload sshd)
echo "적용했습니다."
EOF
  echo "이제 root로는 들어갈 수 없습니다. 관리 작업은 $DEPLOY_USER 로 들어가 sudo 없이 docker로 합니다."
  echo "root가 필요하면 iwinv 콘솔의 웹 콘솔(VNC)로 들어갑니다."
}

# harden 전용: root 접속(현재 DEPLOY_HOST가 root가 아니면 root@ip로 시도)으로 스크립트를 돌린다.
remote_cmd_as_root() {
  local ip="$1" rc=0
  ssh "${SSH_OPTS[@]}" "root@$ip" "sh -s" || rc=$?
  ((rc != 255)) || die "root@$ip 로 접속하지 못했습니다. 이미 잠겨 있으면 할 일이 없습니다."
  return "$rc"
}

cmd_check() {
  need_host
  say "서버 점검 ($DEPLOY_HOST)"
  local out ip domain resolved
  out="$(remote_sh <<'EOF'
. /etc/os-release; echo "os=$PRETTY_NAME"
echo "user=$(id -un) ($(id -Gn | tr ' ' ','))"
if command -v docker >/dev/null; then
  echo "docker=$(docker version --format '{{.Server.Version}}' 2>&1 | head -1)"
  echo "compose=$(docker compose version --short 2>&1 | head -1)"
else
  echo "docker=없음"
fi
echo "mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo) (사용 가능 $(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)), swap_mb=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)"
echo "disk_free=$(df -h "$DIR" 2>/dev/null | awk 'NR==2 {print $4}')"
for p in 80 443; do
  if ss -tln 2>/dev/null | grep -q ":$p "; then echo "port$p=사용중"; else echo "port$p=비어있음"; fi
done
if [ ! -d "$DIR" ]; then echo "dir=없음"; elif [ -w "$DIR" ]; then echo "dir=있음"; else echo "dir=쓰기권한없음"; fi
if [ -f "$DIR/.env" ]; then
  echo "env=있음"; echo "domain=$(sed -n 's/^BARO_DOMAIN=//p' "$DIR/.env")"
else
  echo "env=없음"
fi
[ -f "$DIR/.deployed" ] && echo "deployed=$(cat "$DIR/.deployed")"
true
EOF
)"
  printf '%s\n' "$out" | sed 's/=/: /'
  grep -q '^docker=없음' <<<"$out" && die "docker가 없습니다. root로 deploy/server.sh setup을 먼저 하세요."
  grep -q '^dir=없음' <<<"$out" && die "$DEPLOY_DIR 이 없습니다. root로 deploy/server.sh setup을 먼저 하세요."
  grep -q '^dir=쓰기권한없음' <<<"$out" && die "$DEPLOY_DIR 에 쓸 수 없습니다. $DEPLOY_USER 계정으로 접속하세요."
  # 도메인이 이 서버를 가리키는지(인증서 발급에 필요)
  ip="${DEPLOY_HOST#*@}"
  domain="$(sed -n 's/^domain=//p' <<<"$out")"
  if [[ "$domain" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "접속 방식: 도메인 없이 IP(https://$domain), Let's Encrypt IP 인증서"
    [[ "$domain" == "$ip" ]] || echo "주의: .env의 IP($domain)가 접속 대상($ip)과 다릅니다."
  elif [[ -n "$domain" ]]; then
    resolved="$(getent ahostsv4 "$domain" 2>/dev/null | awk 'NR==1 {print $1}')"
    if [[ "$resolved" == "$ip" ]]; then
      echo "DNS: $domain → $resolved (이 서버, 정상)"
    else
      echo "주의: DNS $domain → ${resolved:-(없음)}. 이 서버($ip)를 가리켜야 인증서를 받습니다(deploy_iwinv.md 2장)."
    fi
  fi
  echo "점검 끝."
}

cmd_env() {
  need_host
  say "서버 .env 만들기 ($DEPLOY_HOST:$DEPLOY_DIR/.env)"
  remote_cmd "test -d '$DEPLOY_DIR' && test -w '$DEPLOY_DIR'" </dev/null ||
    die "$DEPLOY_DIR 이 없거나 쓸 수 없습니다. setup을 먼저 하고 $DEPLOY_USER 계정으로 접속하세요."
  if remote_cmd "test -e '$DEPLOY_DIR/.env'" </dev/null; then
    die "서버에 .env가 이미 있습니다. 덮어쓰지 않습니다. 고치려면 서버에서 직접 편집하세요."
  fi
  local email password password2 company domain tmp
  local ip="${DEPLOY_HOST#*@}" caddyfile=""
  echo "접속 주소: 도메인을 넣거나, 비워 두면 도메인 없이 서버 IP(https://$ip)로 접속합니다."
  read -rp "도메인 (비우면 IP로 접속): " domain
  if [[ -z "$domain" ]]; then
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
      die ".deploy의 DEPLOY_HOST가 IP가 아닙니다($ip). IP로 접속하려면 DEPLOY_HOST=deploy@<서버 IP>로 적으세요."
    domain="$ip"
    caddyfile="Caddyfile.ip"
  fi
  read -rp "시스템 관리자 이메일: " email
  [[ "$email" == *@* ]] || die "이메일 형식이 아닙니다."
  read -rsp "관리자 비밀번호(12자 이상 권장, 인터넷에 열린 서버입니다): " password; echo
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
  git show HEAD:.env.example >"$tmp"
  DOMAIN="$domain" CADDYFILE="$caddyfile" ADMIN_EMAIL="$email" ADMIN_PASSWORD="$password" COMPANY="$company" \
    PUBLIC_COMPOSE_FILE="$PUBLIC_COMPOSE_FILE" python3 - "$tmp" <<'PY'
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
    "BARO_INTEGRATION_ALLOWED_HOSTS": "",
    "BARO_DOMAIN": os.environ["DOMAIN"],
    # 인터넷에 열린 서버: Let's Encrypt HTTP 확인, 공식 Caddy 이미지(deploy/compose.public.yml)
    "COMPOSE_FILE": os.environ["PUBLIC_COMPOSE_FILE"],
}
# 도메인 없이 IP로 접속: Let's Encrypt IP 인증서(deploy/Caddyfile.ip)
if os.environ["CADDYFILE"]:
    values["BARO_CADDYFILE"] = os.environ["CADDYFILE"]
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
  remote_cmd "cd '$DEPLOY_DIR' && umask 077 && [ ! -e .env ] && cat > .env" <"$tmp"
  echo "만들었습니다. DB·MinIO 비밀번호와 SECRET_KEY는 임의 값으로 들어갔습니다(서버의 .env에만 있음)."
  echo "접속 주소: https://$domain"
}

cmd_push() {
  need_host
  say "코드 보내기 → $DEPLOY_HOST:$DEPLOY_DIR"
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    git status --short --untracked-files=no
    die "커밋되지 않은 변경이 있습니다. 커밋한 뒤 다시 실행하세요(보내는 것은 커밋된 HEAD뿐입니다)."
  fi
  local rev
  rev="$(git log -1 --format='%h %ad %s' --date=format:'%Y-%m-%d %H:%M')"
  remote_cmd "test -d '$DEPLOY_DIR' && test -w '$DEPLOY_DIR'" </dev/null ||
    die "$DEPLOY_DIR 이 없거나 쓸 수 없습니다. setup을 먼저 하세요."
  git archive HEAD | remote_cmd "tar xf - -C '$DEPLOY_DIR'"
  printf '%s\n' "$rev" | remote_cmd "cat > '$DEPLOY_DIR/.deployed'"
  echo "보냄: $rev"
}

cmd_up() {
  need_host
  say "서버에서 빌드하고 띄우기"
  remote_sh <<'EOF'
cd "$DIR"
[ -f .env ] || { echo ".env가 없습니다. 먼저 deploy/server.sh env" >&2; exit 1; }
if [ -n "$(docker compose --profile app ps -q --status running app 2>/dev/null)" ]; then
  echo "-- 띄우기 전 백업"
  bash deploy/backup.sh
fi
echo "-- 빌드하고 띄우기 (처음에는 몇 분 걸린다)"
docker compose --profile app up -d --build --remove-orphans
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
  echo "앱이 준비되지 않았습니다($state). 로그: deploy/server.sh logs app" >&2
  exit 1
fi
# 오래된 이미지 정리(디스크가 작다)
docker image prune -f >/dev/null
EOF
  check_url
}

site_url() {
  if [[ -n "$DEPLOY_URL" ]]; then
    printf '%s' "$DEPLOY_URL"
  else
    printf 'https://%s' "$(remote_cmd "sed -n 's/^BARO_DOMAIN=//p' '$DEPLOY_DIR/.env'" </dev/null)"
  fi
}

check_url() {
  local url body i
  url="$(site_url)"
  # 처음에는 인증서를 받는 동안 잠시 걸린다.
  for i in 1 2 3 4 5 6; do
    if body="$(curl -fsS -m 10 "$url/healthz" 2>&1)"; then
      echo "접속 확인: $url/healthz → $body"
      return 0
    fi
    sleep 10
  done
  echo "이 PC에서 $url 에 접속하지 못했습니다: $body" >&2
  echo "DNS(deploy/server.sh check), 방화벽(80·443), 인증서(deploy/server.sh logs caddy)를 확인하세요." >&2
  return 1
}

cmd_deploy() { cmd_push; cmd_up; }

cmd_status() {
  need_host
  say "서버 상태"
  remote_sh <<'EOF'
cd "$DIR"
echo "배포된 커밋: $(cat .deployed 2>/dev/null || echo '없음')"
docker compose --profile app ps
df -h "$DIR" | awk 'NR==2 {print "디스크 남은 공간: " $4}'
EOF
  check_url || true
}

cmd_logs() {
  need_host
  local service="${1:-app}" lines="${2:-100}"
  [[ "$service" =~ ^[a-z]+$ && "$lines" =~ ^[0-9]+$ ]] || die "사용법: deploy/server.sh logs [서비스] [줄수]"
  remote_sh "$service" "$lines" <<'EOF'
cd "$DIR"
docker compose --profile app logs --tail="$2" "$1"
EOF
}

cmd_backup() {
  need_host
  say "서버 백업"
  remote_sh <<'EOF'
cd "$DIR"
bash deploy/backup.sh
EOF
}

cmd_fetch() {
  need_host
  say "서버의 최근 백업 받아 오기 → backups/server/"
  local name
  name="$(remote_cmd "ls -1 '$DEPLOY_DIR/backups' 2>/dev/null | grep -E '^[0-9]{8}-[0-9]{6}$' | tail -1" </dev/null)"
  [[ -n "$name" ]] || die "서버에 백업이 없습니다. deploy/server.sh backup을 먼저 하세요."
  mkdir -p backups/server
  remote_cmd "tar -C '$DEPLOY_DIR/backups' -cf - '$name'" </dev/null | tar xf - -C backups/server
  ls -lh "backups/server/$name"
  echo "받음: backups/server/$name (NAS 등 다른 곳에도 복사해 두세요)"
}

cmd_migrate() {
  need_host
  say "이 PC의 데이터 → 서버"
  echo "서버의 현재 DB와 첨부파일을 지우고 이 PC의 데이터로 바꿉니다."
  local answer name
  read -rp "계속하려면 yes를 입력하세요: " answer
  [[ "$answer" == yes ]] || die "취소했습니다."
  remote_cmd "test -f '$DEPLOY_DIR/.env' && test -f '$DEPLOY_DIR/deploy/restore.sh'" </dev/null ||
    die "서버에 코드와 .env가 있어야 합니다. 먼저 push, env를 하세요."

  name="to-server-$(date +%Y%m%d-%H%M%S)"
  echo "-- 이 PC에서 백업 (backups/$name)"
  deploy/backup.sh "backups/$name"
  echo "-- 서버로 복사"
  tar -C backups -cf - "$name" | remote_cmd "mkdir -p '$DEPLOY_DIR/backups' && tar xf - -C '$DEPLOY_DIR/backups'"
  echo "-- 서버에서 복원"
  remote_sh "$name" <<'EOF'
cd "$DIR"
if [ -n "$(docker compose --profile app ps -q app 2>/dev/null)" ]; then
  echo "-- 복원 전 서버 데이터 백업"
  bash deploy/backup.sh
fi
bash deploy/restore.sh "backups/$1" -y
docker compose --profile app up -d --build
EOF
  check_url
  echo "복원했습니다. 첨부파일이 있는 글을 열어 내려받기가 되는지 확인하세요."
}

cmd_cron() {
  need_host
  say "서버 매일 백업 등록 (03:00, 14일 지난 백업은 지움)"
  remote_sh <<'EOF'
line="0 3 * * * cd $DIR && bash deploy/backup.sh >> backups/backup.log 2>&1 && find backups -maxdepth 1 -type d -name '20*' -mtime +14 -exec rm -r {} +"
if crontab -l 2>/dev/null | grep -qF "$DIR && bash deploy/backup.sh"; then
  echo "이미 등록되어 있습니다."
else
  mkdir -p "$DIR/backups"
  (crontab -l 2>/dev/null; echo "$line") | crontab -
  echo "등록했습니다."
fi
crontab -l | grep -F deploy/backup.sh
EOF
}

case "${1:-}" in
  key) cmd_key ;;
  setup) cmd_setup ;;
  harden) cmd_harden ;;
  check) cmd_check ;;
  env) cmd_env ;;
  push) cmd_push ;;
  up) cmd_up ;;
  deploy) cmd_deploy ;;
  status) cmd_status ;;
  logs) shift; cmd_logs "$@" ;;
  backup) cmd_backup ;;
  fetch) cmd_fetch ;;
  migrate) cmd_migrate ;;
  cron) cmd_cron ;;
  *) usage; [[ -z "${1:-}" ]] || exit 1 ;;
esac
