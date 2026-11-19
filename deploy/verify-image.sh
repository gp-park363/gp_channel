#!/usr/bin/env bash
# 이미지 안의 파일이 원본과 같은지 확인한다.
# QNAP Container Station은 이미지를 풀어 저장할 때 파일을 오류 없이 망가뜨리는 일이 있다
# (deploy_qnap.md "NAS 안에서 단계별로 진행"). 이 PC에서 만든 목록과 NAS의 이미지를 비교한다.
#
#   이 PC: deploy/verify-image.sh make  <이미지> <목록 파일>
#   NAS:   deploy/verify-image.sh check <이미지> <목록 파일>
#
# 이미지를 컨테이너로 만들어(실행하지 않음) 파일 시스템을 꺼낸 뒤 모든 일반 파일의 SHA-256을 쓴다.
# 꺼낸 파일은 목록 파일 옆의 임시 폴더에 두었다가 지운다(NAS의 /tmp는 메모리라 작다).
set -euo pipefail

mode="${1:-}" image="${2:-}" list="${3:-}"
if [[ ! "$mode" =~ ^(make|check)$ || -z "$image" || -z "$list" ]]; then
  echo "사용법: $0 make|check <이미지> <목록 파일>" >&2
  exit 2
fi
mkdir -p "$(dirname "$list")"
list="$(cd "$(dirname "$list")" && pwd)/$(basename "$list")"
work="$(dirname "$list")/.verify-$$"
cid=""
cleanup() {
  [[ -n "$cid" ]] && docker rm -f "$cid" >/dev/null 2>&1
  [[ -d "$work" ]] && rm -rf -- "$work"
  return 0
}
trap cleanup EXIT

[[ "$mode" == check && ! -f "$list" ]] && { echo "목록 파일이 없습니다: $list" >&2; exit 2; }
# 이미지가 없으면 받지 않고 멈춘다(검사가 몰래 받아 버리면 무엇을 검사했는지 알 수 없다).
docker image inspect "$image" >/dev/null 2>&1 || { echo "이미지가 없습니다: $image (먼저 받거나 불러오세요)" >&2; exit 2; }
cid="$(docker create --pull never "$image")"
mkdir -p "$work"
# /dev의 장치 파일은 일반 사용자가 만들 수 없어 건너뛴다.
docker export "$cid" | tar -x -C "$work" --exclude='dev/*' --exclude='./dev/*' 2>/dev/null || true
cd "$work"
# 일반 사용자로 꺼내면 권한이 0인 파일(/etc/shadow 등)을 읽지 못하므로 읽을 수 있게 한다.
chmod -R u+rwX . 2>/dev/null || true

if [[ "$mode" == make ]]; then
  # 컨테이너마다 docker가 새로 넣는 파일은 뺀다.
  find . -type f ! -path './etc/hostname' ! -path './etc/hosts' ! -path './etc/resolv.conf' \
    ! -path './.dockerenv' -print0 | sort -z | xargs -0 sha256sum >"$list"
  echo "$image: 파일 $(wc -l <"$list")개의 목록을 만들었습니다 → $list"
else
  bad="$(sha256sum -c "$list" 2>&1 | grep -v ': OK$' || true)"
  total="$(wc -l <"$list")"
  if [[ -z "$bad" ]]; then
    echo "$image: 정상 (파일 $total개 모두 원본과 같음)"
  else
    printf '%s\n' "$bad" | head -20
    echo "$image: 손상 $(printf '%s\n' "$bad" | grep -c ': FAILED')개 / 전체 $total개. 이미지를 지우고 다시 받으세요." >&2
    exit 1
  fi
fi
