#!/usr/bin/env bash
set -Eeuo pipefail

umask 077
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

CLIPROXY_VERSION="7.2.80"
KEEPER_VERSION="1.13.2"
MANAGEMENT_VERSION="1.18.3"
DEPLOY_RELEASE_VERSION="1.3.2"
USAGE_UI_VERSION="1.13.2-plain-zh.1"
USAGE_UI_SHA256="ce7468c31f955956300d3b668909ce84a98864ed804aeff6da3db2c5a974b4aa"
USAGE_UI_ROOT="/opt/codex-relay-usage-ui"
DEFAULT_PORT="8317"
DEFAULT_TZ="Asia/Shanghai"
INTERNAL_PORTS=(18080 18081 18317 18318)
INSTALL_STATE_DIR="/etc/codex-relay-installer"
OWNER_MARKER="$INSTALL_STATE_DIR/managed"
COMPLETE_MARKER="$INSTALL_STATE_DIR/complete"
PARAMS_FILE="$INSTALL_STATE_DIR/settings"
PORTS_CHECKED_MARKER="$INSTALL_STATE_DIR/ports-checked"

log() {
  printf '\n[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

die() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

warn() {
  printf '\nWARNING: %s\n' "$*" >&2
}

wait_for_http() {
  local expected="$1"
  local url="$2"
  shift 2
  local code
  for _ in $(seq 1 30); do
    code="$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' "$@" "$url" 2>/dev/null || true)"
    if [[ "$code" == "$expected" ]]; then
      return 0
    fi
    sleep 1
  done
  printf 'Expected HTTP %s from %s, got %s\n' "$expected" "$url" "${code:-none}" >&2
  return 1
}

usage() {
  cat <<'EOF'
Codex OAuth personal relay installer

Usage:
  sudo bash install-codex-relay.sh

Optional environment variables:
  PUBLIC_HOST       Public IPv4 address or DNS name
  PUBLIC_PORT       Public Nginx port (default: 8317)
  TZ                Dashboard timezone (default: Asia/Shanghai)
  SKIP_OAUTH=1      Install services without starting device authentication
  SKIP_INFERENCE_TEST=1
                    Skip the final real model request
  INFERENCE_MODEL   Model ID used for the final request (auto-selected by default)
  REPLACE_SQUID=1   Allow replacing an existing non-relay Squid configuration
  REPAIR=1          Repair an installation previously managed by this installer

This installer supports Ubuntu 22.04+ and Debian 12+ on x86_64/aarch64
(glibc 2.34 or newer). It never overwrites an installation it does not own.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

[[ ${EUID} -eq 0 ]] || die "Run this installer with sudo or as root."
[[ -r /etc/os-release ]] || die "Cannot identify this Linux distribution."
[[ "$(ps -p 1 -o comm= 2>/dev/null | tr -d '[:space:]')" == "systemd" ]] || \
  die "This installer requires systemd as PID 1 (not a minimal container or non-systemd WSL)."

command -v flock >/dev/null 2>&1 || die "The flock command is required (package: util-linux)."
exec 9>/run/lock/codex-relay-installer.lock
flock -n 9 || die "Another Codex relay installer process is already running."

# shellcheck disable=SC1091
source /etc/os-release
case "${ID:-}" in
  ubuntu|debian) ;;
  *) die "Only Ubuntu and Debian are supported; detected ${ID:-unknown}." ;;
esac

case "${ID:-}" in
  ubuntu)
    dpkg --compare-versions "${VERSION_ID:-0}" ge 22.04 || \
      die "Ubuntu 22.04 or newer is required; detected ${VERSION_ID:-unknown}."
    ;;
  debian)
    dpkg --compare-versions "${VERSION_ID:-0}" ge 12 || \
      die "Debian 12 or newer is required; detected ${VERSION_ID:-unknown}."
    ;;
esac

GLIBC_VERSION="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')"
[[ -n "$GLIBC_VERSION" ]] || die "GNU libc could not be detected."
dpkg --compare-versions "$GLIBC_VERSION" ge 2.34 || \
  die "glibc 2.34 or newer is required by CPA Usage Keeper; detected $GLIBC_VERSION."

case "$(uname -m)" in
  x86_64)
    CLIPROXY_ARCH="amd64"
    CLIPROXY_SHA256="36616fdd8240719902d0c767a1f7445ea248950f29d8785996b93046472840b6"
    KEEPER_ARCH="amd64"
    KEEPER_SHA256="533c738a736eea6e947cc1495531134d2fe9bf3b55642246739b3d608dfecaa1"
    ;;
  aarch64|arm64)
    CLIPROXY_ARCH="aarch64"
    CLIPROXY_SHA256="4ed25c7f512c54e037247ec385f5e50b48310ce60d6bd3f1427287752c1baafe"
    KEEPER_ARCH="arm64"
    KEEPER_SHA256="b58cb3e1cd51c91914a0d921055b28936ba5f81d2dd3ebde2f2072cd36fd0acd"
    ;;
  *) die "Unsupported CPU architecture: $(uname -m)." ;;
esac

REPAIR_MODE=0
PREVIOUSLY_COMPLETE=0
if [[ -f "$OWNER_MARKER" ]]; then
  if [[ -f "$COMPLETE_MARKER" ]]; then
    PREVIOUSLY_COMPLETE=1
    if [[ "${REPAIR:-0}" != "1" ]]; then
      die "This managed relay is already installed. Set REPAIR=1 to repair it explicitly."
    fi
  fi
  REPAIR_MODE=1
elif [[ -e /etc/cliproxyapi/config.yaml ]]; then
  die "Existing installation detected at /etc/cliproxyapi/config.yaml; it is not owned by this installer."
fi

SAVED_PUBLIC_HOST=""
SAVED_PUBLIC_PORT=""
SAVED_TIMEZONE=""
if [[ "$REPAIR_MODE" == "1" ]]; then
  SETTINGS_SOURCE=""
  if [[ -r "$PARAMS_FILE" ]]; then
    SETTINGS_SOURCE="$PARAMS_FILE"
  elif [[ -r /root/codex-relay-credentials.txt ]]; then
    SETTINGS_SOURCE="/root/codex-relay-credentials.txt"
  fi
  if [[ -n "$SETTINGS_SOURCE" ]]; then
    SAVED_PUBLIC_HOST="$(sed -n 's/^PUBLIC_HOST=//p' "$SETTINGS_SOURCE" | head -n 1)"
    SAVED_PUBLIC_PORT="$(sed -n 's/^PUBLIC_PORT=//p' "$SETTINGS_SOURCE" | head -n 1)"
    SAVED_TIMEZONE="$(sed -n 's/^TIMEZONE=//p' "$SETTINGS_SOURCE" | head -n 1)"
    if [[ -z "$SAVED_PUBLIC_HOST" || -z "$SAVED_PUBLIC_PORT" ]]; then
      SAVED_API_BASE_URL="$(sed -n 's/^API_BASE_URL=//p' "$SETTINGS_SOURCE" | head -n 1)"
      if [[ "$SAVED_API_BASE_URL" =~ ^http://([^:/]+):([0-9]+)/v1$ ]]; then
        SAVED_PUBLIC_HOST="${SAVED_PUBLIC_HOST:-${BASH_REMATCH[1]}}"
        SAVED_PUBLIC_PORT="${SAVED_PUBLIC_PORT:-${BASH_REMATCH[2]}}"
      fi
    fi
  fi
  PUBLIC_HOST="${PUBLIC_HOST:-$SAVED_PUBLIC_HOST}"
  PUBLIC_PORT="${PUBLIC_PORT:-$SAVED_PUBLIC_PORT}"
  TZ="${TZ:-$SAVED_TIMEZONE}"
fi

if [[ "$REPAIR_MODE" == "0" ]]; then
  for target in \
    /etc/systemd/system/cliproxyapi.service \
    /etc/systemd/system/cpa-usage-keeper.service \
    /etc/cpa-usage-keeper/env \
    /etc/nginx/sites-available/codex-relay \
    /etc/nginx/sites-enabled/codex-relay \
    /opt/cliproxyapi \
    /opt/cpa-usage-keeper \
    "$USAGE_UI_ROOT" \
    /var/lib/cliproxyapi \
    /var/lib/cpa-usage-keeper \
    /root/codex-relay-credentials.txt; do
    [[ ! -e "$target" ]] || die "Existing unmanaged file detected: $target"
  done
fi

if [[ "$REPAIR_MODE" == "0" ]] && \
   dpkg-query -W -f='${Status}' squid 2>/dev/null | grep -q 'install ok installed'; then
  if [[ -f /etc/squid/squid.conf ]] && \
     ! grep -q '^visible_hostname codex-network-relay$' /etc/squid/squid.conf && \
     [[ "${REPLACE_SQUID:-0}" != "1" ]]; then
    die "An unrelated Squid installation already exists. Set REPLACE_SQUID=1 only after reviewing and backing it up."
  fi
fi

PUBLIC_PORT="${PUBLIC_PORT:-$DEFAULT_PORT}"
[[ "$PUBLIC_PORT" =~ ^[0-9]+$ ]] || die "PUBLIC_PORT must be numeric."
(( PUBLIC_PORT >= 1 && PUBLIC_PORT <= 65535 )) || die "PUBLIC_PORT is out of range."
[[ "$PUBLIC_PORT" != "80" && "$PUBLIC_PORT" != "443" ]] || \
  die "Ports 80 and 443 are reserved for a separate HTTP/HTTPS front end; choose a custom relay port."
for internal_port in "${INTERNAL_PORTS[@]}"; do
  [[ "$PUBLIC_PORT" != "$internal_port" ]] || \
    die "PUBLIC_PORT cannot equal the internal service port $internal_port."
done

if [[ -z "${PUBLIC_HOST:-}" ]]; then
  detected_host="$(curl -4fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
  if [[ -z "$detected_host" ]]; then
    detected_host="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi

  if exec 8<>/dev/tty 2>/dev/null; then
    printf 'Public IPv4 address or DNS name [%s]: ' "$detected_host" >&8
    if ! IFS= read -r -u 8 PUBLIC_HOST; then
      PUBLIC_HOST="$detected_host"
    fi
    exec 8>&-
    PUBLIC_HOST="${PUBLIC_HOST:-$detected_host}"
  else
    PUBLIC_HOST="$detected_host"
  fi
fi

[[ -n "${PUBLIC_HOST:-}" ]] || die "Set PUBLIC_HOST to the server public IPv4 address or DNS name."
[[ "$PUBLIC_HOST" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || \
  die "PUBLIC_HOST must be an IPv4 address or an ASCII DNS name."
[[ "$PUBLIC_HOST" != *..* ]] || die "PUBLIC_HOST cannot contain consecutive dots."
[[ "$PUBLIC_HOST" != *:* ]] || die "Use a DNS name or IPv4 address; raw IPv6 literals are not supported."

PUBLIC_URL="http://${PUBLIC_HOST}:${PUBLIC_PORT}"
TIMEZONE="${TZ:-$DEFAULT_TZ}"

WORK_DIR="$(mktemp -d /tmp/codex-relay-install.XXXXXX)"
cleanup() {
  local status=$?
  trap - EXIT
  rm -rf -- "$WORK_DIR"
  if (( status != 0 )) && [[ -f "$OWNER_MARKER" ]]; then
    printf '\nThe installation is incomplete but recoverable. Re-run the same command to resume.\n' >&2
  fi
  exit "$status"
}
trap cleanup EXIT

install -d -o root -g root -m 0700 "$INSTALL_STATE_DIR"
printf 'codex-relay-installer-v1\n' >"$OWNER_MARKER"
if [[ "$PREVIOUSLY_COMPLETE" == "1" ]]; then
  touch "$PORTS_CHECKED_MARKER"
fi
rm -f "$COMPLETE_MARKER"
if [[ "$REPAIR_MODE" == "0" ]]; then
  cat >"$PARAMS_FILE" <<EOF
PUBLIC_HOST=$PUBLIC_HOST
PUBLIC_PORT=$PUBLIC_PORT
TIMEZONE=$TIMEZONE
EOF
  chmod 0600 "$PARAMS_FILE"
fi

log "Installing operating-system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl jq openssl tar nginx squid iproute2 sqlite3

[[ "$TIMEZONE" =~ ^[A-Za-z0-9_+./-]+$ && -e "/usr/share/zoneinfo/$TIMEZONE" ]] || \
  die "TZ does not name an installed timezone: $TIMEZONE"

if [[ "$REPAIR_MODE" == "1" && -r /root/codex-relay-credentials.txt ]]; then
  API_KEY="$(sed -n 's/^API_KEY=//p' /root/codex-relay-credentials.txt | head -n 1)"
  MANAGEMENT_KEY="$(sed -n 's/^MANAGEMENT_KEY=//p' /root/codex-relay-credentials.txt | head -n 1)"
  DASHBOARD_PASSWORD="$(sed -n 's/^DASHBOARD_PASSWORD=//p' /root/codex-relay-credentials.txt | head -n 1)"
  if [[ -z "$API_KEY" || -z "$MANAGEMENT_KEY" || -z "$DASHBOARD_PASSWORD" ]]; then
    [[ ! -e /etc/cliproxyapi/config.yaml ]] || \
      die "The managed credential file is incomplete; restore it before repair."
    API_KEY="sk-codex-relay-$(openssl rand -hex 32)"
    MANAGEMENT_KEY="mgmt-$(openssl rand -hex 24)"
    DASHBOARD_PASSWORD="usage-$(openssl rand -hex 16)"
  fi
else
  API_KEY="sk-codex-relay-$(openssl rand -hex 32)"
  MANAGEMENT_KEY="mgmt-$(openssl rand -hex 24)"
  DASHBOARD_PASSWORD="usage-$(openssl rand -hex 16)"
fi

[[ "$API_KEY" =~ ^[A-Za-z0-9._~-]{32,200}$ ]] || \
  die "The saved API key contains characters that cannot be represented safely in Nginx."

FULL_PORT_CHECK=0
PUBLIC_PORT_CHANGED=0
if [[ "$REPAIR_MODE" == "0" ]]; then
  FULL_PORT_CHECK=1
elif [[ -n "$SAVED_PUBLIC_PORT" && "$PUBLIC_PORT" != "$SAVED_PUBLIC_PORT" ]]; then
  PUBLIC_PORT_CHANGED=1
elif [[ "$PREVIOUSLY_COMPLETE" != "1" && ! -f "$PORTS_CHECKED_MARKER" ]]; then
  FULL_PORT_CHECK=1
fi

if [[ "$FULL_PORT_CHECK" == "1" || "$PUBLIC_PORT_CHANGED" == "1" ]]; then
  PORTS_TO_CHECK=("$PUBLIC_PORT")
  if [[ "$FULL_PORT_CHECK" == "1" ]]; then
    PORTS_TO_CHECK+=("${INTERNAL_PORTS[@]}")
  fi
  for port in "${PORTS_TO_CHECK[@]}"; do
    if ss -H -ltn | awk -v port="$port" '
      { address = $4; sub(/^.*:/, "", address); if (address == port) found = 1 }
      END { exit found ? 0 : 1 }
    '; then
      die "TCP port $port is already in use. Choose another public port or free the internal port."
    fi
  done
  touch "$PORTS_CHECKED_MARKER"
elif [[ "$PREVIOUSLY_COMPLETE" == "1" ]]; then
  touch "$PORTS_CHECKED_MARKER"
fi

cat >"$PARAMS_FILE" <<EOF
PUBLIC_HOST=$PUBLIC_HOST
PUBLIC_PORT=$PUBLIC_PORT
TIMEZONE=$TIMEZONE
EOF
chmod 0600 "$PARAMS_FILE"

cat >/root/codex-relay-credentials.txt <<EOF
PUBLIC_HOST=$PUBLIC_HOST
PUBLIC_PORT=$PUBLIC_PORT
TIMEZONE=$TIMEZONE
API_BASE_URL=$PUBLIC_URL/v1
API_KEY=$API_KEY
USAGE_DASHBOARD=$PUBLIC_URL/usage/
DASHBOARD_PASSWORD=$DASHBOARD_PASSWORD
MANAGEMENT_PANEL=$PUBLIC_URL/management.html
MANAGEMENT_KEY=$MANAGEMENT_KEY
EOF
chmod 0600 /root/codex-relay-credentials.txt

log "Creating restricted service accounts and directories"
if ! id cliproxyapi >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/cliproxyapi \
    --shell /usr/sbin/nologin cliproxyapi
else
  IFS=: read -r _ _ cliproxy_uid _ _ cliproxy_home cliproxy_shell < <(getent passwd cliproxyapi)
  [[ "$cliproxy_uid" -lt 1000 && "$cliproxy_home" == "/var/lib/cliproxyapi" && \
     "$cliproxy_shell" == "/usr/sbin/nologin" && "$(id -gn cliproxyapi)" == "cliproxyapi" ]] || \
    die "Existing cliproxyapi account does not match the required restricted service account."
fi
if ! id cpausage >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/cpa-usage-keeper \
    --shell /usr/sbin/nologin cpausage
else
  IFS=: read -r _ _ cpausage_uid _ _ cpausage_home cpausage_shell < <(getent passwd cpausage)
  [[ "$cpausage_uid" -lt 1000 && "$cpausage_home" == "/var/lib/cpa-usage-keeper" && \
     "$cpausage_shell" == "/usr/sbin/nologin" && "$(id -gn cpausage)" == "cpausage" ]] || \
    die "Existing cpausage account does not match the required restricted service account."
fi

install -d -o root -g root -m 0755 \
  /opt/cliproxyapi \
  /opt/cpa-usage-keeper \
  "$USAGE_UI_ROOT"
install -d -o root -g cliproxyapi -m 0750 /etc/cliproxyapi
install -d -o root -g cpausage -m 0750 /etc/cpa-usage-keeper
install -d -o cliproxyapi -g cliproxyapi -m 0700 \
  /var/lib/cliproxyapi \
  /var/lib/cliproxyapi/auth \
  /var/lib/cliproxyapi/logs \
  /var/lib/cliproxyapi/plugins \
  /var/lib/cliproxyapi/static
install -d -o cpausage -g cpausage -m 0700 /var/lib/cpa-usage-keeper

CLIPROXY_ASSET="CLIProxyAPI_${CLIPROXY_VERSION}_linux_${CLIPROXY_ARCH}_no-plugin.tar.gz"
KEEPER_ASSET="cpa-usage-keeper_v${KEEPER_VERSION}_linux_${KEEPER_ARCH}.tar.gz"
USAGE_UI_ASSET="cpa-usage-ui_${USAGE_UI_VERSION}.tar.gz"
USAGE_UI_PACKAGE="${USAGE_UI_ASSET%.tar.gz}"

log "Downloading and verifying CLIProxyAPI v${CLIPROXY_VERSION}"
curl --proto '=https' --tlsv1.2 -fL \
  --connect-timeout 15 --max-time 600 --retry 3 --retry-delay 2 --retry-all-errors \
  "https://github.com/router-for-me/CLIProxyAPI/releases/download/v${CLIPROXY_VERSION}/${CLIPROXY_ASSET}" \
  -o "$WORK_DIR/$CLIPROXY_ASSET"
printf '%s  %s\n' "$CLIPROXY_SHA256" "$WORK_DIR/$CLIPROXY_ASSET" | sha256sum -c -
mkdir "$WORK_DIR/cliproxy"
tar -xzf "$WORK_DIR/$CLIPROXY_ASSET" -C "$WORK_DIR/cliproxy"
install -o root -g root -m 0755 \
  "$WORK_DIR/cliproxy/cli-proxy-api" /opt/cliproxyapi/cli-proxy-api

log "Downloading and verifying CPA Usage Keeper v${KEEPER_VERSION}"
curl --proto '=https' --tlsv1.2 -fL \
  --connect-timeout 15 --max-time 600 --retry 3 --retry-delay 2 --retry-all-errors \
  "https://github.com/Willxup/cpa-usage-keeper/releases/download/v${KEEPER_VERSION}/${KEEPER_ASSET}" \
  -o "$WORK_DIR/$KEEPER_ASSET"
printf '%s  %s\n' "$KEEPER_SHA256" "$WORK_DIR/$KEEPER_ASSET" | sha256sum -c -
mkdir "$WORK_DIR/keeper"
tar -xzf "$WORK_DIR/$KEEPER_ASSET" -C "$WORK_DIR/keeper"
KEEPER_BIN="$(find "$WORK_DIR/keeper" -type f -name cpa-usage-keeper -print -quit)"
[[ -n "$KEEPER_BIN" ]] || die "CPA Usage Keeper binary was not found in the release archive."
install -o root -g root -m 0755 "$KEEPER_BIN" /opt/cpa-usage-keeper/cpa-usage-keeper

log "Downloading and verifying the plain-Chinese usage dashboard ${USAGE_UI_VERSION}"
curl --proto '=https' --tlsv1.2 -fL \
  --connect-timeout 15 --max-time 600 --retry 3 --retry-delay 2 --retry-all-errors \
  "https://github.com/2004liangle/codex-oauth-relay-deploy/releases/download/v${DEPLOY_RELEASE_VERSION}/${USAGE_UI_ASSET}" \
  -o "$WORK_DIR/$USAGE_UI_ASSET"
printf '%s  %s\n' "$USAGE_UI_SHA256" "$WORK_DIR/$USAGE_UI_ASSET" | sha256sum -c -
if tar -tzf "$WORK_DIR/$USAGE_UI_ASSET" | awk '
  /^\// || /(^|\/)\.\.($|\/)/ || /\\/ { bad = 1 }
  END { exit bad ? 0 : 1 }
'; then
  die "The usage dashboard archive contains an unsafe path."
fi
mkdir "$WORK_DIR/usage-ui"
tar --extract --gzip --file "$WORK_DIR/$USAGE_UI_ASSET" \
  --directory "$WORK_DIR/usage-ui" --no-same-owner --no-same-permissions
USAGE_UI_SOURCE="$WORK_DIR/usage-ui/$USAGE_UI_PACKAGE"
[[ -f "$USAGE_UI_SOURCE/usage/index.html" ]] || \
  die "The usage dashboard archive does not contain usage/index.html."
[[ -d "$USAGE_UI_SOURCE/usage/assets" ]] || \
  die "The usage dashboard archive does not contain usage/assets."
if find "$USAGE_UI_SOURCE" \( -type l -o \( ! -type f ! -type d \) \) -print -quit | grep -q .; then
  die "The usage dashboard archive contains an unsupported file type."
fi
[[ -n "$(find "$USAGE_UI_SOURCE/usage/assets" -type f -print -quit)" ]] || \
  die "The usage dashboard archive contains no static assets."
USAGE_UI_TARGET="$USAGE_UI_ROOT/$USAGE_UI_VERSION"
install -d -o root -g root -m 0755 "$USAGE_UI_TARGET"
cp -a "$USAGE_UI_SOURCE/." "$USAGE_UI_TARGET/"
chown -R root:root "$USAGE_UI_TARGET"
find "$USAGE_UI_TARGET" -type d -exec chmod 0755 {} +
find "$USAGE_UI_TARGET" -type f -exec chmod 0644 {} +
ln -sfn "$USAGE_UI_TARGET" "$USAGE_UI_ROOT/current"

log "Downloading and verifying the CLIProxy management panel v${MANAGEMENT_VERSION}"
curl --proto '=https' --tlsv1.2 -fL \
  --connect-timeout 15 --max-time 600 --retry 3 --retry-delay 2 --retry-all-errors \
  "https://github.com/router-for-me/Cli-Proxy-API-Management-Center/releases/download/v${MANAGEMENT_VERSION}/management.html" \
  -o "$WORK_DIR/management.html"
printf '%s  %s\n' \
  "941a49a619a719a59e4c7917c6888a53eb3f41a4fa2fbb5c1cc94f2d1fc9cd4b" \
  "$WORK_DIR/management.html" | sha256sum -c -
install -o cliproxyapi -g cliproxyapi -m 0600 \
  "$WORK_DIR/management.html" /var/lib/cliproxyapi/static/management.html

log "Configuring loopback-only Squid egress"
if [[ -f /etc/squid/squid.conf && ! -f /etc/squid/squid.conf.before-codex-relay ]]; then
  cp -a /etc/squid/squid.conf /etc/squid/squid.conf.before-codex-relay
fi
cat >/etc/squid/squid.conf <<'EOF'
http_port 127.0.0.1:18080
visible_hostname codex-network-relay

acl local_tunnel src 127.0.0.1/32 ::1
acl CONNECT method CONNECT
acl TLS_port port 443
acl codex_hosts dstdomain .openai.com .chatgpt.com .oaistatic.com .oaiusercontent.com .openaiusercontent.com openaipublic.blob.core.windows.net

http_access deny !local_tunnel
http_access deny !CONNECT
http_access deny !TLS_port
http_access allow codex_hosts
http_access deny all

cache deny all
access_log none
cache_store_log none
shutdown_lifetime 1 seconds
EOF
squid -k parse
systemctl enable --now squid
systemctl restart squid

log "Writing CLIProxyAPI configuration"
cat >/etc/cliproxyapi/config.yaml <<EOF
host: "127.0.0.1"
port: 18317

tls:
  enable: false
  cert: ""
  key: ""

remote-management:
  # The process still binds only to 127.0.0.1. This lets Nginx pass the real
  # client IP so failed-login bans do not collapse every user into localhost.
  allow-remote: true
  secret-key: "$MANAGEMENT_KEY"
  disable-control-panel: false
  disable-auto-update-panel: true

auth-dir: "/var/lib/cliproxyapi/auth"

api-keys:
  - "$API_KEY"

debug: false

pprof:
  enable: false
  addr: "127.0.0.1:8316"

plugins:
  enabled: false
  dir: "/var/lib/cliproxyapi/plugins"

commercial-mode: false
logging-to-file: true
logs-max-total-size-mb: 512
error-logs-max-files: 10
usage-statistics-enabled: true
redis-usage-queue-retention-seconds: 3600
request-log: true

proxy-url: "http://127.0.0.1:18080"
passthrough-headers: false
request-retry: 1
max-retry-credentials: 1
max-retry-interval: 10
save-cooldown-status: false
ws-auth: true
EOF
chown root:cliproxyapi /etc/cliproxyapi/config.yaml
chmod 0640 /etc/cliproxyapi/config.yaml

cat >/etc/systemd/system/cliproxyapi.service <<'EOF'
[Unit]
Description=Personal Codex OAuth Responses relay
After=network-online.target squid.service
Wants=network-online.target
Requires=squid.service

[Service]
Type=simple
User=cliproxyapi
Group=cliproxyapi
WorkingDirectory=/var/lib/cliproxyapi
Environment=WRITABLE_PATH=/var/lib/cliproxyapi
ExecStart=/opt/cliproxyapi/cli-proxy-api -config /etc/cliproxyapi/config.yaml -local-model
Restart=on-failure
RestartSec=5s
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
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
MemoryDenyWriteExecute=true
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
IPAddressDeny=any
IPAddressAllow=localhost
ReadWritePaths=/var/lib/cliproxyapi

[Install]
WantedBy=multi-user.target
EOF

has_valid_codex_auth() {
  local auth_file
  while IFS= read -r auth_file; do
    if jq -e '
      .type == "codex" and
      (.refresh_token | type == "string" and length > 0) and
      (.disabled != true)
    ' "$auth_file" >/dev/null 2>&1; then
      return 0
    fi
  done < <(find /var/lib/cliproxyapi/auth -maxdepth 1 -type f -name '*.json' -print)
  return 1
}

AUTH_READY=0
if has_valid_codex_auth; then
  AUTH_READY=1
  log "Reusing an existing valid Codex credential from this managed installation"
elif [[ "${SKIP_OAUTH:-0}" != "1" ]]; then
  log "Starting Codex device authentication"
  printf '%s\n' \
    "Open the URL printed below on any browser, enter the device code, and approve access." \
    "This creates a separate relay session; do not copy an active refresh token from another client."
  touch "$WORK_DIR/oauth-started"
  if runuser -u cliproxyapi -- env HOME=/var/lib/cliproxyapi \
      /opt/cliproxyapi/cli-proxy-api \
      -config /etc/cliproxyapi/config.yaml \
      -codex-device-login \
      -no-browser; then
    while IFS= read -r auth_file; do
      if jq -e '
        .type == "codex" and
        (.refresh_token | type == "string" and length > 0) and
        (.disabled != true)
      ' "$auth_file" >/dev/null 2>&1; then
        AUTH_READY=1
        break
      fi
    done < <(find /var/lib/cliproxyapi/auth -maxdepth 1 -type f -name '*.json' \
      -newer "$WORK_DIR/oauth-started" -print)
  fi
  if [[ "$AUTH_READY" != "1" ]]; then
    warn "Codex authentication is pending. The installer will finish the service setup."
  fi
else
  log "Skipping OAuth because SKIP_OAUTH=1"
fi

systemctl daemon-reload
systemctl enable cliproxyapi
systemctl restart cliproxyapi

log "Configuring CPA Usage Keeper"
cat >/etc/cpa-usage-keeper/env <<EOF
CPA_BASE_URL=http://127.0.0.1:18317
CPA_MANAGEMENT_KEY=$MANAGEMENT_KEY
CPA_REQUEST_LOG_ACCESS_ENABLED=true

APP_PORT=18081
APP_BASE_PATH=/usage
CPA_PUBLIC_URL=$PUBLIC_URL

AUTH_ENABLED=true
LOGIN_PASSWORD=$DASHBOARD_PASSWORD
AUTH_SESSION_TTL=24h

TZ=$TIMEZONE
REQUEST_TIMEOUT=30s
TLS_SKIP_VERIFY=false
QUOTA_REFRESH_WORKER_LIMIT=2

REDIS_QUEUE_ADDR=127.0.0.1:18317
REDIS_QUEUE_TLS=false
REDIS_QUEUE_BATCH_SIZE=1000
REDIS_QUEUE_IDLE_INTERVAL=1s

WORK_DIR=/var/lib/cpa-usage-keeper
LOG_LEVEL=info
LOG_FILE_ENABLED=true
LOG_RETENTION_DAYS=7
CLEANUP_USAGE_EVENTS_ENABLED=false
BACKUP_ENABLED=true
BACKUP_INTERVAL=24h
BACKUP_RETENTION_DAYS=7

TLS_ENABLED=false
EOF
chown root:cpausage /etc/cpa-usage-keeper/env
chmod 0640 /etc/cpa-usage-keeper/env

cat >/etc/systemd/system/cpa-usage-keeper.service <<'EOF'
[Unit]
Description=Persistent usage dashboard for the personal Codex relay
After=network-online.target cliproxyapi.service
Wants=network-online.target
Requires=cliproxyapi.service

[Service]
Type=simple
User=cpausage
Group=cpausage
Environment=GIN_MODE=release
WorkingDirectory=/var/lib/cpa-usage-keeper
ExecStart=/opt/cpa-usage-keeper/cpa-usage-keeper -env /etc/cpa-usage-keeper/env
Restart=on-failure
RestartSec=5s
TimeoutStopSec=30s
UMask=0077

NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
MemoryDenyWriteExecute=true
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
IPAddressDeny=any
IPAddressAllow=localhost
ReadWritePaths=/var/lib/cpa-usage-keeper

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable cpa-usage-keeper
systemctl restart cpa-usage-keeper

log "Configuring the public Nginx entry point"
cat >/etc/nginx/sites-available/codex-relay <<EOF
map_hash_bucket_size 256;

map \$http_authorization \$codex_relay_api_authorized {
    default 0;
    "Bearer $API_KEY" 1;
}

limit_conn_zone \$server_name zone=codex_relay_image_edits_global:1m;
limit_req_zone \$binary_remote_addr zone=codex_relay_image_edits_per_ip:10m rate=6r/m;

server {
    listen $PUBLIC_PORT default_server;
    listen [::]:$PUBLIC_PORT default_server;
    server_name _;

    server_tokens off;
    access_log off;
    error_log /var/log/nginx/codex-relay-error.log warn;
    client_max_body_size 64m;

    location = /management.html {
        if (\$request_method !~ ^(GET|HEAD)\$) { return 405; }
        proxy_pass http://127.0.0.1:18317;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
    }

    # Never expose raw configuration, downloadable OAuth credentials, queue
    # consumption, or browser-driven OAuth state changes on the public port.
    location = /v0/management/config.yaml { return 404; }
    location = /v0/management/auth-files/download { return 404; }
    location = /v0/management/usage-queue { return 404; }
    location = /v0/management/oauth-callback { return 404; }
    location = /v0/management/anthropic-auth-url { return 404; }
    location = /v0/management/codex-auth-url { return 404; }
    location = /v0/management/antigravity-auth-url { return 404; }
    location = /v0/management/kimi-auth-url { return 404; }
    location = /v0/management/xai-auth-url { return 404; }

    location ^~ /v0/management/ {
        if (\$request_method !~ ^(GET|HEAD)\$) { return 405; }
        proxy_pass http://127.0.0.1:18317;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }

    location = /usage {
        return 308 /usage/;
    }

    location ^~ /usage/api/ {
        proxy_pass http://127.0.0.1:18081;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host \$http_host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }

    location = /usage/healthz {
        proxy_pass http://127.0.0.1:18081;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }

    location = /usage/ {
        root $USAGE_UI_ROOT/current;
        index index.html;
        try_files \$uri \$uri/ =404;
        add_header Cache-Control "no-store" always;
        add_header Pragma "no-cache" always;
        add_header Expires "0" always;
        add_header Content-Security-Policy "frame-ancestors 'self'" always;
        add_header X-Content-Type-Options "nosniff" always;
    }

    location = /usage/index.html {
        root $USAGE_UI_ROOT/current;
        try_files /usage/index.html =404;
        add_header Cache-Control "no-store" always;
        add_header Pragma "no-cache" always;
        add_header Expires "0" always;
        add_header Content-Security-Policy "frame-ancestors 'self'" always;
        add_header X-Content-Type-Options "nosniff" always;
    }

    location = /usage/key-overview {
        root $USAGE_UI_ROOT/current;
        try_files /usage/index.html =404;
        add_header Cache-Control "no-store" always;
        add_header Pragma "no-cache" always;
        add_header Expires "0" always;
        add_header Content-Security-Policy "frame-ancestors 'self'" always;
        add_header X-Content-Type-Options "nosniff" always;
    }

    location ^~ /usage/assets/ {
        root $USAGE_UI_ROOT/current;
        try_files \$uri =404;
        access_log off;
        add_header Cache-Control "public, max-age=31536000, immutable" always;
        add_header X-Content-Type-Options "nosniff" always;
    }

    location ^~ /usage/ {
        return 404;
    }

    location = /v1/models {
        if (\$request_method != GET) { return 405; }
        if (\$codex_relay_api_authorized = 0) { return 401; }
        proxy_pass http://127.0.0.1:18317;
    }

    location = /v1/chat/completions {
        if (\$request_method != POST) { return 405; }
        if (\$codex_relay_api_authorized = 0) { return 401; }
        proxy_pass http://127.0.0.1:18317;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location = /v1/completions {
        if (\$request_method != POST) { return 405; }
        if (\$codex_relay_api_authorized = 0) { return 401; }
        proxy_pass http://127.0.0.1:18317;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location = /v1/images/generations {
        if (\$request_method != POST) { return 405; }
        if (\$codex_relay_api_authorized = 0) { return 401; }
        client_max_body_size 1m;
        client_body_timeout 30s;
        proxy_pass http://127.0.0.1:18317;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Authorization \$http_authorization;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 5s;
        proxy_send_timeout 60s;
        proxy_read_timeout 3600s;
        proxy_request_buffering on;
        proxy_buffering off;
        proxy_cache off;
        proxy_next_upstream off;
        proxy_ignore_client_abort off;
        gzip off;
    }

    location = /v1/images/edits {
        if (\$request_method != POST) { return 405; }
        if (\$codex_relay_api_authorized = 0) { return 401; }
        client_max_body_size 64m;
        client_body_timeout 300s;
        limit_conn codex_relay_image_edits_global 1;
        limit_conn_status 429;
        limit_req zone=codex_relay_image_edits_per_ip burst=2 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:18317;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Authorization \$http_authorization;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 5s;
        proxy_send_timeout 600s;
        proxy_read_timeout 3600s;
        proxy_request_buffering on;
        proxy_buffering off;
        proxy_cache off;
        proxy_next_upstream off;
        proxy_ignore_client_abort off;
        gzip off;
    }

    location = /v1/responses {
        if (\$request_method != POST) { return 405; }
        if (\$codex_relay_api_authorized = 0) { return 401; }
        proxy_pass http://127.0.0.1:18317;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location = /v1/responses/compact {
        if (\$request_method != POST) { return 405; }
        if (\$codex_relay_api_authorized = 0) { return 401; }
        proxy_pass http://127.0.0.1:18317;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # Optional sidecars install narrowly scoped locations through this include.
    include /etc/nginx/snippets/codex-artifact-relay-*.conf;

    location / {
        default_type application/json;
        return 404 '{"error":{"message":"Not found","type":"invalid_request_error"}}';
    }
}
EOF

chmod 0600 /etc/nginx/sites-available/codex-relay
ln -sfn /etc/nginx/sites-available/codex-relay /etc/nginx/sites-enabled/codex-relay
install -o www-data -g adm -m 0640 /dev/null /var/log/nginx/codex-relay-error.log
nginx -t
systemctl enable --now nginx
systemctl reload nginx

cat >/root/codex-relay-credentials.txt <<EOF
PUBLIC_HOST=$PUBLIC_HOST
PUBLIC_PORT=$PUBLIC_PORT
TIMEZONE=$TIMEZONE
API_BASE_URL=$PUBLIC_URL/v1
API_KEY=$API_KEY
USAGE_DASHBOARD=$PUBLIC_URL/usage/
DASHBOARD_PASSWORD=$DASHBOARD_PASSWORD
MANAGEMENT_PANEL=$PUBLIC_URL/management.html
MANAGEMENT_KEY=$MANAGEMENT_KEY
EOF
chmod 0600 /root/codex-relay-credentials.txt

log "Running final service checks"
for service in squid cliproxyapi cpa-usage-keeper nginx; do
  systemctl is-active --quiet "$service" || die "$service is not active. Check: journalctl -u $service"
done

LOCAL_URL="http://127.0.0.1:$PUBLIC_PORT"
wait_for_http 200 "$LOCAL_URL/usage/" || die "The usage dashboard did not become ready."
curl --noproxy '*' -fsS "$LOCAL_URL/usage/" | \
  grep -Fq '<title>Codex 中转使用情况</title>' || \
  die "The plain-Chinese usage dashboard is not being served."
wait_for_http 200 "$LOCAL_URL/usage/key-overview" || \
  die "The API-key usage overview page did not become ready."
wait_for_http 200 "$LOCAL_URL/usage/healthz" || \
  die "The usage dashboard health endpoint did not become ready."
wait_for_http 401 "$LOCAL_URL/usage/api/v1/status" || \
  die "The usage dashboard API authentication is not enforced."
wait_for_http 404 "$LOCAL_URL/usage/assets/not-a-real-asset.js" || \
  die "A missing usage dashboard asset does not return 404."
wait_for_http 200 "$LOCAL_URL/management.html" || die "The management page did not become ready."
wait_for_http 401 "$LOCAL_URL/v0/management/config" || die "Management authentication is not enforced."
wait_for_http 405 "$LOCAL_URL/v0/management/config" \
  -X PUT -H "Authorization: Bearer $MANAGEMENT_KEY" \
  -H 'Content-Type: application/json' -d '{}' || \
  die "Public management writes are not blocked."
wait_for_http 404 "$LOCAL_URL/v0/management/auth-files/download" \
  -H "Authorization: Bearer $MANAGEMENT_KEY" || \
  die "Raw OAuth credential downloads are not blocked."
wait_for_http 404 "$LOCAL_URL/v0/management/usage-queue" \
  -H "Authorization: Bearer $MANAGEMENT_KEY" || \
  die "The destructive usage queue endpoint is publicly reachable."
wait_for_http 404 "$LOCAL_URL/v0/management/codex-auth-url" \
  -H "Authorization: Bearer $MANAGEMENT_KEY" || \
  die "A side-effecting OAuth endpoint is publicly reachable."
wait_for_http 401 "$LOCAL_URL/v1/images/generations" \
  -X POST -H 'Content-Type: application/json' -d '{}' || \
  die "Image generation authentication is not enforced."
wait_for_http 401 "$LOCAL_URL/v1/images/generations" \
  -X POST -H 'Authorization: Bearer wrong-nonempty-key' \
  -H 'Content-Type: application/json' -d '{}' || \
  die "Image generation accepts a forged non-empty Authorization header."
wait_for_http 400 "$LOCAL_URL/v1/images/generations" \
  -X POST -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' -d '{}' || \
  die "The image generation route did not reach CLIProxyAPI."
wait_for_http 401 "$LOCAL_URL/v1/images/edits" \
  -X POST -F 'model=gpt-image-2' -F 'prompt=route check' || \
  die "Image editing authentication is not enforced."
wait_for_http 401 "$LOCAL_URL/v1/images/edits" \
  -X POST -H 'Authorization: Bearer wrong-nonempty-key' \
  -F 'model=gpt-image-2' -F 'prompt=route check' || \
  die "Image editing accepts a forged non-empty Authorization header."
wait_for_http 400 "$LOCAL_URL/v1/images/edits" \
  -X POST -H "Authorization: Bearer $API_KEY" \
  -F 'model=gpt-image-2' -F 'prompt=route check' || \
  die "The image editing route did not reach CLIProxyAPI."
wait_for_http 404 "$LOCAL_URL/v1/images/edits/" -X POST || \
  die "The image editing trailing-slash route is unexpectedly public."
wait_for_http 404 "$LOCAL_URL/v1/files" -X POST || \
  die "The general file upload endpoint is unexpectedly public."

INFERENCE_STATUS="not run"
if [[ "$AUTH_READY" == "1" ]]; then
  MODELS_FILE="$WORK_DIR/models.json"
  if curl --noproxy '*' -fsS --max-time 60 \
      -H "Authorization: Bearer $API_KEY" \
      "$LOCAL_URL/v1/models" -o "$MODELS_FILE"; then
    MODEL_ID="${INFERENCE_MODEL:-$(jq -r '
      [.data[]?.id | select(test("image|review"; "i") | not)][0] // empty
    ' "$MODELS_FILE")}"
  else
    MODEL_ID=""
  fi
  if [[ -z "$MODEL_ID" ]]; then
    warn "The authenticated /v1/models check returned no suitable text model."
    INFERENCE_STATUS="not run: no suitable model was returned"
  fi

  if [[ -n "$MODEL_ID" && "${SKIP_INFERENCE_TEST:-0}" != "1" ]]; then
    INFERENCE_FILE="$WORK_DIR/inference.json"
    touch "$WORK_DIR/inference-started"
    INFERENCE_CODE="$(curl --noproxy '*' -sS --max-time 300 -o "$INFERENCE_FILE" -w '%{http_code}' \
      -H "Authorization: Bearer $API_KEY" \
      -H 'Content-Type: application/json' \
      --data "$(jq -nc --arg model "$MODEL_ID" '{model:$model,input:"Reply with exactly OK.",stream:false}')" \
      "$LOCAL_URL/v1/responses" || true)"
    if [[ "$INFERENCE_CODE" != "200" ]]; then
      printf 'Inference response: %s\n' "$(head -c 1000 "$INFERENCE_FILE" 2>/dev/null || true)" >&2
      warn "The real /v1/responses test returned HTTP ${INFERENCE_CODE:-none}; services remain installed for retry."
      INFERENCE_STATUS="failed with HTTP ${INFERENCE_CODE:-none}; rerun with REPAIR=1 after checking OAuth/network"
    else
      INFERENCE_STATUS="passed with model $MODEL_ID"
      for _ in $(seq 1 30); do
        NEW_REQUEST_LOG="$(find /var/lib/cliproxyapi/logs -maxdepth 1 -type f \
          -name 'v1-responses-*.log' -newer "$WORK_DIR/inference-started" -print -quit)"
        PERSISTED_EVENT_COUNT=0
        if [[ -n "$NEW_REQUEST_LOG" ]]; then
          REQUEST_ID="${NEW_REQUEST_LOG##*-}"
          REQUEST_ID="${REQUEST_ID%.log}"
          if [[ "$REQUEST_ID" =~ ^[0-9a-fA-F]{8}$ ]]; then
            PERSISTED_EVENT_COUNT="$(runuser -u cpausage -- \
              sqlite3 -readonly /var/lib/cpa-usage-keeper/app.db \
              "SELECT COUNT(*) FROM usage_events WHERE request_id = '$REQUEST_ID';" \
              2>/dev/null || printf '0')"
          fi
        fi
        if [[ -n "$NEW_REQUEST_LOG" && "$PERSISTED_EVENT_COUNT" =~ ^[0-9]+$ ]] && \
           (( PERSISTED_EVENT_COUNT > 0 )); then
          break
        fi
        sleep 1
      done
      if [[ -z "${NEW_REQUEST_LOG:-}" || ! "${PERSISTED_EVENT_COUNT:-0}" =~ ^[0-9]+$ ]] || \
         (( PERSISTED_EVENT_COUNT == 0 )); then
        warn "Inference passed, but the matching request ID was not confirmed in both the new log and Keeper database."
        INFERENCE_STATUS="$INFERENCE_STATUS; persistence check incomplete"
      fi
    fi
  elif [[ -n "$MODEL_ID" ]]; then
    INFERENCE_STATUS="skipped by SKIP_INFERENCE_TEST=1"
  fi
fi

printf 'completed=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >"$COMPLETE_MARKER"

cat <<EOF

Installation completed.

Open inbound TCP $PUBLIC_PORT in the cloud firewall, preferably only for your client IPs.
The current endpoint uses plain HTTP. Configure HTTPS before using it over untrusted networks.

API base URL:       $PUBLIC_URL/v1
API key:            $API_KEY
Image generation:   $PUBLIC_URL/v1/images/generations
Image editing:      $PUBLIC_URL/v1/images/edits
Usage dashboard:    $PUBLIC_URL/usage/
Dashboard password: $DASHBOARD_PASSWORD
Management panel:   $PUBLIC_URL/management.html (viewing requires the management key)
Management key:     $MANAGEMENT_KEY

Credentials were also saved to /root/codex-relay-credentials.txt with mode 0600.
Inference test: $INFERENCE_STATUS

Usage events are retained in SQLite without automatic cleanup. Request body logs are capped at 512 MiB.
Check disk usage periodically with: du -sh /var/lib/cpa-usage-keeper /var/lib/cliproxyapi/logs
Management GET responses can contain API keys and full request bodies. Use HTTPS or restrict
the cloud firewall to your own client IPs before relying on the public management view.
EOF

if [[ "$AUTH_READY" != "1" ]]; then
  cat <<'EOF'

Codex authentication is still pending. Run:

  sudo runuser -u cliproxyapi -- env HOME=/var/lib/cliproxyapi \
    /opt/cliproxyapi/cli-proxy-api \
    -config /etc/cliproxyapi/config.yaml \
    -codex-device-login -no-browser

Then restart and verify:

  sudo systemctl restart cliproxyapi cpa-usage-keeper
  API_KEY=$(sudo sed -n 's/^API_KEY=//p' /root/codex-relay-credentials.txt)
EOF
  printf "  curl -H \"Authorization: Bearer \\\$API_KEY\" http://127.0.0.1:%s/v1/models\n" "$PUBLIC_PORT"
fi
