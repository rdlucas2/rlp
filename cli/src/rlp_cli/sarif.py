"""Small helpers for the SARIF reports the scanners write.

Code scanning rejects two runs of the same tool under the same category, and
the trivy stage writes one Trivy run per scan target. Tagging every run with
its own automationDetails.id gives each report its own category.
"""

from __future__ import annotations

import json
from pathlib import Path


def tag(path: Path, category: str) -> None:
    """Set runs[].automationDetails.id to "rlp/<category>/" in the SARIF file at path."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    for run in doc.get("runs") or []:
        run["automationDetails"] = {"id": f"rlp/{category}/"}
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
