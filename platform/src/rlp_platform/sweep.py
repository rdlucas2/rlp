"""The retirement sweep: flag deprecated projects that served no traffic for a while.

For each `deprecated` project the sweep finds one of:

- idle: it has AWS deployments, and they served no requests in the window.
  The sweep records a flag on the project and opens (or updates) one issue,
  "Retire <project>?", in the project's first repo, with the evidence and the
  `rlp lifecycle retire` command. It never retires anything itself.
- active: any request in the window. A flag the project had is cleared.
- unknown: it can't be judged: no AWS deployment on record, no traffic source,
  or the traffic source failed. Never flagged.

Traffic comes from a TrafficSource. CloudWatchTraffic sums the load balancer's
RequestCount for the target groups tagged `rlp:project` (docs/aws.md), read
through a read-only role (RLP_SWEEP_ROLE_ARN). Slice 4 can add an OTEL-based
source behind the same interface.

The sweep is a loop in the PLAN.md sense: RLP_LOOPS=off turns it off, and a
run that isn't a dry run is recorded with every project's result
(`sweep_runs`). Like the store, nothing here commits. See docs/lifecycle.md.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy.orm import Session

from rlp_platform.config import Settings
from rlp_platform.github import GitHub, GitHubError
from rlp_platform.models import Phase, Project
from rlp_platform.store import deployments, projects, repos, sweeps

IDLE, ACTIVE, UNKNOWN = "idle", "active", "unknown"
# Keep in step with retirement_idle_days in policy/platform/data.yaml (cli/tests/test_sweep.py checks).
DEFAULT_IDLE_DAYS = 30
# A component whose latest deployment in an environment has this status runs nowhere there.
DESTROYED = "destroyed"
PROJECT_TAG, ENVIRONMENT_TAG = "rlp:project", "rlp:environment"
TARGET_GROUP_TYPE = "elasticloadbalancing:targetgroup"
SESSION_NAME = "rlp-retirement-sweep"
# GetMetricData takes at most 500 queries per call; DescribeTargetGroups up to 400 ARNs. Stay well under both.
QUERY_BATCH = 100
ARN_BATCH = 20


class TrafficError(RuntimeError):
    """The traffic source could not answer; the project is reported unknown."""


class TrafficSource(Protocol):
    def requests(self, project: str, environments: Sequence[str], start: datetime, end: datetime) -> int | None:
        """Requests the project's services in `environments` served in [start, end); None when it has none to count."""
        ...


def issue_title(project: str) -> str:
    return f"Retire {project}?"


@dataclass(frozen=True)
class Result:
    project: str
    result: str  # idle, active or unknown
    detail: str
    environments: list[str] = field(default_factory=list)
    requests: int | None = None
    issue_url: str | None = None
    # Why the issue could not be opened or updated; the flag is recorded anyway.
    issue_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "result": self.result,
            "detail": self.detail,
            "environments": list(self.environments),
            "requests": self.requests,
            "issue_url": self.issue_url,
            "issue_error": self.issue_error,
        }


@dataclass(frozen=True)
class Run:
    """What one sweep came to. `disabled` names why it did nothing; `run_id` is the recorded run's id."""

    idle_days: int
    window_start: datetime
    window_end: datetime
    dry_run: bool = False
    disabled: str | None = None
    results: list[Result] = field(default_factory=list)
    run_id: int | None = None

    @property
    def ok(self) -> bool:
        """False when an idle project's issue could not be opened or updated."""
        return all(r.issue_error is None for r in self.results)


def deployed_environments(session: Session, project: Project) -> list[str]:
    """Environments where a component of the project is deployed: its latest deployment there isn't `destroyed`."""
    latest: dict[tuple[int, str], str] = {}
    for d in deployments.list_(session, project_id=project.id):  # newest first
        latest.setdefault((d.component_id, d.environment), d.status)
    return sorted({env for (_, env), status in latest.items() if status != DESTROYED})


def judge(
    session: Session, project: Project, traffic: TrafficSource | None, start: datetime, end: datetime, why_none: str
) -> Result:
    """idle, active or unknown for one project, from its deployments and the traffic source."""
    envs = deployed_environments(session, project)
    if not envs:
        return Result(project.name, UNKNOWN, "no AWS deployment on record, so its traffic can't be judged")
    if traffic is None:
        return Result(project.name, UNKNOWN, why_none, envs)
    try:
        count = traffic.requests(project.name, envs, start, end)
    except TrafficError as err:
        return Result(project.name, UNKNOWN, f"could not read traffic: {err}", envs)
    if count is None:
        return Result(
            project.name,
            UNKNOWN,
            f"no load balancer target group tagged {PROJECT_TAG}={project.name} in {', '.join(envs)}",
            envs,
        )
    days = (end - start).days
    if count > 0:
        return Result(project.name, ACTIVE, f"{count} request(s) in the last {days} days", envs, count)
    return Result(project.name, IDLE, f"no requests in the last {days} days", envs, 0)


def issue_body(project: Project, result: Result, start: datetime, end: datetime, first_flagged: datetime) -> str:
    stamp = "%Y-%m-%d %H:%M UTC"
    return f"""The retirement sweep found no traffic for **{project.name}**, which is `deprecated`.

| | |
| --- | --- |
| Window | {start.strftime(stamp)} to {end.strftime(stamp)} |
| Environments | {", ".join(result.environments)} |
| Requests | 0 (load balancer `RequestCount` of the target groups tagged `{PROJECT_TAG}={project.name}`) |
| First flagged | {first_flagged.strftime(stamp)} |
| Last checked | {end.strftime(stamp)} |

If it can go, retire it: tear down every environment, archive its repos and expire its artifacts.

```sh
rlp lifecycle retire {project.name}          # show the plan
rlp lifecycle retire {project.name} --yes    # run it
```

If it is still needed, move it back to live: `rlp lifecycle move {project.name} live --reason "<why>"`.
Closing this issue doesn't stop the sweep from flagging it again while it stays deprecated and idle.

The sweep only flags; a person starts the retirement. See docs/lifecycle.md in rdlucas2/rlp.
"""


def _file_issue(gh: GitHub, flag: Any, repo: str, title: str, body: str) -> tuple[str, dict[str, Any]]:
    """(repo, issue): the issue the flag points at, else the open one with this title, else a new one; updated."""
    if flag.issue_number is not None and flag.issue_repo:
        updated = gh.update_issue(flag.issue_repo, flag.issue_number, body)
        if updated is not None:
            return flag.issue_repo, updated
    found = gh.find_issue(repo, title)
    if found is not None:
        return repo, gh.update_issue(repo, int(found["number"]), body) or found
    return repo, gh.create_issue(repo, title, body)


def flag(session: Session, gh: GitHub, project: Project, result: Result, start: datetime, end: datetime) -> Result:
    """Record the idle project's flag and open or update its issue. Returns the result with the issue's URL."""
    record = sweeps.flag_for(session, project.id)
    if record is None:
        record = sweeps.create_flag(session, project_id=project.id, flagged_at=end, detail=result.detail)
    elif record.cleared_at is not None:
        sweeps.update_flag(session, record, flagged_at=end, cleared_at=None, detail=result.detail)
    else:
        sweeps.update_flag(session, record, detail=result.detail)
    linked = sorted(repos.list_(session, project_id=project.id), key=lambda r: r.full_name)
    if not linked:
        return _with(result, issue_error="the project has no linked repo to open the issue in")
    body = issue_body(project, result, start, end, record.flagged_at)
    try:
        repo, issue = _file_issue(gh, record, linked[0].full_name, issue_title(project.name), body)
    except GitHubError as err:
        return _with(result, issue_error=str(err))
    url = issue.get("html_url")
    sweeps.update_flag(session, record, issue_repo=repo, issue_number=int(issue["number"]), issue_url=url)
    return _with(result, issue_url=url)


def _with(result: Result, **changes: Any) -> Result:
    return Result(**{**result.as_dict(), **changes})


def _clear(session: Session, project: Project, at: datetime) -> None:
    record = sweeps.flag_for(session, project.id)
    if record is not None and record.cleared_at is None:
        sweeps.update_flag(session, record, cleared_at=at)


def run(
    session: Session,
    gh: GitHub,
    traffic: TrafficSource | None,
    *,
    idle_days: int = DEFAULT_IDLE_DAYS,
    actor: str = "sweep",
    dry_run: bool = False,
    loops_off: str | None = None,
    why_no_traffic: str = "no traffic source is configured (RLP_SWEEP_ROLE_ARN)",
    now: datetime | None = None,
    commit: Callable[[], None] | None = None,
) -> Run:
    """Sweep every deprecated project. A dry run, or one `loops_off` (RLP_LOOPS) turns off, changes nothing."""
    if idle_days < 1:
        raise ValueError("idle_days must be at least 1")
    end = now or datetime.now(UTC)
    start = end - timedelta(days=idle_days)
    if loops_off is not None:
        reason = f"RLP_LOOPS={loops_off} turns every loop off, the retirement sweep included"
        return Run(idle_days, start, end, dry_run, disabled=reason)
    results: list[Result] = []
    deprecated = projects.list_(session, phase=Phase.DEPRECATED)
    for project in deprecated:
        result = judge(session, project, traffic, start, end, why_no_traffic)
        if not dry_run:
            if result.result == IDLE:
                result = flag(session, gh, project, result, start, end)
            elif result.result == ACTIVE:
                _clear(session, project, end)
            if commit is not None:
                commit()  # an issue opened stays recorded, even if a later project fails
        results.append(result)
    if dry_run:
        return Run(idle_days, start, end, True, results=results)
    # A flag whose project left deprecated (moved back to live, or retired) is cleared.
    for record in sweeps.open_flags(session):
        if Phase(projects.get(session, record.project_id).phase) != Phase.DEPRECATED:
            sweeps.update_flag(session, record, cleared_at=end)
    recorded = sweeps.record_run(
        session,
        actor=actor,
        idle_days=idle_days,
        window_start=start,
        window_end=end,
        results=[r.as_dict() for r in results],
        finished_at=datetime.now(UTC),
    )
    return Run(idle_days, start, end, False, results=results, run_id=recorded.id)


# --- CloudWatch ---


def _dimension(arn: str, resource: str) -> str:
    """The metric dimension value of a load balancer (app/NAME/ID) or a target group (targetgroup/NAME/ID)."""
    _, sep, rest = arn.partition(f":{resource}/")
    if not sep or not rest:
        raise TrafficError(f"{arn!r} is not a {resource} ARN")
    return rest if resource == "loadbalancer" else f"{resource}/{rest}"


class CloudWatchTraffic:
    """Sums AWS/ApplicationELB RequestCount for a project's target groups, through a read-only role.

    The role needs tag:GetResources, elasticloadbalancing:DescribeTargetGroups
    and cloudwatch:GetMetricData (docs/lifecycle.md#retirement-sweep). `clients`
    returns a boto3 client by service name; by default it assumes `role_arn`.
    """

    def __init__(
        self, role_arn: str, *, region: str | None = None, clients: Callable[[str], Any] | None = None
    ) -> None:
        self.role_arn = role_arn
        self.region = region
        self._clients = clients
        self._cache: dict[str, Any] = {}

    def _client(self, service: str) -> Any:
        if self._clients is not None:
            return self._clients(service)
        if not self._cache:
            import boto3  # only the sweep needs AWS

            base = boto3.Session(region_name=self.region)
            creds = base.client("sts").assume_role(RoleArn=self.role_arn, RoleSessionName=SESSION_NAME)["Credentials"]
            assumed = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=base.region_name,
            )
            for name in ("resourcegroupstaggingapi", "elbv2", "cloudwatch"):
                self._cache[name] = assumed.client(name)
        return self._cache[service]

    def requests(self, project: str, environments: Sequence[str], start: datetime, end: datetime) -> int | None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return self._requests(project, environments, start, end)
        except (BotoCoreError, ClientError) as err:
            raise TrafficError(str(err)) from err

    def _target_groups(self, project: str, environments: Sequence[str]) -> list[str]:
        tagging = self._client("resourcegroupstaggingapi")
        arns = []
        pages = tagging.get_paginator("get_resources").paginate(
            TagFilters=[
                {"Key": PROJECT_TAG, "Values": [project]},
                {"Key": ENVIRONMENT_TAG, "Values": list(environments)},
            ],
            ResourceTypeFilters=[TARGET_GROUP_TYPE],
        )
        for page in pages:
            arns.extend(r["ResourceARN"] for r in page.get("ResourceTagMappingList", []))
        return sorted(set(arns))

    def _requests(self, project: str, environments: Sequence[str], start: datetime, end: datetime) -> int | None:
        arns = self._target_groups(project, environments)
        if not arns:
            return None
        elbv2 = self._client("elbv2")
        dimensions = []
        for i in range(0, len(arns), ARN_BATCH):
            for tg in elbv2.describe_target_groups(TargetGroupArns=arns[i : i + ARN_BATCH])["TargetGroups"]:
                # A target group no load balancer uses served nothing.
                for lb in tg.get("LoadBalancerArns", []):
                    dimensions.append(
                        [
                            {"Name": "TargetGroup", "Value": _dimension(tg["TargetGroupArn"], "targetgroup")},
                            {"Name": "LoadBalancer", "Value": _dimension(lb, "loadbalancer")},
                        ]
                    )
        total = 0.0
        cloudwatch = self._client("cloudwatch")
        period = 86400
        for i in range(0, len(dimensions), QUERY_BATCH):
            queries = [
                {
                    "Id": f"q{n}",
                    "MetricStat": {
                        "Metric": {"Namespace": "AWS/ApplicationELB", "MetricName": "RequestCount", "Dimensions": dims},
                        "Period": period,
                        "Stat": "Sum",
                    },
                    "ReturnData": True,
                }
                for n, dims in enumerate(dimensions[i : i + QUERY_BATCH])
            ]
            token = None
            while True:
                kwargs: dict[str, Any] = {"MetricDataQueries": queries, "StartTime": start, "EndTime": end}
                if token:
                    kwargs["NextToken"] = token
                answer = cloudwatch.get_metric_data(**kwargs)
                for series in answer.get("MetricDataResults", []):
                    total += sum(series.get("Values", []))
                token = answer.get("NextToken")
                if not token:
                    break
        return int(total)


def traffic_from_settings(settings: Settings) -> tuple[TrafficSource | None, str]:
    """The traffic source the settings configure, and why there is none when there isn't."""
    role = settings.sweep_role_arn
    if not role:
        return None, "no traffic source: RLP_SWEEP_ROLE_ARN is not set"
    if not role.startswith("arn:") or ":role/" not in role:
        return None, f"no traffic source: RLP_SWEEP_ROLE_ARN is not an IAM role ARN ({role!r})"
    return CloudWatchTraffic(role), ""
