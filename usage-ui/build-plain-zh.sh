#!/usr/bin/env bash
set -Eeuo pipefail

umask 022
export LC_ALL=C
export TZ=UTC

SOURCE_REPOSITORY="https://github.com/Willxup/cpa-usage-keeper.git"
SOURCE_COMMIT="05573ca5aa701786b9ecf1b5af56e3cc31547ca8"
SOURCE_VERSION="v1.13.2"
UI_VERSION="1.13.2-plain-zh.2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${1:-$SCRIPT_DIR/../dist}"
PATCH_FILE="$SCRIPT_DIR/plain-zh.patch"
PACKAGE_NAME="cpa-usage-ui_${UI_VERSION}"
OUTPUT_FILE="$OUTPUT_DIR/${PACKAGE_NAME}.tar.gz"
WORK_DIR=""

cleanup() {
  if [[ -n "$WORK_DIR" && -d "$WORK_DIR" ]]; then
    rm -rf -- "$WORK_DIR"
  fi
}
trap cleanup EXIT

for command in git npm tar gzip sha256sum; do
  command -v "$command" >/dev/null 2>&1 || {
    printf 'Required command not found: %s\n' "$command" >&2
    exit 1
  }
done

[[ -f "$PATCH_FILE" ]] || {
  printf 'Patch not found: %s\n' "$PATCH_FILE" >&2
  exit 1
}
[[ ! -e "$OUTPUT_FILE" ]] || {
  printf 'Refusing to overwrite existing output: %s\n' "$OUTPUT_FILE" >&2
  exit 1
}

WORK_DIR="$(mktemp -d /tmp/cpa-usage-ui-build.XXXXXX)"
SOURCE_DIR="$WORK_DIR/source"
PACKAGE_ROOT="$WORK_DIR/package"

git init -q "$SOURCE_DIR"
git -C "$SOURCE_DIR" remote add origin "$SOURCE_REPOSITORY"
git -C "$SOURCE_DIR" fetch -q --depth 1 origin "$SOURCE_COMMIT"
git -C "$SOURCE_DIR" checkout -q --detach FETCH_HEAD
[[ "$(git -C "$SOURCE_DIR" rev-parse HEAD)" == "$SOURCE_COMMIT" ]] || {
  printf 'Fetched source commit does not match the pinned commit.\n' >&2
  exit 1
}

git -C "$SOURCE_DIR" apply --check "$PATCH_FILE"
git -C "$SOURCE_DIR" apply "$PATCH_FILE"

npm --prefix "$SOURCE_DIR/web" ci
npm --prefix "$SOURCE_DIR/web" run test
npm --prefix "$SOURCE_DIR/web" run lint
npm --prefix "$SOURCE_DIR/web" run typecheck
npm --prefix "$SOURCE_DIR/web" run build

sed -i \
  -e 's|<base href="__APP_BASE_PATH__/" />|<base href="/usage/" />|' \
  -e 's|window.__APP_BASE_PATH__ = "__APP_BASE_PATH__";|window.__APP_BASE_PATH__ = "/usage";|' \
  "$SOURCE_DIR/web/dist/index.html"
grep -Fq '<html lang="zh-CN">' "$SOURCE_DIR/web/dist/index.html"
grep -Fq '<base href="/usage/" />' "$SOURCE_DIR/web/dist/index.html"
grep -Fq '<title>Codex 中转使用情况</title>' "$SOURCE_DIR/web/dist/index.html"
grep -Fq 'window.__APP_BASE_PATH__ = "/usage";' "$SOURCE_DIR/web/dist/index.html"
if grep -Fq 'href="__APP_BASE_PATH__/' "$SOURCE_DIR/web/dist/index.html" || \
   grep -Fq '= "__APP_BASE_PATH__";' "$SOURCE_DIR/web/dist/index.html"; then
  printf 'The built index still contains the base-path placeholder.\n' >&2
  exit 1
fi

install -d "$PACKAGE_ROOT/$PACKAGE_NAME/usage"
cp -a "$SOURCE_DIR/web/dist/." "$PACKAGE_ROOT/$PACKAGE_NAME/usage/"
install -m 0644 "$SOURCE_DIR/LICENSE" "$PACKAGE_ROOT/$PACKAGE_NAME/LICENSE"
printf '%s\n' \
  "Source: $SOURCE_REPOSITORY" \
  "Upstream version: $SOURCE_VERSION" \
  "Upstream commit: $SOURCE_COMMIT" \
  "Customization: plain Simplified Chinese labels for the Codex relay usage dashboard" \
  >"$PACKAGE_ROOT/$PACKAGE_NAME/SOURCE.txt"

find "$PACKAGE_ROOT/$PACKAGE_NAME" -type d -exec chmod 0755 {} +
find "$PACKAGE_ROOT/$PACKAGE_NAME" -type f -exec chmod 0644 {} +
install -d "$OUTPUT_DIR"
tar --sort=name \
  --mtime='UTC 2020-01-01' \
  --owner=0 --group=0 --numeric-owner \
  --format=posix \
  --pax-option=delete=atime,delete=ctime \
  -cf - -C "$PACKAGE_ROOT" "$PACKAGE_NAME" | gzip -n >"$OUTPUT_FILE"

printf 'Built %s\n' "$OUTPUT_FILE"
sha256sum "$OUTPUT_FILE"
