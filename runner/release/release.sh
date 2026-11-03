#!/bin/sh
# Build the dsh-runner CLI release artifacts published to S3.
#
#   VERSION=0.1.0-abc1234 ./runner/release/release.sh
#
# The runner is a pure-Python CLI (no .exe/.dmg): the artifact is a wheel,
# installed by `uv tool install` (see install/install.sh). Layout:
#
#   dist/releases/<version>/manifest.json
#   dist/releases/<version>/dsh_runner-<version>-py3-none-any.whl{,.sha256,.sig}
#   dist/channels/stable.json
#
# RELEASE_KEY (an ed25519 private key in PEM form) signs the wheel when set.
set -eu

cd "$(dirname "$0")/../.."
ROOT=$(pwd)
RUNNER_DIR="$ROOT/runner"
OUT=${OUT:-$ROOT/dist}
case "$OUT" in /*) ;; *) OUT="$ROOT/$OUT" ;; esac
VERSION=${VERSION:-0.1.0-$(git rev-parse --short HEAD 2>/dev/null || echo dev)}
UV=${UV:-uv}
: "${UV_CACHE_DIR:=$ROOT/.poc/uv-cache}"
: "${UV_PYTHON_INSTALL_DIR:=$ROOT/.poc/uv-python}"
export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR

DIST="$OUT/releases/$VERSION"
rm -rf "$DIST"
mkdir -p "$DIST" "$OUT/channels"

echo "building dsh-runner $VERSION"
(cd "$RUNNER_DIR" && cp pyproject.toml "$DIST/pyproject.toml.bak" \
	&& sed -e "s/^version = .*/version = \"$VERSION\"/" pyproject.toml >pyproject.toml.tmp \
	&& mv pyproject.toml.tmp pyproject.toml \
	&& "$UV" build --out-dir "$DIST" 2>&1 | tail -3; \
	mv "$DIST/pyproject.toml.bak" pyproject.toml)

WHEEL=$(ls "$DIST"/*.whl | head -1)
NAME=$(basename "$WHEEL")
if command -v sha256sum >/dev/null 2>&1; then
	SUM=$(sha256sum "$WHEEL" | awk '{print $1}')
else
	SUM=$(shasum -a 256 "$WHEEL" | awk '{print $1}')
fi
printf '%s  %s\n' "$SUM" "$NAME" >"$DIST/$NAME.sha256"

if [ -n "${RELEASE_KEY:-}" ]; then
	echo "signing $NAME"
	openssl pkeyutl -sign -inkey "$RELEASE_KEY" -rawin -in "$WHEEL" -out "$DIST/$NAME.sig"
else
	echo "RELEASE_KEY not set: publishing unsigned (dev only)"
fi

cat >"$DIST/manifest.json" <<JSON
{
  "version": "$VERSION",
  "kind": "python-wheel",
  "python": ">=3.12",
  "files": [
    { "name": "$NAME", "sha256": "$SUM", "size": $(wc -c <"$WHEEL" | tr -d ' ') }
  ]
}
JSON
cp "$DIST/manifest.json" "$OUT/channels/stable.json"

echo "artifacts in $DIST and $OUT/channels/stable.json"
ls -la "$DIST"
