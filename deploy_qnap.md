# QNAP NAS 배포 매뉴얼

> **보류(2026-11-18):** 이 NAS의 Container Station이 이미지를 풀어 저장할 때 파일을 망가뜨려(아래 "NAS 안에서 단계별로 진행") 배포를 멈췄다. 운영은 iwinv 클라우드로 한다([deploy_iwinv.md](deploy_iwinv.md)). NAS는 백업 보관에 쓴다.

바로채널을 사내 QNAP NAS(gpnas, 192.168.0.114)의 Container Station에 올리는 절차다. 두 단계로 나눈다.

| 단계 | 접속 주소 | 쓰는 것 | 언제 |
|---|---|---|---|
| **1차: 사내망 IP 접속** | `http://192.168.0.114:8000` | 앱·PostgreSQL·MinIO (Caddy 없음) | 지금 |
| **2차: 도메인 + HTTPS** | `https://<도메인>:<HTTPS 포트>` | 1차 + Caddy(인증서), Cloudflare | 도메인을 정한 뒤 |

1차에서 쌓은 데이터는 2차로 넘어갈 때 그대로 이어진다(같은 Docker 볼륨을 쓴다). 2차는 `.env` 몇 줄을 바꾸고 다시 띄우는 것으로 끝난다.

## 빠른 진행: `deploy/nas.sh`

1차 배포는 [deploy/nas.sh](deploy/nas.sh)로 **이 PC(프로젝트 폴더)에서** 끝낼 수 있다. 스크립트가 SSH로 NAS에 명령을 보낸다. 각 단계가 무엇을 하는지는 아래 1~9장에 있다.

| 순서 | 명령 | 하는 일 | 매뉴얼 |
|---|---|---|---|
| 0 | `git commit` | 보낼 코드를 커밋한다. 커밋되지 않은 변경이 있으면 스크립트가 멈춘다 | 3장 |
| 1 | QTS에서 공유 폴더 `barochannel` 만들기 | 코드·`.env`·백업을 둘 폴더. 스크립트는 만들지 않는다 | 2.5 |
| 1-1 | `deploy/nas.sh key` | (선택) 이 PC의 SSH 키를 NAS에 등록한다. QTS 홈 폴더 서비스가 켜져 있어야 한다 | 2.1 |
| 2 | `deploy/nas.sh check` | docker·compose 버전(2.24 이상), bash, 8000 포트, 메모리를 점검한다 | 1장 |
| 3 | `deploy/nas.sh push` | 커밋된 코드(HEAD)를 NAS 설치 폴더로 보낸다 | 3장 |
| 4 | `deploy/nas.sh env` | 관리자 이메일·비밀번호·회사명을 묻고 NAS에 `.env`를 만든다. SECRET_KEY와 DB·MinIO 비밀번호는 임의 값으로 넣고, 1차 설정(`COMPOSE_FILE`, 포트 8000)을 켠다. 이미 있으면 덮어쓰지 않는다 | 4장 |
| 5 | `deploy/nas.sh up` | NAS에서 빌드하고 띄운 뒤, 앱이 준비될 때까지 기다리고 이 PC에서 `http://192.168.0.114:8000/healthz`로 확인한다 | 5장 |
| 6 | `deploy/nas.sh migrate` | (선택) 이 PC의 DB와 첨부파일을 백업해 NAS로 옮기고 복원한다. `yes`를 입력해야 진행한다 | 6장 |
| 7 | `deploy/nas.sh cron` | NAS에 매일 03:00 백업을 등록한다(관리자 비밀번호를 물을 수 있다) | 8장 |

그 뒤 브라우저에서 `http://192.168.0.114:8000`에 들어가 4번에서 정한 관리자 계정으로 로그인한다.

이후 자주 쓰는 명령:

```bash
deploy/nas.sh deploy          # 코드를 고치고 커밋한 뒤: push + (백업 후) 다시 빌드
deploy/nas.sh ship            # deploy와 같지만 앱 이미지를 이 PC에서 빌드해 보낸다(NAS 빌드가 실패할 때)
deploy/nas.sh status          # 배포된 커밋, 컨테이너 상태, 접속 확인
deploy/nas.sh logs app 200    # 앱 로그 200줄 (서비스: app, postgres, minio)
deploy/nas.sh backup          # 지금 백업
```

- 대상은 환경 변수로 바꾼다: `NAS_HOST`(기본 `gp@192.168.0.114`), `NAS_DIR`(기본 `/share/barochannel`), `NAS_URL`(기본 `http://192.168.0.114:8000`). 2차로 넘어간 뒤에는 `NAS_URL=https://<도메인>:<포트>`로 실행한다.
- SSH 키를 등록하지 않아도 쓸 수 있다. 연결을 2분 동안 재사용하므로 비밀번호를 명령마다 한 번쯤만 묻는다.
- 스크립트는 SSH가 되지 않는 환경에서 흉내 내어 시험했다. 실제 NAS에서 처음 돌릴 때는 `check`부터 한 단계씩 결과를 보며 진행한다.

## NAS 안에서 단계별로 진행 (이미지 검증)

이 NAS의 Container Station(docker 27.1.2-qnap8)은 이미지를 풀어 저장할 때 **파일을 오류 없이 망가뜨리는 일이 있다**. 같은 이미지를 이 PC와 NAS에서 `dpkg --verify`로 비교했더니 NAS 쪽만 `postgres:17`에서 4개, 앱 이미지에서 1개 파일이 원본과 달랐다(2026-11-18). PostgreSQL이 `CREATE DATABASE`에서 Segmentation fault로 죽은 것도 이 때문으로 본다. 그래서 `deploy/nas.sh ship`처럼 한 번에 띄우지 않고, NAS에 접속해 **이미지마다 원본과 같은지 확인한 뒤** 띄운다.

검증은 [deploy/verify-image.sh](deploy/verify-image.sh)로 한다. 이 PC에서 이미지의 모든 파일 SHA-256 목록을 만들고(`make`), NAS에서 같은 이미지의 파일과 비교한다(`check`).

### A. 이 PC에서 준비

```bash
git status                                     # 변경을 커밋해 둔다
deploy/nas.sh push                             # 코드(검증 스크립트, 1차 설정 포함)를 NAS로
deploy/nas.sh pack                             # .ship/에 앱 이미지·검증 목록·이미지 버전을 만든다
scp .ship/* gp@192.168.0.114:/share/barochannel/.ship/
```

`.ship/`에 생기는 파일:

| 파일 | 내용 |
|---|---|
| `app-image.tar.gz` | 한 층으로 합친 앱 이미지(docker load용, 약 100MB) |
| `app.sha256`, `postgres.sha256`, `minio.sha256` | 이미지별 파일 목록과 SHA-256 |
| `images.txt` | NAS에서 받을 PostgreSQL·MinIO 이미지의 정확한 버전(digest) |
| `SHA256SUMS` | 위 파일들이 온전히 도착했는지 확인용 |

### B. NAS에 접속

```bash
ssh gp@192.168.0.114
```

접속할 때마다 먼저 실행한다(docker 경로와 설정 폴더):

```bash
CS=$(getcfg container-station Install_Path -f /etc/config/qpkg.conf)
export PATH=$PATH:$CS/bin:$CS/usr/bin DOCKER_CONFIG=/share/barochannel/.docker
cd /share/barochannel
```

### C. 받은 파일 확인

```bash
(cd .ship && sha256sum -c SHA256SUMS)          # 모두 OK
```

### D. 이전 시도 정리

이전에 받은 이미지는 망가졌을 수 있으므로 지운다. 아직 데이터가 없을 때만 DB 볼륨도 지운다(초기화 도중 죽은 볼륨).

```bash
docker compose --profile app down
docker rmi postgres:17 postgres:17-bookworm barochannel-app:latest 2>/dev/null
docker rmi "$(awk '$1=="minio"{print $2}' .ship/images.txt)" 2>/dev/null
docker volume rm barochannel_postgres-data barochannel_minio-data 2>/dev/null   # 데이터가 없을 때만
docker image prune -f
```

### E. PostgreSQL 이미지 받고 검증

```bash
P=$(awk '$1=="postgres"{print $2}' .ship/images.txt)
docker pull "$P" && docker tag "$P" postgres:17-bookworm
deploy/verify-image.sh check postgres:17-bookworm .ship/postgres.sha256
```

`정상 (파일 … 모두 원본과 같음)`이 나오면 다음으로. `손상 …개`가 나오면 지우고 다시 받는다.

```bash
docker rmi postgres:17-bookworm "$P"          # 그리고 E를 다시
```

### F. MinIO 이미지 받고 검증

```bash
M=$(awk '$1=="minio"{print $2}' .ship/images.txt)
docker pull "$M"
deploy/verify-image.sh check "$M" .ship/minio.sha256
```

손상이면 `docker rmi "$M"` 후 다시.

### G. 앱 이미지 불러오고 검증

```bash
docker load -i .ship/app-image.tar.gz
deploy/verify-image.sh check barochannel-app:latest .ship/app.sha256
```

`docker load`가 `invalid tar header`, `invalid diffID`로 실패하면 그대로 다시 실행한다. 손상이면 `docker rmi barochannel-app:latest` 후 다시.

### H. 띄우기

```bash
docker compose --profile app up -d --no-build
docker compose --profile app ps                # app·postgres가 (healthy)
curl -s http://127.0.0.1:8000/healthz          # {"status":"ok"}
```

PC 브라우저에서 `http://192.168.0.114:8000`에 들어가 관리자 계정으로 로그인한다. 다 되면 `.ship/`의 파일은 지워도 된다(`rm .ship/*`).

### 판단 기준

- `check`가 실패하면 같은 이미지로 한 번 더 `check`한다. 검사도 docker를 거쳐 파일을 읽으므로, 두 번의 결과에서 **다른 파일**이 손상으로 나오면 읽는 과정에서 망가지는 것이다(이 경우도 NAS docker가 불안정하다는 뜻이다).
- 몇 번을 다시 받아도 `정상`이 나오지 않으면 이 NAS의 Container Station으로는 운영할 수 없다. QTS·Container Station 업데이트와 QNAP 지원 문의, 가상머신(Virtualization Station) 안의 docker, 다른 PC에서 운영 중 하나를 고른다.
- `정상`으로 띄운 뒤에도 앱이나 DB가 이유 없이 죽으면 같은 문제를 의심한다(실행 중 쓰는 파일은 일반 공유 폴더·볼륨에 저장되므로 검증 대상이 아니다).

---

# 1차: 사내망에서 IP로 접속

## 1. 구성

```
 사내 PC 브라우저 ── http://192.168.0.114:8000 ──▶ QNAP NAS
                                                   ├─ QTS (8080, 443, 80, 8081, 8880, 8888 …) ← 그대로
                                                   └─ Container Station (프로젝트 barochannel)
                                                       ├─ app       8000  ← 사내망에 여는 것은 이것뿐
                                                       ├─ postgres  127.0.0.1:5432 (NAS 안에서만)
                                                       └─ minio     127.0.0.1:9000·9001 (NAS 안에서만)
```

| 항목 | 값 |
|---|---|
| NAS | TS-453D, Celeron J4125(x86_64), 메모리 4GB, QTS 5.2.9 |
| Docker / Compose | 27.1.2 / v2.29.1 (Container Station) |
| 설치 폴더 | `/share/barochannel` |
| 데이터 | Docker 볼륨 `barochannel_postgres-data`, `barochannel_minio-data` |

1차 설정은 [deploy/compose.lan.yml](deploy/compose.lan.yml)에 있다. `.env`의 `COMPOSE_FILE` 한 줄로 켠다.

- 앱을 8000 포트로 바로 연다.
- HTTPS 프록시(Caddy)는 띄우지 않는다.
- 세션 쿠키를 HTTP에서도 오가게 한다(`BARO_SECURE_COOKIES=false`).
- 프록시가 없으므로 접속자가 보낸 `X-Forwarded-*` 헤더는 믿지 않는다.

**보안상 알아둘 점:** HTTP라서 로그인 비밀번호와 세션이 암호화되지 않은 채 사내망을 오간다. 공유기에서 8000을 바깥으로 포트포워딩하지 않는다. 손님용 Wi-Fi는 사내망과 분리해 둔다. 외부에서 쓰려면 VPN으로 들어온다(9장).

## 2. NAS 준비

### 2.1 SSH
- QTS **제어판 → 네트워크 및 파일 서비스 → Telnet/SSH** → "SSH 연결 허용"(포트 22).
- SSH는 관리자 그룹 계정만 들어갈 수 있다. `gp` 계정으로 `docker`가 되는 것을 확인했다.
- SSH 키를 쓰려면 홈 폴더가 있어야 한다. **제어판 → 권한 → 사용자 → 홈 폴더 → "모든 사용자에 대해 홈 폴더 서비스 활성화"**. 켜지 않으면 키 등록이 `Could not chdir to home directory`로 실패한다. 키 없이 비밀번호로 해도 된다.
- PC에서 비밀번호 없이 들어가려면 SSH 키를 등록한다.

  ```bash
  ssh-keygen -t ed25519            # 키가 없을 때만
  ssh-copy-id gp@192.168.0.114
  ```

### 2.2 고정 IP
모든 PC가 `192.168.0.114`로 접속하므로 IP가 바뀌면 끊긴다. 공유기의 **DHCP 고정 할당**으로 묶거나, QTS **네트워크 및 가상 스위치**에서 고정 IP로 정한다.

### 2.3 방화벽
QuFirewall을 쓰면 사내 대역 `192.168.0.0/24`에 TCP 8000을 허용한다. 쓰지 않으면 할 일이 없다.

### 2.4 인터넷
처음 빌드할 때 Docker 이미지와 파이썬 패키지를 받으므로 NAS가 인터넷에 나갈 수 있어야 한다.

### 2.5 설치용 공유 폴더
코드, `.env`, 백업을 둘 공유 폴더를 만든다. 다른 서비스(Public, Web …)처럼 데이터 볼륨(`CACHEDEV1_DATA`)에 생기고, File Station과 HBS 3 백업에서도 보인다.

1. QTS **제어판 → 권한 → 공유 폴더 → 생성 → 공유 폴더**
2. 폴더 이름 `barochannel`, 디스크 볼륨은 데이터 볼륨(DataVol1)
3. 사용자 권한: `gp`에 **읽기/쓰기**. 다른 사용자는 **접근 거부**(`.env`에 비밀번호가 있다)
4. 만든 뒤 SSH에서 `ls -ld /share/barochannel`로 `/share/barochannel -> CACHEDEV1_DATA/barochannel`처럼 보이면 된다

`/share` 바로 아래에 `mkdir`로 폴더를 만들지 않는다. 데이터 디스크가 아니라 시스템 영역에 생겨 재부팅 때 사라질 수 있다. 스크립트도 이 폴더를 만들지 않고, 없으면 멈춘다.

Docker 볼륨(DB·첨부파일 데이터)은 이 폴더가 아니라 Container Station의 저장 위치에 생긴다. 백업 파일(`backups/`)만 이 폴더에 쌓인다.

## 3. 코드 보내기

NAS에는 git이나 GitHub 인증을 두지 않는다. PC에서 커밋된 코드를 묶어 보낸다. 커밋되지 않은 변경은 가지 않는다.

PC(프로젝트 폴더)에서:

```bash
git status                       # 보낼 변경이 모두 커밋됐는지 확인
git archive HEAD | ssh gp@192.168.0.114 \
  'tar x -C /share/barochannel'
```

- `.env`, `backups/`처럼 git에 없는 파일은 가지 않는다. NAS의 `.env`도 덮어쓰지 않는다.
- `/share/barochannel`은 2.5에서 만든 공유 폴더다. 다른 이름으로 만들었다면 이 문서의 경로를 바꿔 읽고, 스크립트는 `NAS_DIR=/share/<이름>`으로 실행한다.

## 4. `.env` 만들기

NAS에서:

```bash
cd /share/barochannel
cp .env.example .env
chmod 600 .env
vi .env
```

임의 값은 PC에서 만들어 붙여 넣는다.

```bash
python -c "import secrets;print(secrets.token_urlsafe(48))"   # BARO_SECRET_KEY
python -c "import secrets;print(secrets.token_hex(24))"       # BARO_DB_PASSWORD, BARO_S3_SECRET_KEY
```

| 키 | 값 | 비고 |
|---|---|---|
| `BARO_DEBUG` | `false` | 운영 모드 |
| `BARO_SECRET_KEY` | 임의 값(token_urlsafe) | 바꾸면 모두 다시 로그인 |
| `BARO_ADMIN_EMAIL` / `BARO_ADMIN_PASSWORD` | 시스템 관리자 계정 | 비밀번호 8자 이상 |
| `BARO_COMPANY_NAME` | `좋은상자` | 화면 왼쪽 위 |
| `BARO_DB_PASSWORD` | 임의 값(token_hex) | **처음 띄우기 전에** 정한다(아래) |
| `BARO_S3_SECRET_KEY` | 임의 값(token_hex) | MinIO 루트 비밀번호, 8자 이상 |
| `BARO_INTEGRATION_ALLOWED_HOSTS` | 업무 채널이 부를 사내 시스템 | 없으면 비운다 |
| `COMPOSE_FILE` | `docker-compose.yml:deploy/compose.lan.yml` | **주석(`#`)을 지운다.** 1차 설정을 켠다 |
| `BARO_APP_PORT` | `8000` | 주석을 지운다 |

- `BARO_DOMAIN`, `CLOUDFLARE_API_TOKEN`, `BARO_HTTP_PORT`, `BARO_HTTPS_PORT`, `BARO_VPN_IP`는 1차에서 쓰지 않는다. 그대로 둔다.
- `BARO_HOST`, `BARO_PORT`, `BARO_S3_ENDPOINT_URL`은 컨테이너 안에서 compose가 정한 값을 쓰므로 그대로 둔다.
- **PostgreSQL 비밀번호는 DB 볼륨을 처음 만들 때 한 번만 적용된다.** 처음 띄운 뒤 `.env`만 바꾸면 앱이 DB에 못 붙는다. 나중에 바꾸려면 [README.md](README.md)의 "저장소 비밀번호"를 따른다.
- 값에 `$`를 넣지 않는다. compose가 변수로 읽는다(token_hex 값은 안전하다).

## 5. 빌드하고 띄우기

NAS에서:

```bash
cd /share/barochannel
docker compose --profile app up -d --build
docker compose --profile app ps
```

- 처음에는 이미지 받기와 빌드로 몇 분 걸린다. 앱은 시작할 때마다 DB 마이그레이션을 맞춘 뒤 뜬다.
- `ps`에 `app`, `postgres`, `minio` **세 개**가 보이고, app 포트가 `0.0.0.0:8000->8000`이면 맞다.
- `caddy`가 보이면 `COMPOSE_FILE` 줄의 주석이 안 풀린 것이다.

PC에서 확인:

1. `http://192.168.0.114:8000/healthz` → `{"status":"ok"}`
2. `http://192.168.0.114:8000` → 로그인 화면
3. `.env`의 관리자 계정으로 로그인 → 첫 화면이 열리고 새로고침해도 로그인이 유지된다

Container Station 화면에도 `barochannel`로 보인다. 상태를 보는 데는 써도 되지만, 시작·정지·다시 만들기는 SSH의 `docker compose`로 한다. 그래야 `.env`와 1차 설정 파일을 항상 같이 읽는다.

## 6. 기존 데이터 옮기기 (개발 PC → NAS)

개발 PC에서 쓰던 DB와 첨부파일을 가져갈 때 한 번 한다. 새로 시작한다면 건너뛴다.

PC(프로젝트 폴더)에서:

```bash
deploy/backup.sh                                     # backups/<시각>/ 생성
scp -r backups/<시각> gp@192.168.0.114:/share/barochannel/backups/
```

NAS에서:

```bash
cd /share/barochannel
docker compose up -d postgres minio                  # 저장소만 띄운다
deploy/restore.sh backups/<시각>                     # 확인 질문에 y
docker compose --profile app up -d --build
```

- 5장을 이미 했어도 된다. 복원 스크립트가 앱을 잠시 멈추고 DB와 첨부파일을 백업 내용으로 바꾼 뒤 다시 띄운다.
- NAS의 DB 계정·비밀번호가 PC와 달라도 된다. 복원할 때 NAS 계정 소유로 다시 만든다.
- 복원한 뒤 첨부파일이 있는 글을 하나 열어 내려받기가 되는지 확인한다. MinIO 계정을 PC와 다르게 정한 상태에서 복원하는 경우는 아직 시험하지 않았다.

## 7. 업데이트

PC에서:

```bash
git status                                           # 커밋 확인
git archive HEAD | ssh gp@192.168.0.114 'tar x -C /share/barochannel'
```

NAS에서:

```bash
cd /share/barochannel
deploy/backup.sh                                     # 업데이트 전 백업
docker compose --profile app up -d --build
docker compose --profile app ps
```

- `git archive`는 덮어쓰기만 한다. 저장소에서 지운 파일은 NAS에 남는다. 문제가 되면 `.env`와 `backups/`만 남기고 폴더를 비운 뒤 다시 보낸다.
- `.env.example`에 새 키가 생겼으면 NAS의 `.env`에도 넣는다.
- 되돌리려면 이전 커밋을 `git archive <커밋>`으로 보내 다시 빌드한다. DB 마이그레이션이 바뀐 업데이트라면 업데이트 전 백업을 `restore.sh`로 되돌린다.

## 8. 백업

```bash
cd /share/barochannel
deploy/backup.sh                     # backups/<시각>/barochannel.dump, minio-data.tgz
```

백업하는 몇 초 동안 앱이 멈춘다. 정기 백업은 QTS cron에 등록한다(매일 새벽 3시 예시).

```bash
echo '0 3 * * * cd /share/barochannel && deploy/backup.sh >> backups/backup.log 2>&1' >> /etc/config/crontab
crontab /etc/config/crontab && /etc/init.d/crond.sh restart
```

- QTS는 `crontab -e`로 고친 내용이 재부팅 때 사라진다. 반드시 `/etc/config/crontab`을 고친다.
- `backup.sh`는 bash로 돈다. 처음 한 번은 손으로 돌려 오류가 없는지 본다.
- `backups/`는 같은 NAS 안에 있다. HBS 3(Hybrid Backup Sync)로 다른 디스크나 클라우드에 복사해야 NAS 고장에 대비할 수 있다.
- 오래된 백업은 직접 지운다(예: `find backups -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +`).

## 9. 외부 접속 (선택: QNAP VPN)

사무실 밖에서는 VPN으로 사내망에 들어온 뒤 같은 주소(`http://192.168.0.114:8000`)로 접속한다. 바로채널 포트(8000)는 바깥에 열지 않는다. 인터넷 구간은 VPN 터널이 암호화한다.

1. QTS **App Center → QVPN Service** 설치 → **VPN 서버 → WireGuard** 사용 설정.
2. 공유기에서 WireGuard 포트(기본 UDP 51820) **하나만** NAS(192.168.0.114)로 포트포워딩한다.
3. 사무실 공인 IP가 바뀔 수 있으니 **myQNAPcloud**에서 DDNS 주소를 만들어 접속 주소로 쓴다.
4. QVPN에서 사용자별 피어를 만들고, 설정 파일이나 QR을 각 기기의 WireGuard 앱에 넣는다.
5. VPN 접속자는 별도 대역(QVPN에서 정한 가상 IP 대역)에서 들어온다. QuFirewall을 켰다면 그 대역에도 TCP 8000을 허용한다.

---

# 2차: 도메인 + HTTPS (도메인 결정 후)

1차 운영 중에 도메인이 정해지면 진행한다. 데이터는 그대로 이어지고, 바뀌는 것은 접속 주소뿐이다.

## 10. 먼저 정할 것

| 항목 | 내용 |
|---|---|
| 도메인 | 예: `gw.goodpackage.co.kr`. 상위 도메인의 DNS를 Cloudflare가 관리해야 한다(11.1) |
| HTTP·HTTPS 포트 | NAS에서 80, 443, 8080, 8081, 8880, 8888을 이미 쓴다. 비어 있는 두 포트를 고른다. HTTPS 포트가 접속 주소에 붙는다 |
| 8000 포트 | 2차에서는 앱을 직접 열지 않는다. 기존 `http://…:8000` 주소는 닫힌다 |

빈 포트 확인(NAS에서):

```bash
netstat -tln | grep -E ':(8443|9443)\b' || echo 비어있음      # 확인할 포트를 바꿔 넣는다
```

## 11. Cloudflare 설정

### 11.1 왜 Cloudflare가 필요한가

브라우저가 경고 없이 HTTPS로 접속하려면 공인 인증 기관(Let's Encrypt)이 발급한 인증서가 있어야 한다. Let's Encrypt는 "이 도메인의 주인이 맞다"는 것을 두 가지 방법으로 확인한다.

| 방식 | 확인 방법 | 이 NAS에서 |
|---|---|---|
| HTTP-01 | 인터넷에서 `http://도메인:80/`으로 들어와 확인 파일을 읽는다 | 불가. NAS가 외부에 열려 있지 않고 80은 QTS가 쓴다 |
| **DNS-01** | 도메인 DNS에 `_acme-challenge` TXT 레코드를 올렸는지 본다 | **가능.** NAS가 바깥에서 보일 필요가 없다 |

바로채널은 DNS-01을 쓴다. Caddy가 Cloudflare API로 TXT 레코드를 직접 올렸다 지우므로, 사람이 할 일은 처음에 토큰 하나 만들어 주는 것뿐이다. 인증서는 90일짜리이고, 만료 30일 전쯤 Caddy가 같은 방식으로 알아서 갱신한다.

그래서 **도메인의 DNS를 Cloudflare가 관리하고 있어야 한다.**

1. <https://dash.cloudflare.com> 에 로그인해, 계정 홈에 도메인(예: `goodpackage.co.kr`)이 있고 상태가 **Active**인지 본다.
2. 없다면 도메인을 산 곳(가비아, 후이즈 등)의 네임서버를 Cloudflare로 바꾼다.
   - Cloudflare → **Add a domain** → 도메인 입력 → Free 요금제
   - Cloudflare가 기존 레코드를 읽어 온다. 홈페이지(`www`), 메일(`MX`) 레코드가 빠짐없이 들어왔는지 반드시 대조한다. 빠지면 네임서버를 바꾸는 순간 홈페이지나 메일이 끊긴다.
   - Cloudflare가 알려 준 네임서버 2개(`xxx.ns.cloudflare.com`)를 도메인 등록 업체 관리 화면에 넣는다.
   - 반영까지 수 분에서 최대 24시간. Cloudflare에서 **Active**가 되면 끝이다.

### 11.2 A 레코드

Cloudflare → 도메인 → **DNS → Records → Add record**

| 항목 | 값 |
|---|---|
| Type | `A` |
| Name | `gw` (도메인 앞부분) |
| IPv4 address | `192.168.0.114` |
| Proxy status | **DNS only (회색 구름)** |
| TTL | Auto |

- **Proxy status는 반드시 DNS only로 둔다.** 주황 구름(Proxied)은 Cloudflare 서버를 거쳐 접속하는 방식이다. Cloudflare는 사내 IP에 닿을 수 없고, 임의 포트도 지원하지 않는다.
- 공용 DNS에 사내 IP가 올라가도 바깥에서 그 IP로 들어올 수는 없다. 접속은 사내망과 VPN 안에서만 된다.
- 확인(PC 명령 프롬프트): `nslookup gw.goodpackage.co.kr` → `Address: 192.168.0.114`

### 11.3 API 토큰

1. Cloudflare 오른쪽 위 사람 아이콘 → **My Profile → API Tokens → Create Token**
2. **Edit zone DNS** 템플릿의 **Use template**
3. 다음처럼 맞춘다.

   | 항목 | 값 |
   |---|---|
   | Permissions | `Zone` · `DNS` · `Edit` |
   | | **Add more** → `Zone` · `Zone` · `Read` |
   | Zone Resources | `Include` · `Specific zone` · 해당 도메인 |
   | TTL | 비워 둔다(만료 없음). 만료를 정하면 그날 이후 인증서 갱신이 실패한다 |

4. **Continue to summary → Create Token**
5. 한 번만 보이는 토큰을 복사한다. 이 값이 `.env`의 `CLOUDFLARE_API_TOKEN`이다.
6. 화면의 `curl … /user/tokens/verify` 예시를 실행해 `"status": "active"`를 확인한다.

토큰은 해당 도메인의 DNS를 바꿀 수 있는 권한이다. NAS의 `.env`(`chmod 600`)에만 둔다. 유출이 의심되면 **Roll**로 재발급하고 `.env`를 고친 뒤 Caddy를 다시 만든다.

### 11.4 NAS에서 나가는 연결

Caddy는 다음 주소로 **나가는** 연결만 쓴다. 들어오는 연결은 필요 없다.

| 목적지 | 포트 | 용도 |
|---|---|---|
| `api.cloudflare.com` | 443 | TXT 레코드 작성·삭제 |
| `acme-v02.api.letsencrypt.org` | 443 | 인증서 발급 |
| `1.1.1.1` | 53 (UDP·TCP) | TXT 반영 확인 |

Let's Encrypt는 같은 도메인 인증서를 일주일에 5번까지만 새로 발급한다. `barochannel_caddy-data` 볼륨을 지우고 다시 띄우기를 반복하지 않는다.

## 12. 2차로 전환

NAS에서:

```bash
cd /share/barochannel
deploy/backup.sh                                     # 전환 전 백업
vi .env
```

`.env` 바꾸기:

| 키 | 바꿀 내용 |
|---|---|
| `COMPOSE_FILE` | **줄을 지우거나 다시 주석 처리한다.** 1차 설정이 꺼지고 Caddy가 같이 뜬다 |
| `BARO_APP_PORT` | 지우거나 주석 처리한다(쓰지 않는다) |
| `BARO_DOMAIN` | 정한 도메인 |
| `CLOUDFLARE_API_TOKEN` | 11.3에서 만든 토큰 |
| `BARO_HTTP_PORT` / `BARO_HTTPS_PORT` | 10장에서 고른 포트. 주석을 지운다 |
| `BARO_POSTGRES_IMAGE` | `postgres:17-bookworm`. **반드시 넣는다.** 1차 DB와 같은 이미지를 유지해야 한다(QNAP에서 기본 `postgres:17`은 죽고, 기반 Debian이 바뀌면 문자열 정렬도 달라진다) |

다시 띄운다.

```bash
docker compose --profile app up -d --build
docker compose --profile app ps                      # app, postgres, minio, caddy 네 개
docker compose logs -f caddy                         # 인증서 발급 확인 (Ctrl+C로 빠져나옴)
```

Caddy 로그에 다음이 보이면 발급이 끝난 것이다.

```
"msg":"certificate obtained successfully","identifier":"<도메인>"
```

PC에서 확인:

1. `https://<도메인>:<HTTPS 포트>` → 자물쇠 표시, 경고 없음
2. `http://<도메인>:<HTTP 포트>` → HTTPS 주소로 넘어감
3. 로그인, 기존 글과 첨부파일 확인
4. `http://192.168.0.114:8000` → 열리지 않음(정상)

전환 후:

- 직원들에게 새 주소를 알린다. 주소가 바뀌므로 모두 한 번 다시 로그인한다.
- QuFirewall을 쓰면 새 포트를 허용하고 8000은 닫는다.
- 1차로 되돌리려면 `.env`의 `COMPOSE_FILE` 줄을 되살리고 `docker rm -f barochannel-caddy-1 && docker compose --profile app up -d`를 실행한다.

---

## 13. 문제 해결

| 증상 | 확인할 것 |
|---|---|
| 1차인데 `caddy`가 뜨고 Restarting을 반복 | `.env`의 `COMPOSE_FILE` 줄 주석이 안 풀렸다. 고친 뒤 `docker rm -f barochannel-caddy-1 && docker compose --profile app up -d` |
| `http://192.168.0.114:8000`이 안 열림 | `docker compose --profile app ps`에서 app 포트가 `0.0.0.0:8000`인지, QuFirewall, NAS IP가 바뀌었는지 |
| 로그인 후 바로 로그인 화면으로 돌아옴 | 1차: `COMPOSE_FILE`이 빠져 쿠키가 HTTPS 전용이다. 2차: HTTPS 주소로 들어왔는지 |
| `app`이 healthy가 안 됨 | `docker compose logs app`. `password authentication failed`면 DB를 만든 뒤 `BARO_DB_PASSWORD`를 바꾼 경우다(4장) |
| `mkdir …/.qpkg/container-station/homes/<사용자>: permission denied` (빌드 중) | Container Station의 docker가 admin만 쓸 수 있는 곳에 사용자 설정을 만들려는 것이다. `deploy/nas.sh`는 알아서 피한다. NAS에서 직접 `docker compose`를 쓸 때는 먼저 `export DOCKER_CONFIG=/share/barochannel/.docker` |
| 빌드 중 `failed to register layer: archive/tar: invalid tar header` | NAS가 받은 이미지 데이터가 깨진 것이다. `deploy/nas.sh ship`으로 이 PC에서 빌드한 앱 이미지를 보내 띄운다. NAS에서 고치려면 `docker builder prune -f` 뒤 다시 `up`, 그래도 같으면 Container Station의 레지스트리 미러 설정을 확인한다 |
| `failed to create network …: execute qnetwork-tool [vswitch create …] failed` / `AddNATrule … failed` | QNAP이 compose용 새 네트워크(가상 스위치)를 만들지 못한 것이다. 1차 설정은 네트워크를 만들지 않고 NAS 네트워크를 그대로 쓴다(`deploy/compose.lan.yml`의 `network_mode: host`). `.env`의 `COMPOSE_FILE` 줄이 켜져 있는지 확인한다. 실패하며 남은 네트워크가 있으면 `docker network rm barochannel_default` |
| postgres가 `exited (2)`, 로그에 `terminated by signal 11: Segmentation fault`, `Failed process was running: CREATE DATABASE` | Debian 13(trixie) 기반 `postgres:17`이 QNAP에서 죽는 것이다. 1차 설정은 `postgres:17-bookworm`을 쓴다. 이미 반쯤 만들어진 DB 볼륨은 지우고 다시 띄운다(데이터가 없을 때만): `docker compose --profile app down && docker volume rm barochannel_postgres-data`. 2차로 넘어갈 때는 `.env`에 `BARO_POSTGRES_IMAGE=postgres:17-bookworm`을 넣는다 |
| `!override` 관련 오류 | Docker Compose 2.24 이상이 필요하다(NAS는 2.29.1). Container Station을 업데이트한다 |
| 2차: Caddy가 Restarting 반복 | `docker compose logs caddy`. 토큰이 비었거나 틀리면 `provision tls.issuance.acme` 오류가 난다 |
| 2차: `timed out waiting for record to fully propagate` | NAS에서 1.1.1.1:53으로 나가는 연결이 막혔는지 본다 |
| 2차: `rateLimited` | Let's Encrypt 발급 한도(주 5회). `caddy-data` 볼륨을 지우지 말고 기다린다 |
| 2차: 도메인이 안 열림(`nslookup`에 주소 없음) | ① Cloudflare A 레코드 확인 ② 일부 공유기·사내 DNS는 공용 도메인이 사설 IP를 가리키면 응답을 막는다(DNS 리바인딩 보호). 공유기에서 예외로 두거나 PC의 `C:\Windows\System32\drivers\etc\hosts`에 `192.168.0.114 <도메인>`을 넣는다 |
| 2차: 브라우저 인증서 경고 | IP로 들어왔는지 본다. 인증서는 도메인 이름에 발급된다 |
| 디스크·메모리 부족 | `docker system df`, `free -m`. 쓰지 않는 이미지는 `docker image prune` |

자주 쓰는 명령:

```bash
cd /share/barochannel
docker compose --profile app ps                      # 상태
docker compose --profile app logs -f app             # 앱 로그
docker compose --profile app restart app             # 앱만 다시 시작
docker compose --profile app stop                    # 모두 정지 (데이터 유지)
docker compose --profile app up -d                   # 모두 시작
```

`docker compose down -v`는 DB와 첨부파일 볼륨을 지운다. 쓰지 않는다.
