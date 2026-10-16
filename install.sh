#!/bin/sh
# Install the rlp standalone binary on Linux or macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/rdlucas2/rlp/main/install.sh | sh
#
# Environment:
#   RLP_VERSION      release tag to install, e.g. v0.1.0 (default: latest)
#   RLP_INSTALL_DIR  where to put the binary (default: $HOME/.local/bin)
#   RLP_DOWNLOAD_BASE  base URL of the release assets (default: GitHub Releases)
set -eu

repo="rdlucas2/rlp"
version="${RLP_VERSION:-latest}"
install_dir="${RLP_INSTALL_DIR:-$HOME/.local/bin}"

if [ -n "${RLP_DOWNLOAD_BASE:-}" ]; then
  base="$RLP_DOWNLOAD_BASE"
elif [ "$version" = "latest" ]; then
  base="https://github.com/$repo/releases/latest/download"
else
  base="https://github.com/$repo/releases/download/$version"
fi

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=macos ;;
  *) echo "rlp: unsupported OS $(uname -s); on Windows use install.ps1" >&2; exit 1 ;;
esac
case "$(uname -m)" in
  x86_64 | amd64) arch=x86_64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *) echo "rlp: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac
if [ "$os" = macos ] && [ "$arch" = x86_64 ]; then
  echo "rlp: no Intel macOS binary yet; install the Python package instead: pipx install rlp-cli" >&2
  exit 1
fi
asset="rlp-$os-$arch"

if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -q "$1" -O "$2"; }
else
  echo "rlp: curl or wget is required" >&2
  exit 1
fi
if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$1" | cut -d ' ' -f 1; }
else
  sha256() { shasum -a 256 "$1" | cut -d ' ' -f 1; }
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "rlp: downloading $asset ($version)"
fetch "$base/$asset" "$tmp/$asset"
fetch "$base/SHA256SUMS" "$tmp/SHA256SUMS"

want="$(awk -v a="$asset" '$2 == a || $2 == "*"a { print $1 }' "$tmp/SHA256SUMS")"
got="$(sha256 "$tmp/$asset")"
if [ -z "$want" ] || [ "$want" != "$got" ]; then
  echo "rlp: checksum mismatch for $asset (want ${want:-none}, got $got)" >&2
  exit 1
fi

mkdir -p "$install_dir"
chmod +x "$tmp/$asset"
mv "$tmp/$asset" "$install_dir/rlp"
echo "rlp: installed $("$install_dir/rlp" --version) to $install_dir/rlp"

case ":$PATH:" in
  *":$install_dir:"*) ;;
  *) echo "rlp: add $install_dir to your PATH, for example: export PATH=\"$install_dir:\$PATH\"" ;;
esac
if [ "$os" = macos ]; then
  echo "rlp: the binary is not notarized yet; if macOS blocks it, run: xattr -d com.apple.quarantine $install_dir/rlp"
fi
