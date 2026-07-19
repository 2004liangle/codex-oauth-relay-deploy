#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

VERSION="v1.4.0"
REPOSITORY="2004liangle/codex-oauth-relay-deploy"
RAW_BASE="https://raw.githubusercontent.com/$REPOSITORY/$VERSION/skills/relay-images"
TARGET="${CODEX_HOME:-$HOME/.codex}/skills/relay-images"
STAGING=""

manifest() {
  cat <<'EOF'
014c64236e0f9e161304f2185fad2eba4369e6e6193d2b471f19fb60841d58d0  SKILL.md
57428fdbca39e223f0545603b540141867c636e350873cdef90837c7f40a7dff  agents/openai.yaml
d454b186cf1affa73103e72f4cb21df52c5cfb9e45031b5d2ef0607094d64562  references/artifact-delivery.md
a51df8d8d82b01f81ed3e5621afecd97236474cf2669255c4530f7270ad173e8  references/image-options.md
b15a40a55251783e9ee6e4354591bff65e4311157137da9037aad9720bfe25a4  references/prompting.md
b01e0363071940416b3f5ef9ccfd21dc73cc7c4ba65c2f828ecf15dbb4fdeca8  references/relay-contract.md
4bbd2b1ca72a45217b2f2dcc1450c4913693a383eb0235cea96310def18496f2  scripts/relay_images.py
EOF
}

cleanup() {
  if [[ -n "$STAGING" && -d "$STAGING" ]]; then
    find "$STAGING" -type f -delete
    find "$STAGING" -depth -type d -empty -delete
  fi
}
trap cleanup EXIT

usage() {
  cat <<'EOF'
Relay Images Skill installer

Usage:
  bash install-relay-images-skill.sh
  bash install-relay-images-skill.sh --verify-local PATH

The Skill is installed to ${CODEX_HOME:-$HOME/.codex}/skills/relay-images.
An existing installation is backed up outside the Codex skills directory.
EOF
}

verify_directory() {
  local directory="$1"
  local expected file actual failed=0
  [[ -d "$directory" ]] || {
    printf 'Skill directory not found: %s\n' "$directory" >&2
    return 1
  }
  while read -r expected file; do
    if [[ ! -f "$directory/$file" ]]; then
      actual=""
    elif command -v sha256sum >/dev/null 2>&1; then
      actual="$(sha256sum "$directory/$file" 2>/dev/null | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
      actual="$(shasum -a 256 "$directory/$file" 2>/dev/null | awk '{print $1}')"
    else
      printf 'Required SHA-256 command not found: install sha256sum or shasum\n' >&2
      return 1
    fi
    if [[ "$actual" == "$expected" ]]; then
      printf '%s: OK\n' "$file"
    else
      printf '%s: FAILED\n' "$file" >&2
      failed=1
    fi
  done < <(manifest)
  return "$failed"
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ "${1:-}" == "--verify-local" ]]; then
  [[ $# -eq 2 ]] || {
    usage >&2
    exit 2
  }
  verify_directory "$2"
  exit 0
fi

[[ $# -eq 0 ]] || {
  usage >&2
  exit 2
}

for command in curl mktemp python3; do
  command -v "$command" >/dev/null 2>&1 || {
    printf 'Required command not found: %s\n' "$command" >&2
    exit 1
  }
done
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  printf 'Python 3.10 or newer is required.\n' >&2
  exit 1
}
if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
  printf 'Required SHA-256 command not found: install sha256sum or shasum\n' >&2
  exit 1
fi

TARGET_PARENT="$(dirname "$TARGET")"
mkdir -p "$TARGET_PARENT"
STAGING="$(mktemp -d "$TARGET_PARENT/.relay-images.XXXXXX")"

while read -r _ file; do
  mkdir -p "$STAGING/$(dirname "$file")"
  curl --proto '=https' --tlsv1.2 -fsSL \
    --connect-timeout 15 --max-time 300 --retry 3 --retry-all-errors \
    "$RAW_BASE/$file" -o "$STAGING/$file"
done < <(manifest)

verify_directory "$STAGING"
chmod 0644 "$STAGING/SKILL.md" "$STAGING/agents/openai.yaml" "$STAGING"/references/*.md
chmod 0755 "$STAGING/scripts/relay_images.py"

if [[ -e "$TARGET" || -L "$TARGET" ]]; then
  BACKUP_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/relay-images/backups"
  mkdir -p "$BACKUP_ROOT"
  BACKUP="$BACKUP_ROOT/relay-images-$(date -u '+%Y%m%dT%H%M%SZ')"
  mv "$TARGET" "$BACKUP"
  printf 'Previous Skill backed up to %s\n' "$BACKUP"
fi

mv "$STAGING" "$TARGET"
STAGING=""

cat <<EOF
Relay Images Skill $VERSION installed at:
  $TARGET

Configure it with:
  $TARGET/scripts/relay_images.py configure --base-url 'https://relay.example.com/v1'

For an intentional remote HTTP relay, append --allow-http.
Restart Codex or open a new session before invoking \$relay-images.
EOF
