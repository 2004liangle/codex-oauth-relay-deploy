#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

INSTALLER_URL="https://github.com/2004liangle/codex-oauth-relay-deploy/releases/download/v1.3.0/install-codex-relay.sh"
INSTALLER_SHA256="b0b26e609a2c2268d4bdf67aab2442477955901ba58b672beb3a81409b94d641"
INSTALLER_FILE="$(mktemp /tmp/install-codex-relay.XXXXXX.sh)"

cleanup() {
  rm -f -- "$INSTALLER_FILE"
}
trap cleanup EXIT

printf 'Downloading the Codex relay installer...\n'
curl --proto '=https' --tlsv1.2 -fsSL \
  --connect-timeout 15 --max-time 600 --retry 3 --retry-all-errors \
  "$INSTALLER_URL" -o "$INSTALLER_FILE"

printf '%s  %s\n' "$INSTALLER_SHA256" "$INSTALLER_FILE" | sha256sum -c - >/dev/null
printf 'Checksum verified. Starting installation...\n'

bash "$INSTALLER_FILE" "$@"
