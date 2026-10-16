#!/usr/bin/env bash
# Installs the rlp toolchain into the devcontainer at pinned versions.
# Bump a version here, rebuild the container, and commit the change in a PR.
set -euo pipefail

OPA_VERSION=1.21.2
CONFTEST_VERSION=0.71.1
TRIVY_VERSION=0.76.0
GITLEAKS_VERSION=8.30.1
ACT_VERSION=0.2.89
TASK_VERSION=3.54.0
SEMGREP_VERSION=1.179.0

BIN=/usr/local/bin
GH=https://github.com

case "$(uname -m)" in
  x86_64|amd64)
    opa_arch=amd64; conftest_arch=x86_64; trivy_arch=64bit
    gitleaks_arch=x64; act_arch=x86_64; task_arch=amd64 ;;
  aarch64|arm64)
    opa_arch=arm64; conftest_arch=arm64; trivy_arch=ARM64
    gitleaks_arch=arm64; act_arch=arm64; task_arch=arm64 ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# untar <url> <binary-name>: download a release tarball and install one binary from it
untar() {
  curl -fsSL "$1" -o "$tmp/pkg.tgz"
  tar -xzf "$tmp/pkg.tgz" -C "$tmp" "$2"
  sudo install -m 0755 "$tmp/$2" "$BIN/$2"
}

curl -fsSL "$GH/open-policy-agent/opa/releases/download/v${OPA_VERSION}/opa_linux_${opa_arch}_static" -o "$tmp/opa"
sudo install -m 0755 "$tmp/opa" "$BIN/opa"
untar "$GH/open-policy-agent/conftest/releases/download/v${CONFTEST_VERSION}/conftest_${CONFTEST_VERSION}_Linux_${conftest_arch}.tar.gz" conftest
untar "$GH/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-${trivy_arch}.tar.gz" trivy
untar "$GH/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${gitleaks_arch}.tar.gz" gitleaks
untar "$GH/nektos/act/releases/download/v${ACT_VERSION}/act_Linux_${act_arch}.tar.gz" act
untar "$GH/go-task/task/releases/download/v${TASK_VERSION}/task_linux_${task_arch}.tar.gz" task

# Python tooling via uv
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv tool install pre-commit
uv tool install copier
uv tool install "semgrep==${SEMGREP_VERSION}"

for tool in opa conftest trivy gitleaks act task uv pre-commit copier semgrep terraform gh docker; do
  printf '%-11s' "$tool"; command -v "$tool" >/dev/null && echo ok || { echo MISSING; exit 1; }
done
