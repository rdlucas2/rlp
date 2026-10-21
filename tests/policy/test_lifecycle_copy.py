"""The platform image runs the lifecycle rules on its own copy of them, with its own pinned OPA (#91).

The image is built from platform/, which can't reach policy/ at the repo root,
so the platform package ships policy/lifecycle/lifecycle.rego in
platform/src/rlp_platform/policy/lifecycle/ and pins OPA in
platform/src/rlp_platform/opa.py. These tests keep both in step with the
bundle and with the CLI's pins.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from rlp_cli import tools

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "policy" / "lifecycle"
PLATFORM = ROOT / "platform"
SHIPPED = PLATFORM / "src" / "rlp_platform" / "policy" / "lifecycle"


def platform_opa():
    spec = importlib.util.spec_from_file_location("rlp_platform_opa", PLATFORM / "src" / "rlp_platform" / "opa.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_platform_ships_the_bundles_lifecycle_rules() -> None:
    rules = sorted(p.name for p in BUNDLE.glob("*.rego") if not p.name.endswith("_test.rego"))
    assert sorted(p.name for p in SHIPPED.iterdir()) == rules == ["lifecycle.rego"]
    bundle = (BUNDLE / "lifecycle.rego").read_bytes()
    assert (SHIPPED / "lifecycle.rego").read_bytes() == bundle, (
        "platform/src/rlp_platform/policy/lifecycle/lifecycle.rego differs from policy/lifecycle/lifecycle.rego; "
        "copy the bundle's file over it"
    )


def test_platform_pins_opa_where_the_cli_does() -> None:
    opa = platform_opa()
    assert opa.OPA_VERSION == tools.OPA_VERSION
    assert opa.ASSETS == tools.OPA


def test_platform_image_installs_opa_where_lifecycle_runs_it() -> None:
    dockerfile = (PLATFORM / "Dockerfile").read_text(encoding="utf-8")
    # The image builds the pinned version from source (patched Go), so it runs the same OPA as the CLI.
    assert f"ARG OPA_VERSION={platform_opa().OPA_VERSION}\n" in dockerfile
    assert 'go install -ldflags="-s -w" "github.com/open-policy-agent/opa@v${OPA_VERSION}"' in dockerfile
    assert "COPY --from=opa /go/bin/opa /usr/local/bin/opa" in dockerfile
    lifecycle = (PLATFORM / "src" / "rlp_platform" / "lifecycle.py").read_text(encoding="utf-8")
    assert '"/usr/local/bin/opa"' in lifecycle
