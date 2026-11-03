#!/bin/sh
# Build the runner release artifacts published to S3.
#
#   VERSION=0.1.0-abc1234 ./runner/release/release.sh
#
# Layout (see platform-design/local-runner-cli.md §2):
#   dist/releases/<version>/manifest.json
#   dist/releases/<version>/<os>/<arch>/dsh-runner-<version>-<os>-<arch>.tar.gz{,.sha256,.sig}
#   dist/channels/stable.json
#
# RELEASE_KEY (an ed25519 private key in PEM form) signs the tarballs when set.
set -eu

cd "$(dirname "$0")/../.."
ROOT=$(pwd)
RUNNER_DIR="$ROOT/runner"
OUT=${OUT:-$ROOT/dist}
case "$OUT" in /*) ;; *) OUT="$ROOT/$OUT" ;; esac
VERSION=${VERSION:-0.1.0-$(git rev-parse --short HEAD 2>/dev/null || echo dev)}
MATRIX=${MATRIX:-"darwin/amd64 darwin/arm64 linux/amd64 linux/arm64"}
GO=${GO:-go}

rm -rf "$OUT/releases/$VERSION"
mkdir -p "$OUT/releases/$VERSION" "$OUT/channels"

entries=""
for pair in $MATRIX; do
	os=${pair%/*}
	arch=${pair#*/}
	name="dsh-runner-$VERSION-$os-$arch"
	dir="$OUT/releases/$VERSION/$os/$arch"
	mkdir -p "$dir"
	echo "building $name"
	(cd "$RUNNER_DIR" && GOOS=$os GOARCH=$arch CGO_ENABLED=0 "$GO" build \
		-trimpath -ldflags "-s -w -X main.Version=$VERSION" \
		-o "$dir/dsh-runner" .)
	(
		cd "$dir"
		tar -czf "$name.tar.gz" dsh-runner
		rm dsh-runner
		if command -v sha256sum >/dev/null 2>&1; then
			sha256sum "$name.tar.gz" > "$name.tar.gz.sha256"
		else
			shasum -a 256 "$name.tar.gz" > "$name.tar.gz.sha256"
		fi
		if [ -n "${RELEASE_KEY:-}" ]; then
			openssl pkeyutl -sign -inkey "$RELEASE_KEY" -rawin \
				-in "$name.tar.gz" -out "$name.tar.gz.sig"
		fi
	)
	sha=$(cut -d' ' -f1 "$dir/$name.tar.gz.sha256")
	entries="$entries
    \"$os-$arch\": { \"url\": \"releases/$VERSION/$os/$arch/$name.tar.gz\", \"sha256\": \"$sha\", \"size\": $(wc -c < "$dir/$name.tar.gz" | tr -d ' ') },"
done

cat > "$OUT/releases/$VERSION/manifest.json" <<EOF
{
  "version": "$VERSION",
  "releasedAt": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "protocol": 1,
  "minimumVersion": "0.1.0",
  "artifacts": {${entries%,}
  }
}
EOF

cat > "$OUT/channels/stable.json" <<EOF
{
  "channel": "stable",
  "version": "$VERSION",
  "rolloutPercent": 100,
  "minimumVersion": "0.1.0",
  "manifestUrl": "releases/$VERSION/manifest.json"
}
EOF

echo
echo "artifacts in $OUT"
find "$OUT" -type f | sort
