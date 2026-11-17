# Cloudflare DNS 인증(DNS-01) 플러그인을 넣은 Caddy.
# 서버가 외부에 열려 있지 않아도 Let's Encrypt 공인 인증서를 받을 수 있다.
FROM caddy:2.10-builder-alpine AS build
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:2.10-alpine
COPY --from=build /usr/bin/caddy /usr/bin/caddy
