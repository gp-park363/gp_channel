# iwinv 클라우드 배포 매뉴얼

바로채널을 iwinv(스마일서브) 클라우드 서버에 올리는 절차다. 인터넷에 열린 서버이므로 처음부터 도메인과 HTTPS로 운영한다. 서버 준비부터 배포·백업까지 이 PC에서 [deploy/server.sh](deploy/server.sh)로 한다.

QNAP NAS 배포는 보류했다. Container Station이 이미지를 풀 때 파일을 망가뜨려서다([deploy_qnap.md](deploy_qnap.md)). NAS는 백업 보관에 쓴다(8장).

## 0. 구성

```
 사용자 브라우저 ── https://gw.goodpackage.co.kr ──▶ iwinv 서버 (Ubuntu 24.04)
                                                    ├─ caddy     80·443 (HTTPS, 인증서 자동 발급·갱신)
                                                    ├─ app       8000 (컨테이너끼리만)
                                                    ├─ postgres  127.0.0.1:5434 (서버 안에서만)
                                                    └─ minio     127.0.0.1:9000·9001 (서버 안에서만)
 이 PC ── SSH(deploy 계정, 키) ──▶ 배포·백업 ── 백업 파일 ──▶ 이 PC, NAS
```

| 항목 | 값 |
|---|---|
| 서버 | Ubuntu 24.04, 메모리 2GB 권장(1GB면 스왑 2GB를 만든다), 디스크 25GB 이상 |
| 설치 폴더 | `/srv/barochannel` |
| 접속 계정 | `deploy`(docker 사용, SSH 키로만). `root`는 처음 준비에만 쓰고 막는다 |
| 열리는 포트 | 22(SSH), 80·443(웹). DB와 첨부 저장소는 서버 안에서만 |
| 인증서 | Let's Encrypt HTTP 확인. Cloudflare 토큰이나 Caddy 플러그인이 필요 없다([deploy/compose.public.yml](deploy/compose.public.yml), [deploy/Caddyfile.public](deploy/Caddyfile.public)) |

## 1. 서버 만들기 (iwinv 콘솔)

화면 이름은 iwinv 콘솔과 조금 다를 수 있다.

1. <https://www.iwinv.kr> 로그인 → **클라우드 서버 → 서버 생성**
2. 운영체제 **Ubuntu 24.04**, 사양은 메모리 2GB(1 vCPU면 충분), 지역은 국내
3. 로그인 방식: root 비밀번호(또는 SSH 키 등록). 비밀번호는 3장에서 키를 등록할 때 한 번만 쓴다
4. iwinv 방화벽(보안 그룹)을 쓰면 **TCP 22, 80, 443**과 UDP 443을 연다
5. 만들어지면 **공인 IP**를 적어 둔다

요금은 월 단위로 나가므로 시험이 끝난 서버는 지운다.

## 2. 도메인 연결 (DNS)

도메인 없이 서버 IP로만 쓸 거면 이 장을 건너뛰고 아래 "도메인 없이 IP로 쓰기"를 본다.

`gw.goodpackage.co.kr`(또는 정한 도메인)이 서버 공인 IP를 가리키게 한다. 도메인 DNS를 관리하는 곳(Cloudflare, 가비아 등)에서:

| 항목 | 값 |
|---|---|
| Type | `A` |
| Name | `gw` |
| 값 | 서버 공인 IP |
| Cloudflare Proxy | **DNS only(회색 구름)**. 인증서는 서버의 Caddy가 받는다 |

확인(이 PC): `getent hosts gw.goodpackage.co.kr` → 서버 IP. 반영까지 몇 분 걸릴 수 있다. DNS가 맞아야 6장에서 인증서를 받는다.

### 도메인 없이 IP로 쓰기

도메인이 없어도 `https://<서버 IP>`로 경고 없이 접속할 수 있다. Let's Encrypt가 2026년 1월부터 IP 주소 인증서를 정식으로 발급하고, Caddy 2.11이 이를 받아 온다([deploy/Caddyfile.ip](deploy/Caddyfile.ip)).

- 5장의 `deploy/server.sh env`에서 **도메인을 비워 두면** 이 방식이 된다. `.env`에 `BARO_DOMAIN=<서버 IP>`, `BARO_CADDYFILE=Caddyfile.ip`가 들어간다.
- `.deploy`의 `DEPLOY_HOST`는 `deploy@<서버 IP>`처럼 IP로 적어야 한다(그 IP로 인증서를 받는다).
- IP 인증서는 유효 기간이 약 6일이다. Caddy가 만료 전에 알아서 갱신하므로 할 일은 없지만, 갱신하려면 **80·443이 늘 열려 있어야** 한다.
- 서버 IP가 바뀌면 접속 주소와 인증서가 바뀐다. iwinv에서 IP가 고정인지(서버를 지우고 새로 만들지 않는 한 유지되는지) 확인한다.
- 나중에 도메인을 연결하면: DNS를 설정하고(위 표), 서버 `.env`에서 `BARO_DOMAIN`을 도메인으로 바꾸고 `BARO_CADDYFILE` 줄을 지운 뒤 `deploy/server.sh up`. 데이터는 그대로다.

## 3. 서버 준비 (이 PC에서, 한 번)

접속 대상을 `.deploy`에 적는다(git에 올리지 않는 파일).

`<서버 IP>`는 실제 IP로 바꿔 적는다. 꺾쇠(`<>`)는 넣지 않는다. 예: `DEPLOY_HOST=root@49.247.130.35`

```bash
echo 'DEPLOY_HOST=root@<서버 IP>' > .deploy
deploy/server.sh key            # 이 PC의 SSH 키 등록(root 비밀번호 한 번)
deploy/server.sh setup          # docker, deploy 계정, 방화벽, 스왑, 설치 폴더
```

`setup`이 하는 일:

- 시간대 Asia/Seoul
- docker 설치(Ubuntu 패키지 `docker.io`, `docker-compose-v2`, `docker-buildx`)
- 메모리가 3GB 미만이고 스왑이 없으면 스왑 2GB
- `deploy` 계정을 만들고 docker 그룹에 넣는다. root에 등록한 SSH 키를 복사한다
- `/srv/barochannel` 폴더(`deploy` 소유)
- 방화벽 ufw: 22, 80, 443만 연다

그다음 배포 계정으로 바꾸고 root를 막는다.

```bash
echo 'DEPLOY_HOST=deploy@<서버 IP>' > .deploy
ssh deploy@<서버 IP> true       # 비밀번호 없이 끝나야 한다
deploy/server.sh harden         # root 로그인과 비밀번호 로그인을 막는다
```

`harden`은 `deploy` 키 접속이 되는 것을 먼저 확인하고, 안 되면 아무것도 바꾸지 않는다. 막은 뒤 root가 필요하면 iwinv 콘솔의 웹 콘솔로 들어간다.

## 4. 점검

```bash
deploy/server.sh check
```

docker·compose 버전, 메모리·스왑, 디스크, 80·443 포트, 설치 폴더, 도메인 DNS가 이 서버를 가리키는지 보여 준다.

## 5. 코드 보내기와 `.env`

```bash
git status                      # 커밋되지 않은 변경이 있으면 push가 멈춘다
deploy/server.sh push
deploy/server.sh env            # 도메인, 관리자 이메일·비밀번호, 회사명을 묻는다
```

`env`가 서버에 만드는 `.env`(권한 600):

- `BARO_DEBUG=false`, `BARO_SECRET_KEY`·`BARO_DB_PASSWORD`·`BARO_S3_SECRET_KEY`는 임의 값
- `BARO_DOMAIN`, 관리자 계정, 회사명
- `COMPOSE_FILE=docker-compose.yml:deploy/compose.public.yml`: 공식 Caddy 이미지와 Let's Encrypt HTTP 확인

인터넷에 열린 서버이므로 관리자 비밀번호는 12자 이상으로 정한다. `.env`가 이미 있으면 덮어쓰지 않는다. PostgreSQL 비밀번호는 DB 볼륨을 처음 만들 때 한 번만 적용되므로, 처음 띄운 뒤에는 `.env`를 지우고 다시 만들지 않는다.

## 6. 띄우기

```bash
deploy/server.sh up
```

서버에서 앱 이미지를 빌드하고(처음 몇 분) 띄운 뒤, 앱이 준비되면 이 PC에서 `https://<도메인>/healthz`로 확인한다. 처음에는 인증서를 받는 동안 1분쯤 걸릴 수 있다.

브라우저에서 `https://gw.goodpackage.co.kr` → 관리자 계정으로 로그인.

## 7. 기존 데이터 옮기기 (선택)

이 PC에서 쓰던 DB와 첨부파일을 서버로 옮긴다. 서버의 데이터는 지워진다(지우기 전에 서버에서 백업한다).

```bash
deploy/server.sh migrate        # yes 입력
```

옮긴 뒤 첨부파일이 있는 글을 열어 내려받기가 되는지 본다.

## 8. 백업

```bash
deploy/server.sh cron           # 매일 03:00 백업, 14일 지난 백업은 지운다
deploy/server.sh backup         # 지금 백업
deploy/server.sh fetch          # 가장 최근 백업을 이 PC의 backups/server/로
```

서버가 고장 나면 서버 안의 백업도 같이 잃는다. `fetch`로 받은 백업을 NAS(공유 폴더)에도 복사해 둔다. iwinv에 서버 스냅샷(이미지 백업) 기능이 있으면 함께 쓴다.

복원: 새 서버를 3~5장대로 준비한 뒤 `backups/server/<시각>` 폴더를 서버 `backups/`에 올리고 서버에서 `bash deploy/restore.sh backups/<시각>`.

## 9. 업데이트

```bash
git commit ...                  # 고친 것을 커밋
deploy/server.sh deploy         # push + (백업 후) 빌드·재시작 + 접속 확인
```

`.env.example`에 새 키가 생겼으면 서버의 `.env`에도 넣는다(`ssh deploy@<IP>` → `vi /srv/barochannel/.env`).

## 10. 운영 명령

```bash
deploy/server.sh status              # 배포된 커밋, 컨테이너 상태, 디스크, 접속 확인
deploy/server.sh logs app 200        # 앱 로그 (caddy, postgres, minio)
```

서버에서 직접 할 때:

```bash
ssh deploy@<서버 IP>
cd /srv/barochannel
docker compose --profile app ps
docker compose --profile app logs -f app
```

`docker compose down -v`는 DB와 첨부파일 볼륨을 지운다. 쓰지 않는다.

## 11. 문제 해결

| 증상 | 확인할 것 |
|---|---|
| `SSH로 … 접속하지 못했습니다` | `.deploy`의 주소·계정, iwinv 방화벽 22, `harden` 뒤라면 `deploy` 계정과 키로만 들어간다 |
| IP 접속에서 인증서 오류 | `deploy/server.sh logs caddy`. IP 인증서는 Caddy 2.11 이상, `profile shortlived`가 필요하다. 80·443이 인터넷에서 열려 있어야 발급·갱신된다 |
| `up` 뒤 HTTPS 접속이 안 됨 | `deploy/server.sh check`로 DNS가 서버 IP를 가리키는지, iwinv 방화벽과 ufw에서 80·443이 열렸는지. `deploy/server.sh logs caddy`에 인증서 오류가 있는지 |
| Caddy 로그에 `rateLimited` | Let's Encrypt 발급 한도(같은 도메인 주 5회). `caddy-data` 볼륨을 지우고 다시 띄우기를 반복하지 않는다 |
| 빌드 중 메모리 부족(`Killed`) | `check`에서 스왑이 있는지 본다. 없으면 `setup`을 다시(root) 하거나 서버 사양을 2GB로 올린다 |
| `app`이 healthy가 안 됨 | `deploy/server.sh logs app`. `password authentication failed`면 DB를 만든 뒤 `.env`의 DB 비밀번호가 바뀐 경우다 |
| 로그인 후 바로 로그인 화면으로 돌아옴 | `https://`로 들어왔는지 본다. 운영 모드 세션 쿠키는 HTTPS에서만 오간다 |
| 디스크 부족 | `deploy/server.sh status`의 남은 공간. `docker image prune -a`(쓰지 않는 이미지), 오래된 `backups/` 정리 |
