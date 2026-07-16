#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

INSTALLER_URL="https://github.com/2004liangle/codex-oauth-relay-deploy/releases/download/v1.0.1/install-codex-relay.sh"
INSTALLER_SHA256="a1afb9e61311e2cdf5557ea55a86952ca9059a3299b6b840e39a7a127318e43b"
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
