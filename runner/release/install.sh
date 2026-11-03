#!/bin/sh
# Install dsh-runner from the S3 release channel.
#
#   curl -fsSL "$BASE/install/install.sh" | DSH_RUNNER_BASE="$BASE" sh
#
# Env: DSH_RUNNER_BASE (required), DSH_RUNNER_CHANNEL (stable|beta),
#      DSH_RUNNER_BIN_DIR (default ~/.local/bin), DSH_RUNNER_VERSION (pin).
set -eu

BASE=${DSH_RUNNER_BASE:?set DSH_RUNNER_BASE to the S3 install root, e.g. https://cdn.example.com/dsh-runner}
CHANNEL=${DSH_RUNNER_CHANNEL:-stable}
BIN_DIR=${DSH_RUNNER_BIN_DIR:-$HOME/.local/bin}
VERSION=${DSH_RUNNER_VERSION:-}

os=$(uname -s | tr '[:upper:]' '[:lower:]')
case "$os" in
darwin | linux) ;;
*)
	echo "unsupported OS: $os" >&2
	exit 1
	;;
esac
arch=$(uname -m)
case "$arch" in
arm64 | aarch64) arch=arm64 ;;
x86_64 | amd64) arch=amd64 ;;
*)
	echo "unsupported arch: $arch" >&2
	exit 1
	;;
esac

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

if [ -z "$VERSION" ]; then
	channel_json=$(curl -fsSL "$BASE/channels/$CHANNEL.json")
	VERSION=$(printf '%s' "$channel_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')
fi
manifest=$(curl -fsSL "$BASE/releases/$VERSION/manifest.json")
printf '%s' "$manifest" > "$tmp/manifest.json"

python3 - "$tmp/manifest.json" "$os-$arch" > "$tmp/artifact.txt" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
art = data["artifacts"][sys.argv[2]]
print(art["url"], art["sha256"])
PY
url=$(cut -d' ' -f1 "$tmp/artifact.txt")
sha=$(cut -d' ' -f2 "$tmp/artifact.txt")

echo "installing dsh-runner $VERSION ($os-$arch)"
curl -fsSL "$BASE/$url" -o "$tmp/runner.tar.gz"
actual=$(shasum -a 256 "$tmp/runner.tar.gz" 2>/dev/null | cut -d' ' -f1 || sha256sum "$tmp/runner.tar.gz" | cut -d' ' -f1)
if [ "$actual" != "$sha" ]; then
	echo "sha256 mismatch: expected $sha, got $actual" >&2
	exit 1
fi

tar -xzf "$tmp/runner.tar.gz" -C "$tmp"
mkdir -p "$BIN_DIR"
install -m 0755 "$tmp/dsh-runner" "$BIN_DIR/dsh-runner"

echo "installed: $BIN_DIR/dsh-runner ($("$BIN_DIR/dsh-runner" version))"
case ":$PATH:" in
*":$BIN_DIR:"*) ;;
*) echo "note: add $BIN_DIR to PATH" ;;
esac
cat <<EOF

next:
  dsh-runner login --server http://<panel>:4096 --oauth
  dsh-runner doctor
  dsh-runner start
EOF
