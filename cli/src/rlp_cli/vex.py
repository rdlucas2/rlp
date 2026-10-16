"""Vulnerability exceptions from .rlp/vex.yaml.

Each exception names a CVE (`id`), why it is acceptable (`reason`) and when the
exception lapses (`expires`, YYYY-MM-DD). `component` is optional: when set,
the exception covers only that package name; otherwise every package with the
CVE. The trivy gate honours unexpired exceptions only. An expired one stops
covering its CVE, and the policy stage fails on it (RLP-L005), so it has to be
renewed or removed.

    exceptions:
      - id: CVE-2026-0001
        component: urllib3
        reason: The vulnerable function is never called.
        expires: "2026-12-31"
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import yaml

VEX_FILE = Path(".rlp") / "vex.yaml"


class VexError(Exception):
    """.rlp/vex.yaml cannot be read."""


@dataclass(frozen=True)
class Entry:
    id: str
    reason: str
    expires: dt.date | None
    component: str | None = None

    def active(self, today: dt.date) -> bool:
        return self.expires is not None and self.expires >= today

    def covers(self, vuln_id: str, package: str) -> bool:
        return self.id == vuln_id and (self.component is None or self.component == package)


def _date(value) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def load(root: Path) -> list[Entry]:
    path = root / VEX_FILE
    if not path.is_file():
        return []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as err:
        raise VexError(f"{VEX_FILE.as_posix()} is not valid YAML: {err}") from err
    entries = doc.get("exceptions") if isinstance(doc, dict) else None
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise VexError(f"{VEX_FILE.as_posix()}: exceptions must be a list")
    out = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        component = e.get("component")
        out.append(Entry(str(e["id"]), str(e.get("reason") or ""), _date(e.get("expires")), str(component) if component else None))
    return out


def active(exceptions: list[Entry], today: dt.date | None = None) -> list[Entry]:
    today = today or dt.date.today()
    return [e for e in exceptions if e.active(today)]
