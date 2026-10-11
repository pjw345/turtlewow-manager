#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p "$HOME/.config/turtlewow-manager/tls"
chmod 700 "$HOME/.config/turtlewow-manager/tls"
if [[ ! -f "$HOME/.config/turtlewow-manager/tls/server.crt" ]]; then
  umask 077
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 825 \
    -keyout "$HOME/.config/turtlewow-manager/tls/server.key" \
    -out "$HOME/.config/turtlewow-manager/tls/server.crt" \
    -subj '/CN=turtlewow-server' \
    -addext 'subjectAltName=DNS:turtlewow-server,DNS:turtlewow-server.localdomain' \
    -addext 'basicConstraints=critical,CA:FALSE' \
    -addext 'keyUsage=critical,digitalSignature,keyEncipherment' \
    -addext 'extendedKeyUsage=serverAuth'
fi
docker compose up -d
