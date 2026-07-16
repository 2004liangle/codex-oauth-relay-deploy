#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

INSTALLER_URL="https://github.com/2004liangle/codex-oauth-relay-deploy/releases/download/v1.2.0/install-codex-relay.sh"
INSTALLER_SHA256="f9950f27497c733b9507ffe429c4ad6f0bedd91ce6cc75dfebb3f36cbfe80867"
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
