#!/usr/bin/env bash
set -Eeuo pipefail

umask 077
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$SCRIPT_DIR/artifact-relay/artifact_relay.py"
DREAMINA_RUNNER_SOURCE="$SCRIPT_DIR/artifact-relay/dreamina_agent_cutout.mjs"
BACKGROUND_REMOVER_SOURCE="$SCRIPT_DIR/artifact-relay/remove_background.py"
REQUIREMENTS_SOURCE="$SCRIPT_DIR/artifact-relay/requirements.txt"
INSTALL_ROOT="/opt/codex-artifact-relay"
VENV=""
VENV_STAGING=""
STATE_DIR="/var/lib/codex-artifact-relay"
MODEL_DIR="$STATE_DIR/models"
DREAMINA_PROFILE_DIR="$STATE_DIR/dreamina-profile"
DREAMINA_DIAGNOSTICS_DIR="$STATE_DIR/dreamina-diagnostics"
CONFIG_DIR="/etc/codex-artifact-relay"
ENV_FILE="$CONFIG_DIR/env"
UNIT_FILE="/etc/systemd/system/codex-artifact-relay.service"
LOCAL_CLI="/usr/local/sbin/codex-artifact-relay-local"
NGINX_SITE="/etc/nginx/sites-available/codex-relay"
NGINX_SNIPPET="/etc/nginx/snippets/codex-artifact-relay-locations.conf"
INTERNAL_PORT="${ARTIFACT_RELAY_PORT:-18318}"
WORKER_COUNT="${ARTIFACT_RELAY_WORKERS:-2}"
REMBG_VERSION="2.0.77"
PYTHON_BIN="${ARTIFACT_RELAY_PYTHON:-}"
DREAMINA_NODE="${ARTIFACT_RELAY_DREAMINA_NODE:-/usr/bin/node}"
DREAMINA_BROWSER="${ARTIFACT_RELAY_DREAMINA_BROWSER:-}"
DREAMINA_PROFILE_SOURCE="${ARTIFACT_RELAY_DREAMINA_PROFILE_SOURCE:-}"
GENERAL_MODEL_SHA256="60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a"
ANIME_MODEL_SHA256="f15622d853e8260172812b657053460e20806f04b9e05147d49af7bed31a6e99"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [[ -n "$VENV_STAGING" && -d "$VENV_STAGING" ]]; then
    rm -rf -- "$VENV_STAGING"
  fi
}
trap cleanup EXIT

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
[[ -f "$DREAMINA_RUNNER_SOURCE" ]] || die "The Dreamina Agent runner is missing."
[[ -f "$BACKGROUND_REMOVER_SOURCE" ]] || die "The background-removal helper is missing."
[[ -f "$REQUIREMENTS_SOURCE" ]] || die "The artifact relay requirements file is missing."
if [[ -n "$PYTHON_BIN" ]]; then
  [[ "$PYTHON_BIN" == /* && "$PYTHON_BIN" =~ ^/[A-Za-z0-9_./+-]+$ ]] || \
    die "ARTIFACT_RELAY_PYTHON must be a safe absolute executable path."
  [[ -x "$PYTHON_BIN" ]] || die "ARTIFACT_RELAY_PYTHON is not executable: $PYTHON_BIN"
  PYTHON_BIN="$(readlink -f -- "$PYTHON_BIN")"
else
  for CANDIDATE in \
    /usr/bin/python3 \
    /usr/bin/python3.13 /usr/bin/python3.12 /usr/bin/python3.11 \
    /usr/local/bin/python3 /usr/local/bin/python3.13 \
    /usr/local/bin/python3.12 /usr/local/bin/python3.11; do
    if [[ -x "$CANDIDATE" ]] && \
      "$CANDIDATE" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
        >/dev/null 2>&1; then
      PYTHON_BIN="$(readlink -f -- "$CANDIDATE")"
      break
    fi
  done
fi
[[ -n "$PYTHON_BIN" ]] || \
  die "Transparent artifact output requires Python 3.11 or newer. Set ARTIFACT_RELAY_PYTHON to its absolute path."
[[ "$PYTHON_BIN" == /* && "$PYTHON_BIN" =~ ^/[A-Za-z0-9_./+-]+$ ]] || \
  die "The selected Python resolves to an unsupported path."
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || \
  die "ARTIFACT_RELAY_PYTHON must point to Python 3.11 or newer."
if ! "$PYTHON_BIN" -c 'import ensurepip, venv' >/dev/null 2>&1; then
  PYTHON_SERIES="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if [[ "$PYTHON_BIN" == /usr/bin/python3 ]]; then
    VENV_PACKAGE="python3-venv"
  elif [[ "$PYTHON_BIN" == /usr/bin/python* ]]; then
    VENV_PACKAGE="python${PYTHON_SERIES}-venv"
  else
    die "The selected Python lacks venv/ensurepip. Install its venv support and retry."
  fi
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$VENV_PACKAGE"
fi
REQUIREMENTS_SHA="$(sha256sum "$REQUIREMENTS_SOURCE" | cut -d' ' -f1)"
PYTHON_TAG="$("$PYTHON_BIN" -c 'import sys; print(f"cp{sys.version_info.major}{sys.version_info.minor}")')"
[[ "$PYTHON_TAG" =~ ^cp[0-9]{3}$ ]] || die "Could not determine the selected Python ABI tag."
VENV="$INSTALL_ROOT/venv-${PYTHON_TAG}-${REQUIREMENTS_SHA:0:16}"
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
[[ "$DREAMINA_NODE" == /* && "$DREAMINA_NODE" =~ ^/[A-Za-z0-9_./+-]+$ && -x "$DREAMINA_NODE" ]] || \
  die "ARTIFACT_RELAY_DREAMINA_NODE must be an executable absolute path."
"$DREAMINA_NODE" -e 'const major=Number(process.versions.node.split(".")[0]);process.exit(major>=22?0:1)' || \
  die "Dreamina Agent automation requires Node.js 22 or newer."
if [[ -z "$DREAMINA_BROWSER" ]]; then
  for CANDIDATE in \
    /usr/bin/google-chrome /usr/bin/chromium /usr/bin/chromium-browser \
    "$RUN_HOME"/.cache/ms-playwright/chromium-*/chrome-linux64/chrome; do
    if [[ -x "$CANDIDATE" ]]; then
      DREAMINA_BROWSER="$CANDIDATE"
    fi
  done
fi
[[ "$DREAMINA_BROWSER" == /* && "$DREAMINA_BROWSER" =~ ^/[A-Za-z0-9_./+-]+$ && -x "$DREAMINA_BROWSER" ]] || \
  die "Set ARTIFACT_RELAY_DREAMINA_BROWSER to an executable Chromium path."
if [[ -n "$DREAMINA_PROFILE_SOURCE" ]]; then
  [[ "$DREAMINA_PROFILE_SOURCE" == /* && "$DREAMINA_PROFILE_SOURCE" =~ ^/[A-Za-z0-9_./+-]+$ && -d "$DREAMINA_PROFILE_SOURCE" ]] || \
    die "ARTIFACT_RELAY_DREAMINA_PROFILE_SOURCE must be a readable absolute directory."
fi
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
install -d -o "$RUN_USER" -g "$RUN_GROUP" -m 0700 "$MODEL_DIR"
install -d -o "$RUN_USER" -g "$RUN_GROUP" -m 0700 "$DREAMINA_PROFILE_DIR"
install -d -o "$RUN_USER" -g "$RUN_GROUP" -m 0700 "$DREAMINA_DIAGNOSTICS_DIR"
if [[ -n "$DREAMINA_PROFILE_SOURCE" && ! -f "$DREAMINA_PROFILE_DIR/Local State" ]]; then
  cp -a "$DREAMINA_PROFILE_SOURCE"/. "$DREAMINA_PROFILE_DIR"/
  rm -f -- "$DREAMINA_PROFILE_DIR/SingletonCookie" \
    "$DREAMINA_PROFILE_DIR/SingletonLock" "$DREAMINA_PROFILE_DIR/SingletonSocket"
  chown -R "$RUN_USER":"$RUN_GROUP" "$DREAMINA_PROFILE_DIR"
  chmod 0700 "$DREAMINA_PROFILE_DIR"
fi

INSTALLED_REQUIREMENTS_SHA=""
if [[ -r "$VENV/.artifact-relay-requirements.sha256" ]]; then
  INSTALLED_REQUIREMENTS_SHA="$(cat "$VENV/.artifact-relay-requirements.sha256")"
fi
if [[ "$INSTALLED_REQUIREMENTS_SHA" != "$REQUIREMENTS_SHA" ]] || \
  ! "$VENV/bin/python" -c "from importlib.metadata import version; raise SystemExit(0 if version('rembg') == '$REMBG_VERSION' else 1)" >/dev/null 2>&1; then
  VENV_STAGING="$(mktemp -d "$INSTALL_ROOT/.venv-staging.XXXXXX")"
  "$PYTHON_BIN" -m venv "$VENV_STAGING" || die "Could not create the background-removal environment."
  "$VENV_STAGING/bin/python" -m pip install --disable-pip-version-check --no-cache-dir \
    --index-url https://pypi.org/simple --only-binary=:all: \
    --requirement "$REQUIREMENTS_SOURCE" || die "Could not install background-removal dependencies."
  printf '%s\n' "$REQUIREMENTS_SHA" >"$VENV_STAGING/.artifact-relay-requirements.sha256"
  chown -R root:"$RUN_GROUP" "$VENV_STAGING"
  chmod -R u=rwX,g=rX,o= "$VENV_STAGING"
  if [[ -e "$VENV" ]]; then
    mv "$VENV" "$VENV.invalid-$(date -u '+%Y%m%dT%H%M%SZ')"
  fi
  mv "$VENV_STAGING" "$VENV"
  VENV_STAGING=""
fi
chown -R root:"$RUN_GROUP" "$VENV"
chmod -R u=rwX,g=rX,o= "$VENV"

for MODEL in isnet-general-use isnet-anime; do
  runuser -u "$RUN_USER" -- env -i \
    HOME="$STATE_DIR" \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    OMP_NUM_THREADS=2 \
    U2NET_HOME="$MODEL_DIR" \
    XDG_CACHE_HOME="$STATE_DIR/cache" \
    "$VENV/bin/python" "$BACKGROUND_REMOVER_SOURCE" warmup --model "$MODEL" || \
    die "Could not initialize background-removal model: $MODEL"
done
[[ -s "$MODEL_DIR/isnet-general-use.onnx" ]] || die "The general background-removal model is missing."
[[ -s "$MODEL_DIR/isnet-anime.onnx" ]] || die "The anime background-removal model is missing."
printf '%s  %s\n' "$GENERAL_MODEL_SHA256" "$MODEL_DIR/isnet-general-use.onnx" | \
  sha256sum -c - >/dev/null || die "The general background-removal model checksum is invalid."
printf '%s  %s\n' "$ANIME_MODEL_SHA256" "$MODEL_DIR/isnet-anime.onnx" | \
  sha256sum -c - >/dev/null || die "The anime background-removal model checksum is invalid."

install -o root -g root -m 0755 "$SOURCE" "$INSTALL_ROOT/artifact_relay.py"
install -o root -g root -m 0755 "$DREAMINA_RUNNER_SOURCE" "$INSTALL_ROOT/dreamina_agent_cutout.mjs"
install -o root -g root -m 0755 "$BACKGROUND_REMOVER_SOURCE" "$INSTALL_ROOT/remove_background.py"
install -o root -g root -m 0644 "$REQUIREMENTS_SOURCE" "$INSTALL_ROOT/requirements.txt"

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
ARTIFACT_RELAY_BACKGROUND_REMOVAL_PYTHON=$VENV/bin/python
ARTIFACT_RELAY_BACKGROUND_REMOVAL_SCRIPT=$INSTALL_ROOT/remove_background.py
ARTIFACT_RELAY_BACKGROUND_REMOVAL_MODEL_DIR=$MODEL_DIR
ARTIFACT_RELAY_BACKGROUND_REMOVAL_MODEL=isnet-general-use
ARTIFACT_RELAY_BACKGROUND_REMOVAL_TIMEOUT=600
ARTIFACT_RELAY_DREAMINA_NODE=$DREAMINA_NODE
ARTIFACT_RELAY_DREAMINA_RUNNER=$INSTALL_ROOT/dreamina_agent_cutout.mjs
ARTIFACT_RELAY_DREAMINA_BROWSER=$DREAMINA_BROWSER
ARTIFACT_RELAY_DREAMINA_PROFILE_DIR=$DREAMINA_PROFILE_DIR
ARTIFACT_RELAY_DREAMINA_DIAGNOSTICS_DIR=$DREAMINA_DIAGNOSTICS_DIR
ARTIFACT_RELAY_DREAMINA_TIMEOUT=900
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
ExecStart=$VENV/bin/python $INSTALL_ROOT/artifact_relay.py serve
Restart=on-failure
RestartSec=5s
TimeoutStopSec=30s
UMask=0077
MemoryAccounting=true
MemoryHigh=50%
MemoryMax=70%
MemorySwapMax=1G
TasksMax=384

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
  $VENV/bin/python $INSTALL_ROOT/artifact_relay.py "\$@"
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
  '.delivery == "lark_drive" and .retention == "manual" and .input_target.token == $token and (.operations | index("image.cutout")) != null and .cutout.provider == "dreamina_agent"' \
  >/dev/null || die "Artifact capabilities are not available through Nginx."

mapfile -t INSTALLED_VENVS < <(
  find "$INSTALL_ROOT" -mindepth 1 -maxdepth 1 -type d \
    \( -name venv -o -name 'venv-*' \) -printf '%T@ %f\n' | sort -rn | awk '{print $2}'
)
ROLLBACK_KEPT=0
for VENV_NAME in "${INSTALLED_VENVS[@]}"; do
  [[ "$VENV_NAME" == "venv" || "$VENV_NAME" =~ ^venv(-cp[0-9]{3})?-[0-9a-f]{16}$ ]] || \
    continue
  VENV_PATH="$INSTALL_ROOT/$VENV_NAME"
  if [[ "$VENV_PATH" == "$VENV" ]]; then
    continue
  fi
  if (( ROLLBACK_KEPT == 0 )); then
    ROLLBACK_KEPT=1
    continue
  fi
  rm -rf -- "$VENV_PATH"
done
for INVALID_PATH in "$INSTALL_ROOT"/*; do
  [[ -d "$INVALID_PATH" ]] || continue
  INVALID_NAME="$(basename -- "$INVALID_PATH")"
  if [[ "$INVALID_NAME" =~ ^venv(-cp[0-9]{3})?-[0-9a-f]{16}\.invalid-[0-9]{8}T[0-9]{6}Z$ ]]; then
    rm -rf -- "$INVALID_PATH"
  fi
done
for STAGING_PATH in "$INSTALL_ROOT"/.venv-staging.*; do
  [[ -d "$STAGING_PATH" ]] || continue
  STAGING_NAME="$(basename -- "$STAGING_PATH")"
  if [[ "$STAGING_NAME" =~ ^\.venv-staging\.[A-Za-z0-9]{6}$ ]]; then
    rm -rf -- "$STAGING_PATH"
  fi
done

printf 'Artifact relay installed. Public routes use the existing API base URL and relay key.\n'
