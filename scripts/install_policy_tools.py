"""Install the pinned OPA and conftest binaries into .tools/bin.

Used by `task test`. The pins and the checksum-verified download live in the
CLI (cli/src/rlp_cli/tools.py), so there is one source of truth; this script
only needs the standard library, so it runs before any dependency is installed.

Usage: python scripts/install_policy_tools.py [DEST]   (default: .tools/bin)
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli" / "src"))
    tools = importlib.import_module("rlp_cli.tools")
    sys.exit(tools.main([sys.argv[0], sys.argv[1] if len(sys.argv) > 1 else str(Path(".tools") / "bin")]))
