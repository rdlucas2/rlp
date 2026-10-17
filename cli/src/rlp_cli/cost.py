"""Cost estimates for a Terraform plan with Infracost.

The cost stage runs the pinned Infracost on the plan JSON the terraform stage
wrote, and records the estimated monthly total, what the deployed stack costs
now, and the change. When rlp.yaml sets quality.cost.monthlyBudget (USD), the
stage fails if the estimate goes over it.

Infracost prices resources through its hosted pricing API, which needs a free
API key in INFRACOST_API_KEY. Without one the stage is skipped, unless a budget
is declared: a declared budget is never left silently unchecked.

Estimates cover what Terraform declares (task size and count, log retention,
the target group...). Usage-based charges, such as data transfer or log volume,
are not included.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from rlp_cli import tools


class CostError(Exception):
    """Infracost could not run or returned something unexpected."""


def budget(doc: dict) -> float | None:
    """quality.cost.monthlyBudget from rlp.yaml, in USD, if declared."""
    value = ((doc.get("quality") or {}).get("cost") or {}).get("monthlyBudget")
    return float(value) if value is not None else None


def api_key() -> str:
    return os.environ.get("INFRACOST_API_KEY", "")


def _money(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Estimate:
    currency: str
    monthly: float
    past: float | None = None  # what the deployed stack costs now; None when Infracost gives no baseline
    resources: list[dict] = field(default_factory=list)  # most expensive first: name, monthly
    unsupported: int = 0  # resources Infracost found but can't price
    free: int = 0  # resources Infracost knows to be free
    detected: int = 0  # resources Infracost found in the plan

    @property
    def change(self) -> float | None:
        return None if self.past is None else self.monthly - self.past

    def text(self) -> str:
        parts = [f"{fmt(self.monthly, self.currency)}/month"]
        if self.change is not None:
            sign = "+" if self.change >= 0 else "-"
            parts.append(f"({sign}{fmt(abs(self.change), self.currency)} vs. now)")
        if self.resources:
            top = ", ".join(f"{r['name']} {fmt(r['monthly'], self.currency)}" for r in self.resources[:3])
            parts.append(f"top: {top}")
        if self.unsupported:
            parts.append(f"{self.unsupported} resource(s) not priced")
        return " ".join(parts)

    def as_dict(self) -> dict:
        return {
            "currency": self.currency,
            "monthly": round(self.monthly, 2),
            "past": None if self.past is None else round(self.past, 2),
            "change": None if self.change is None else round(self.change, 2),
            "resources": self.resources,
            "unsupported": self.unsupported,
            "free": self.free,
            "detected": self.detected,
        }


def fmt(amount: float, currency: str) -> str:
    return f"${amount:,.2f}" if currency == "USD" else f"{amount:,.2f} {currency}"


def parse(report: dict) -> Estimate:
    """Infracost's JSON output (--format json) as an Estimate."""
    if not isinstance(report, dict) or "projects" not in report:
        raise CostError("Infracost output has no projects")
    monthly = _money(report.get("totalMonthlyCost")) or 0.0
    past = _money(report.get("pastTotalMonthlyCost"))
    resources = []
    for project in report.get("projects") or []:
        for r in (project.get("breakdown") or {}).get("resources") or []:
            cost = _money(r.get("monthlyCost"))
            if cost:
                resources.append({"name": r.get("name", "?"), "monthly": round(cost, 2)})
    resources.sort(key=lambda r: (-r["monthly"], r["name"]))
    summary = report.get("summary") or {}
    return Estimate(
        currency=report.get("currency") or "USD",
        monthly=monthly,
        past=past,
        resources=resources,
        unsupported=int(summary.get("totalUnsupportedResources") or 0),
        free=int(summary.get("totalNoPriceResources") or 0),
        detected=int(summary.get("totalDetectedResources") or 0),
    )


def run(plan_json: Path, out: Path) -> dict:
    """Run Infracost on a plan JSON file, write its JSON report to OUT, and return it."""
    try:
        exe = tools.ensure_infracost()
    except tools.ToolError as err:
        raise CostError(str(err)) from err
    out.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "INFRACOST_SKIP_UPDATE_CHECK": "true", "INFRACOST_NO_COLOR": "true"}
    args = [str(exe), "breakdown", "--path", str(plan_json), "--format", "json", "--out-file", str(out), "--log-level", "warn"]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", env=env, check=False)
    if proc.returncode != 0:
        raise CostError(f"infracost exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-1500:]}")
    # Infracost exits 0 even when it can't reach its pricing API, and then prices everything at $0.
    if "pricing.api.infracost.io/graphql" in (proc.stderr or "") and "giving up" in proc.stderr:
        raise CostError("infracost could not reach its pricing API (pricing.api.infracost.io), so the estimate would be $0")
    try:
        return json.loads(out.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise CostError(f"infracost wrote no readable report at {out}: {err}") from err
