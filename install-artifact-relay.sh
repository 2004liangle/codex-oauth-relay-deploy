#!/usr/bin/env bash
set -Eeuo pipefail

umask 077
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$SCRIPT_DIR/artifact-relay/artifact_relay.py"
INSTALL_ROOT="/opt/codex-artifact-relay"
STATE_DIR="/var/lib/codex-artifact-relay"
CONFIG_DIR="/etc/codex-artifact-relay"
ENV_FILE="$CONFIG_DIR/env"
UNIT_FILE="/etc/systemd/system/codex-artifact-relay.service"
LOCAL_CLI="/usr/local/sbin/codex-artifact-relay-local"
NGINX_SITE="/etc/nginx/sites-available/codex-relay"
NGINX_SNIPPET="/etc/nginx/snippets/codex-artifact-relay-locations.conf"
INTERNAL_PORT="${ARTIFACT_RELAY_PORT:-18318}"
WORKER_COUNT="${ARTIFACT_RELAY_WORKERS:-2}"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

wait_for_http() {
  local expected="$1"
  local url="$2"
  shift 2
  local code
  for _ in $(seq 1 30); do
    code="$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' "$@" "$url" || true)"
    if [[ "$code" == "$expected" ]]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

[[ ${EUID} -eq 0 ]] || die "Run this installer with sudo or as root."
[[ -f "$SOURCE" ]] || die "Run this script from the codex-oauth-relay-deploy checkout."
[[ -f "$NGINX_SITE" ]] || die "The managed Codex relay Nginx site is missing."
[[ -r /etc/cliproxyapi/config.yaml ]] || die "The managed Codex relay configuration is missing."
grep -Fq "map \$http_authorization \$codex_relay_api_authorized" "$NGINX_SITE" || \
  die "The Nginx site is not the managed Codex relay."
grep -Fq 'host: "127.0.0.1"' /etc/cliproxyapi/config.yaml || \
  die "CLIProxyAPI is not bound to the managed loopback address."
grep -Eq '^port:[[:space:]]*18317$' /etc/cliproxyapi/config.yaml || \
  die "CLIProxyAPI is not using the managed internal port."

FEISHU_INPUT_FOLDER_TOKEN="${FEISHU_INPUT_FOLDER_TOKEN:-}"
FEISHU_OUTPUT_FOLDER_TOKEN="${FEISHU_OUTPUT_FOLDER_TOKEN:-}"
[[ "$FEISHU_INPUT_FOLDER_TOKEN" =~ ^[A-Za-z0-9_-]{6,200}$ ]] || \
  die "Set FEISHU_INPUT_FOLDER_TOKEN to the dedicated input folder token."
[[ "$FEISHU_OUTPUT_FOLDER_TOKEN" =~ ^[A-Za-z0-9_-]{6,200}$ ]] || \
  die "Set FEISHU_OUTPUT_FOLDER_TOKEN to the dedicated output folder token."
[[ "$FEISHU_INPUT_FOLDER_TOKEN" != "$FEISHU_OUTPUT_FOLDER_TOKEN" ]] || \
  die "Input and output folders must be different."

RUN_USER="${FEISHU_LARK_USER:-${SUDO_USER:-ubuntu}}"
getent passwd "$RUN_USER" >/dev/null || die "The lark-cli user does not exist: $RUN_USER"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
RUN_GROUP="$(id -gn "$RUN_USER")"
LARK_CLI="${FEISHU_LARK_CLI:-$RUN_HOME/.local/bin/lark-cli}"
LARK_IDENTITY="${FEISHU_LARK_IDENTITY:-bot}"
[[ "$LARK_IDENTITY" == "bot" || "$LARK_IDENTITY" == "user" ]] || \
  die "FEISHU_LARK_IDENTITY must be bot or user."
[[ "$LARK_CLI" =~ ^[A-Za-z0-9_./-]+$ && -x "$LARK_CLI" ]] || \
  die "lark-cli is not executable at $LARK_CLI"
[[ "$RUN_HOME" =~ ^[A-Za-z0-9_./-]+$ ]] || die "The lark-cli home path contains unsupported characters."
[[ "$INTERNAL_PORT" =~ ^[0-9]+$ ]] || die "ARTIFACT_RELAY_PORT must be numeric."
(( INTERNAL_PORT >= 1 && INTERNAL_PORT <= 65535 )) || die "ARTIFACT_RELAY_PORT is out of range."
[[ "$WORKER_COUNT" =~ ^[0-9]+$ ]] || die "ARTIFACT_RELAY_WORKERS must be numeric."
(( WORKER_COUNT >= 1 && WORKER_COUNT <= 4 )) || die "ARTIFACT_RELAY_WORKERS must be between 1 and 4."

if [[ -r /root/codex-relay-credentials.txt ]]; then
  API_KEY="$(sed -n 's/^API_KEY=//p' /root/codex-relay-credentials.txt | head -n 1)"
  PUBLIC_PORT="$(sed -n 's/^PUBLIC_PORT=//p' /root/codex-relay-credentials.txt | head -n 1)"
else
  API_KEY="$(awk '
    /^api-keys:[[:space:]]*$/ { in_keys = 1; next }
    in_keys && /^[[:space:]]*-[[:space:]]*"[A-Za-z0-9._~-]+"[[:space:]]*$/ {
      value = $0
      sub(/^[[:space:]]*-[[:space:]]*"/, "", value)
      sub(/"[[:space:]]*$/, "", value)
      print value
      exit
    }
    in_keys && /^[^[:space:]]/ { exit }
  ' /etc/cliproxyapi/config.yaml)"
  PUBLIC_PORT="$(awk '
    /^[[:space:]]*listen[[:space:]]+[0-9]+[[:space:]]+default_server;/ {
      value = $2
      sub(/;.*/, "", value)
      print value
      exit
    }
  ' "$NGINX_SITE")"
fi
[[ "$API_KEY" =~ ^[A-Za-z0-9._~-]{32,200}$ ]] || die "The managed relay API key is invalid."
[[ "$PUBLIC_PORT" =~ ^[0-9]+$ ]] || die "The managed relay public port is invalid."

if ss -H -ltn | awk -v port="$INTERNAL_PORT" '
  { address = $4; sub(/^.*:/, "", address); if (address == port) found = 1 }
  END { exit found ? 0 : 1 }
' && ! systemctl is-active --quiet codex-artifact-relay.service; then
  die "TCP port $INTERNAL_PORT is already in use."
fi

STATUS_JSON="$(runuser -u "$RUN_USER" -- env \
  HOME="$RUN_HOME" \
  LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 \
  LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1 \
  "$LARK_CLI" auth status --json --verify 2>/dev/null)" || die "lark-cli authentication status failed."
printf '%s' "$STATUS_JSON" | jq -e --arg identity "$LARK_IDENTITY" \
  '.identities[$identity].available == true' >/dev/null || \
  die "The configured lark-cli identity is not ready."

install -d -o root -g root -m 0755 "$INSTALL_ROOT"
install -d -o root -g "$RUN_GROUP" -m 0750 "$CONFIG_DIR"
install -d -o "$RUN_USER" -g "$RUN_GROUP" -m 0700 "$STATE_DIR"
install -o root -g root -m 0755 "$SOURCE" "$INSTALL_ROOT/artifact_relay.py"

cat >"$ENV_FILE" <<EOF
ARTIFACT_RELAY_API_KEY=$API_KEY
ARTIFACT_RELAY_UPSTREAM_API_KEY=$API_KEY
ARTIFACT_RELAY_UPSTREAM_BASE_URL=http://127.0.0.1:18317/v1
ARTIFACT_RELAY_STATE_DIR=$STATE_DIR
ARTIFACT_RELAY_HOST=127.0.0.1
ARTIFACT_RELAY_PORT=$INTERNAL_PORT
ARTIFACT_RELAY_WORKERS=$WORKER_COUNT
ARTIFACT_RELAY_LARK_CLI=$LARK_CLI
ARTIFACT_RELAY_LARK_HOME=$RUN_HOME
ARTIFACT_RELAY_LARK_IDENTITY=$LARK_IDENTITY
ARTIFACT_RELAY_FEISHU_INPUT_FOLDER_TOKEN=$FEISHU_INPUT_FOLDER_TOKEN
ARTIFACT_RELAY_FEISHU_OUTPUT_FOLDER_TOKEN=$FEISHU_OUTPUT_FOLDER_TOKEN
EOF
chown root:"$RUN_GROUP" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

cat >"$UNIT_FILE" <<EOF
[Unit]
Description=Feishu-backed asynchronous artifact relay
After=network-online.target cliproxyapi.service
Wants=network-online.target
Requires=cliproxyapi.service

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$STATE_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 $INSTALL_ROOT/artifact_relay.py serve
Restart=on-failure
RestartSec=5s
TimeoutStopSec=30s
UMask=0077
MemoryAccounting=true
MemoryHigh=50%
MemoryMax=70%
MemorySwapMax=1G
TasksMax=128

NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths=$STATE_DIR $RUN_HOME/.lark-cli $RUN_HOME/.local/share/lark-cli

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$UNIT_FILE"

cat >"$LOCAL_CLI" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
set -a
# shellcheck disable=SC1091
source $ENV_FILE
set +a
exec runuser --preserve-environment -u $RUN_USER -- \
  /usr/bin/python3 $INSTALL_ROOT/artifact_relay.py "\$@"
EOF
chown root:root "$LOCAL_CLI"
chmod 0750 "$LOCAL_CLI"

cat >"$NGINX_SNIPPET" <<EOF
location = /v1/artifact-capabilities {
    if (\$request_method != GET) { return 405; }
    if (\$codex_relay_api_authorized = 0) { return 401; }
    proxy_pass http://127.0.0.1:$INTERNAL_PORT;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Authorization \$http_authorization;
    proxy_connect_timeout 5s;
    proxy_read_timeout 30s;
    proxy_buffering off;
}

location = /v1/artifact-jobs {
    if (\$request_method != POST) { return 405; }
    if (\$codex_relay_api_authorized = 0) { return 401; }
    client_max_body_size 1m;
    client_body_timeout 30s;
    proxy_pass http://127.0.0.1:$INTERNAL_PORT;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Authorization \$http_authorization;
    proxy_connect_timeout 5s;
    proxy_send_timeout 30s;
    proxy_read_timeout 30s;
    proxy_request_buffering on;
    proxy_buffering off;
}

location ~ "^/v1/artifact-jobs/[A-Za-z0-9][A-Za-z0-9._-]{7,127}\$" {
    if (\$request_method != GET) { return 405; }
    if (\$codex_relay_api_authorized = 0) { return 401; }
    proxy_pass http://127.0.0.1:$INTERNAL_PORT;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Authorization \$http_authorization;
    proxy_connect_timeout 5s;
    proxy_read_timeout 30s;
    proxy_buffering off;
}
EOF
chmod 0644 "$NGINX_SNIPPET"

if ! grep -Fq 'codex-artifact-relay-*.conf' "$NGINX_SITE"; then
  BACKUP="$NGINX_SITE.before-artifact-relay"
  [[ -e "$BACKUP" ]] || cp -a "$NGINX_SITE" "$BACKUP"
  TEMP_SITE="$(mktemp /etc/nginx/sites-available/codex-relay.XXXXXX)"
  awk '
    /^    location \/ \{/ && !inserted {
      print "    include /etc/nginx/snippets/codex-artifact-relay-*.conf;"
      print ""
      inserted = 1
    }
    { print }
    END { if (!inserted) exit 1 }
  ' "$NGINX_SITE" >"$TEMP_SITE" || {
    rm -f "$TEMP_SITE"
    die "Could not find the managed Nginx fallback location."
  }
  chown --reference="$NGINX_SITE" "$TEMP_SITE"
  chmod --reference="$NGINX_SITE" "$TEMP_SITE"
  mv -f "$TEMP_SITE" "$NGINX_SITE"
fi

systemctl daemon-reload
systemctl enable codex-artifact-relay.service
systemctl restart codex-artifact-relay.service

for _ in $(seq 1 30); do
  if curl --noproxy '*' -fs "http://127.0.0.1:$INTERNAL_PORT/healthz" >/dev/null; then
    break
  fi
  sleep 1
done
curl --noproxy '*' -fsS "http://127.0.0.1:$INTERNAL_PORT/healthz" >/dev/null || \
  die "The artifact sidecar did not become ready."

nginx -t
systemctl reload nginx

LOCAL_URL="http://127.0.0.1:$PUBLIC_PORT"
wait_for_http 401 "$LOCAL_URL/v1/artifact-capabilities" || \
  die "Artifact capability authentication is not enforced."
curl --noproxy '*' -fsS \
  -H "Authorization: Bearer $API_KEY" \
  "$LOCAL_URL/v1/artifact-capabilities" | \
  jq -e --arg token "$FEISHU_INPUT_FOLDER_TOKEN" \
  '.delivery == "lark_drive" and .retention == "manual" and .input_target.token == $token' \
  >/dev/null || die "Artifact capabilities are not available through Nginx."

printf 'Artifact relay installed. Public routes use the existing API base URL and relay key.\n'
