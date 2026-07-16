#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

INSTALLER_URL="https://github.com/2004liangle/codex-oauth-relay-deploy/releases/download/v1.0.2/install-codex-relay.sh"
INSTALLER_SHA256="869341777f074a58cfd8cd295ccb8c9bf7ecc30cd9409f3f56491fce91b46fb9"
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
