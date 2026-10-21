"""The retirement sweep: idle, active and unknown projects, issues, the kill switch, and the CloudWatch source."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from rlp_platform import __main__ as entrypoint
from rlp_platform import sweep
from rlp_platform.app import create_app
from rlp_platform.config import Settings
from rlp_platform.github import GitHub
from rlp_platform.models import Phase
from rlp_platform.store import artifacts, components, deployments, projects, repos, sweeps

API_KEY = "test-only"
AUTH = {"Authorization": f"Bearer {API_KEY}"}
NOW = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)
LONG_AGO = datetime(2026, 8, 1, tzinfo=UTC)


class FakeIssues:
    """GitHub's issue endpoints, in memory. Repos in `refuse` answer 403."""

    def __init__(self) -> None:
        self.issues: dict[str, list[dict]] = {}
        self.refuse: set[str] = set()
        self.created = 0
        self.updated = 0

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> GitHub:
        return GitHub("https://api.github.test", "test-token", transport=self.transport)

    def add(self, repo: str, title: str, *, pull_request: bool = False, state: str = "open") -> dict:
        issues = self.issues.setdefault(repo, [])
        issue = {
            "number": len(issues) + 1,
            "title": title,
            "body": "",
            "state": state,
            "html_url": f"https://github.test/{repo}/issues/{len(issues) + 1}",
        }
        if pull_request:
            issue["pull_request"] = {}
        issues.append(issue)
        return issue

    def handle(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        assert parts[0] == "repos" and parts[3] == "issues", request.url
        repo = f"{parts[1]}/{parts[2]}"
        if repo in self.refuse:
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        issues = self.issues.setdefault(repo, [])
        if request.method == "GET":
            page, per_page = int(request.url.params["page"]), int(request.url.params["per_page"])
            open_ = [i for i in issues if i["state"] == "open"]
            return httpx.Response(200, json=open_[(page - 1) * per_page : page * per_page])
        body = json.loads(request.content)
        if request.method == "POST":
            self.created += 1
            issue = self.add(repo, body["title"])
            issue["body"] = body["body"]
            return httpx.Response(201, json=issue)
        number = int(parts[4])
        for issue in issues:
            if issue["number"] == number:
                self.updated += 1
                issue["body"] = body["body"]
                return httpx.Response(200, json=issue)
        return httpx.Response(404, json={"message": "Not Found"})

    def titled(self, repo: str, title: str) -> list[dict]:
        return [i for i in self.issues.get(repo, []) if i["title"] == title]


class FakeTraffic:
    """Requests per project; a project missing from `counts` has no target groups (None)."""

    def __init__(self, counts: dict[str, int] | None = None, fail: bool = False) -> None:
        self.counts = counts or {}
        self.fail = fail
        self.calls: list[tuple[str, list[str], datetime, datetime]] = []

    def requests(self, project, environments, start, end):
        self.calls.append((project, list(environments), start, end))
        if self.fail:
            raise sweep.TrafficError("AccessDenied")
        return self.counts.get(project)


def deploy(s: Session, project: str, repo: str, env: str = "dev", status: str = "succeeded", at=LONG_AGO) -> None:
    record = projects.get_by_name(s, project)
    try:
        r = repos.get_by_full_name(s, repo)
    except LookupError:
        r = repos.create(s, full_name=repo, project_id=record.id)
    found = [c for c in components.list_(s, repo_id=r.id, include_removed=False) if c.name == "api"]
    c = (
        found[0]
        if found
        else components.create(
            s,
            repo_id=r.id,
            name="api",
            language="python",
            artifact_kind="container-image",
            runtime_kind="container-service",
        )
    )
    a = artifacts.create(
        s, component_id=c.id, kind="container-image", reference=f"ghcr.io/acme/{project}:{status}{env}"
    )
    deployments.create(s, component_id=c.id, environment=env, artifact_id=a.id, status=status, deployed_at=at)


def seed(s: Session) -> None:
    """orders (deprecated, idle candidate, two repos), web (deprecated, busy), lab (deprecated, nothing deployed),
    billing (live, deployed)."""
    for name, phase in (("orders", Phase.DEPRECATED), ("web", Phase.DEPRECATED), ("lab", Phase.DEPRECATED)):
        projects.create(s, name=name, owner="rdlucas2", phase=phase)
    projects.create(s, name="billing", owner="rdlucas2", phase=Phase.LIVE)
    deploy(s, "orders", "acme/orders-web", "dev")
    deploy(s, "orders", "acme/orders-api", "prod")
    deploy(s, "web", "acme/web", "dev")
    repos.create(s, full_name="acme/lab", project_id=projects.get_by_name(s, "lab").id)
    deploy(s, "billing", "acme/billing", "dev")
    s.flush()


@pytest.fixture
def seeded(session) -> Session:
    seed(session)
    return session


@pytest.fixture
def issues() -> FakeIssues:
    return FakeIssues()


def by_project(result: sweep.Run) -> dict[str, sweep.Result]:
    return {r.project: r for r in result.results}


TRAFFIC = {"orders": 0, "web": 12}


def test_an_idle_project_is_flagged_once_even_after_two_sweeps(seeded, issues):
    traffic = FakeTraffic(TRAFFIC)
    first = sweep.run(seeded, issues.client(), traffic, now=NOW)
    second = sweep.run(seeded, issues.client(), traffic, now=NOW + timedelta(days=1))
    for run in (first, second):
        orders = by_project(run)["orders"]
        assert orders.result == sweep.IDLE and orders.requests == 0
        assert orders.issue_url == "https://github.test/acme/orders-api/issues/1"
    # One issue, in the first repo by name, opened once and updated by the second sweep.
    assert len(issues.titled("acme/orders-api", "Retire orders?")) == 1
    assert issues.created == 1 and issues.updated == 1
    body = issues.titled("acme/orders-api", "Retire orders?")[0]["body"]
    assert "rlp lifecycle retire orders --yes" in body
    assert "dev, prod" in body and "2026-10-22 03:00 UTC" in body
    flag = sweeps.flag_for(seeded, projects.get_by_name(seeded, "orders").id)
    assert flag.flagged_at == NOW and flag.cleared_at is None and flag.issue_number == 1
    # The window is the last idle_days, for every environment the project is deployed in.
    assert traffic.calls[0] == ("orders", ["dev", "prod"], NOW - timedelta(days=30), NOW)


def test_an_active_project_and_a_project_with_no_deployment_are_not_flagged(seeded, issues):
    result = by_project(sweep.run(seeded, issues.client(), FakeTraffic(TRAFFIC), now=NOW))
    assert set(result) == {"orders", "web", "lab"}  # billing is live, so not swept
    assert result["web"].result == sweep.ACTIVE and result["web"].requests == 12
    assert result["lab"].result == sweep.UNKNOWN and "no AWS deployment" in result["lab"].detail
    for name in ("web", "lab"):
        assert sweeps.flag_for(seeded, projects.get_by_name(seeded, name).id) is None
    assert issues.titled("acme/web", "Retire web?") == [] and issues.titled("acme/lab", "Retire lab?") == []


def test_a_destroyed_deployment_does_not_count(seeded, issues):
    deploy(seeded, "web", "acme/web", "dev", status="destroyed", at=LONG_AGO + timedelta(days=1))
    result = by_project(sweep.run(seeded, issues.client(), FakeTraffic(TRAFFIC), now=NOW))
    assert result["web"].result == sweep.UNKNOWN


def test_no_target_groups_no_source_or_a_failing_source_is_unknown(seeded, issues):
    result = by_project(sweep.run(seeded, issues.client(), FakeTraffic({}), now=NOW))
    assert result["orders"].result == sweep.UNKNOWN and "no load balancer target group" in result["orders"].detail
    result = by_project(sweep.run(seeded, issues.client(), None, why_no_traffic="no source", now=NOW))
    assert result["orders"].result == sweep.UNKNOWN and result["orders"].detail == "no source"
    result = by_project(sweep.run(seeded, issues.client(), FakeTraffic(fail=True), now=NOW))
    assert result["orders"].result == sweep.UNKNOWN and "AccessDenied" in result["orders"].detail
    assert issues.created == 0


def test_the_kill_switch_reports_disabled_and_changes_nothing(seeded, issues):
    traffic = FakeTraffic(TRAFFIC)
    result = sweep.run(seeded, issues.client(), traffic, loops_off="off", now=NOW)
    assert result.disabled and "RLP_LOOPS=off" in result.disabled
    assert result.results == [] and result.run_id is None
    assert traffic.calls == [] and issues.created == 0
    assert sweeps.list_runs(seeded) == []


def test_a_dry_run_reports_and_changes_nothing(seeded, issues):
    result = sweep.run(seeded, issues.client(), FakeTraffic(TRAFFIC), dry_run=True, now=NOW)
    assert by_project(result)["orders"].result == sweep.IDLE
    assert result.run_id is None and issues.created == 0
    assert sweeps.flag_for(seeded, projects.get_by_name(seeded, "orders").id) is None
    assert sweeps.list_runs(seeded) == []


def test_each_run_is_recorded(seeded, issues):
    result = sweep.run(seeded, issues.client(), FakeTraffic(TRAFFIC), actor="nightly", idle_days=7, now=NOW)
    [run] = sweeps.list_runs(seeded)
    assert run.id == result.run_id and run.actor == "nightly" and run.idle_days == 7
    assert run.window_start == NOW - timedelta(days=7) and run.window_end == NOW
    assert {r["project"]: r["result"] for r in run.results} == {"orders": "idle", "web": "active", "lab": "unknown"}


def test_an_existing_open_issue_is_updated_not_duplicated(seeded, issues):
    issues.add("acme/orders-api", "Retire orders?", pull_request=True)  # a PR with the title doesn't count
    existing = issues.add("acme/orders-api", "Retire orders?")
    result = by_project(sweep.run(seeded, issues.client(), FakeTraffic(TRAFFIC), now=NOW))
    assert result["orders"].issue_url == existing["html_url"]
    assert issues.created == 0 and existing["body"]


def test_a_deleted_issue_is_opened_again(seeded, issues):
    sweep.run(seeded, issues.client(), FakeTraffic(TRAFFIC), now=NOW)
    issues.issues["acme/orders-api"].clear()
    sweep.run(seeded, issues.client(), FakeTraffic(TRAFFIC), now=NOW)
    assert issues.created == 2 and len(issues.titled("acme/orders-api", "Retire orders?")) == 1


def test_a_refused_issue_is_reported_and_the_flag_kept(seeded, issues):
    issues.refuse.add("acme/orders-api")
    result = sweep.run(seeded, issues.client(), FakeTraffic(TRAFFIC), now=NOW)
    assert not result.ok
    assert "403" in by_project(result)["orders"].issue_error
    assert sweeps.flag_for(seeded, projects.get_by_name(seeded, "orders").id) is not None


def test_activity_and_leaving_deprecated_clear_the_flag(seeded, issues):
    sweep.run(seeded, issues.client(), FakeTraffic(TRAFFIC), now=NOW)
    orders = projects.get_by_name(seeded, "orders")
    sweep.run(seeded, issues.client(), FakeTraffic({"orders": 3}), now=NOW + timedelta(days=1))
    flag = sweeps.flag_for(seeded, orders.id)
    assert flag.cleared_at == NOW + timedelta(days=1)
    # Idle again: flagged again, and the same issue is updated.
    sweep.run(seeded, issues.client(), FakeTraffic(TRAFFIC), now=NOW + timedelta(days=2))
    assert flag.cleared_at is None and flag.flagged_at == NOW + timedelta(days=2)
    assert issues.created == 1
    # Moved back to live: the next sweep doesn't judge it and clears its flag.
    orders.phase = Phase.LIVE
    seeded.flush()
    result = sweep.run(seeded, issues.client(), FakeTraffic(TRAFFIC), now=NOW + timedelta(days=3))
    assert "orders" not in by_project(result)
    assert flag.cleared_at == NOW + timedelta(days=3)


def test_idle_days_must_be_positive(session, issues):
    with pytest.raises(ValueError, match="at least 1"):
        sweep.run(session, issues.client(), None, idle_days=0)


# --- API ---


@pytest.fixture
def api(settings, migrated_db, issues):
    configured = dataclasses.replace(settings, database_url=migrated_db, api_token=API_KEY, github_token=API_KEY)
    app = create_app(configured)
    app.state.github_transport = issues.transport
    app.state.traffic_source = FakeTraffic(TRAFFIC)
    with TestClient(app, headers=AUTH) as client:
        yield client


@pytest.fixture
def api_record(migrated_db):
    from rlp_platform import db

    engine = db.make_engine(migrated_db)
    with Session(engine) as s:
        seed(s)
        s.commit()
    engine.dispose()


def test_api_sweep_and_runs(api, api_record, issues):
    resp = api.post("/api/v1/lifecycle/sweep", json={"actor": "nightly", "idle_days": 14})
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert doc["disabled"] is None and doc["idle_days"] == 14 and doc["run_id"] is not None
    results = {r["project"]: r for r in doc["results"]}
    assert results["orders"]["result"] == "idle" and results["orders"]["issue_url"]
    again = api.post("/api/v1/lifecycle/sweep", json={}).json()
    assert {r["project"]: r["result"] for r in again["results"]}["orders"] == "idle"
    assert issues.created == 1
    runs = api.get("/api/v1/lifecycle/sweeps", params={"limit": 1}).json()
    assert len(runs) == 1 and runs[0]["id"] == again["run_id"] and runs[0]["idle_days"] == 30


def test_api_dry_run_and_validation(api, api_record, issues):
    doc = api.post("/api/v1/lifecycle/sweep", json={"dry_run": True}).json()
    assert doc["dry_run"] is True and doc["run_id"] is None and issues.created == 0
    assert api.post("/api/v1/lifecycle/sweep", json={"idle_days": 0}).status_code == 422
    assert api.get("/api/v1/lifecycle/sweeps").json() == []


def test_api_kill_switch(settings, migrated_db, issues, api_record):
    configured = dataclasses.replace(
        settings, database_url=migrated_db, api_token=API_KEY, github_token=API_KEY, loops_off="off"
    )
    app = create_app(configured)
    app.state.github_transport = issues.transport
    with TestClient(app, headers=AUTH) as client:
        doc = client.post("/api/v1/lifecycle/sweep", json={}).json()
        assert doc["disabled"].startswith("RLP_LOOPS=off") and doc["results"] == []
        assert client.get("/api/v1/lifecycle/sweeps").json() == []
    assert issues.created == 0


def test_api_without_a_traffic_source_reports_unknown(settings, migrated_db, issues, api_record):
    configured = dataclasses.replace(settings, database_url=migrated_db, api_token=API_KEY)
    app = create_app(configured)
    app.state.github_transport = issues.transport
    with TestClient(app, headers=AUTH) as client:
        doc = client.post("/api/v1/lifecycle/sweep", json={}).json()
    assert {r["result"] for r in doc["results"]} == {"unknown"}
    assert any("RLP_SWEEP_ROLE_ARN is not set" in r["detail"] for r in doc["results"])


# --- python -m rlp_platform sweep ---


def test_sweep_command(migrated_db, api_record, issues, capsys):
    settings = Settings.from_env({"DATABASE_URL": migrated_db})
    code = entrypoint.sweep_command(
        settings, idle_days=30, dry_run=False, transport=issues.transport, traffic=FakeTraffic(TRAFFIC)
    )
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and doc["ok"] is True and doc["run_id"] is not None
    assert {r["project"]: r["result"] for r in doc["results"]}["orders"] == "idle"


def test_sweep_command_exits_1_when_an_issue_fails(migrated_db, api_record, issues, capsys):
    issues.refuse.add("acme/orders-api")
    settings = Settings.from_env({"DATABASE_URL": migrated_db})
    code = entrypoint.sweep_command(
        settings, idle_days=30, dry_run=False, transport=issues.transport, traffic=FakeTraffic(TRAFFIC)
    )
    assert code == 1


def test_sweep_command_kill_switch(migrated_db, api_record, issues, capsys):
    settings = Settings.from_env({"DATABASE_URL": migrated_db, "RLP_LOOPS": "OFF"})
    assert entrypoint.sweep_command(settings, idle_days=30, dry_run=False, transport=issues.transport) == 0
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert doc["disabled"].startswith("RLP_LOOPS=OFF") and doc["results"] == [] and issues.created == 0


def test_sweep_command_needs_a_database():
    assert entrypoint.sweep_command(Settings.from_env({}), idle_days=30, dry_run=False) == 2


def test_main_runs_sweep(monkeypatch):
    seen = {}
    monkeypatch.setattr(entrypoint, "sweep_command", lambda settings, **kw: seen.update(kw) or 0)
    assert entrypoint.main(["sweep", "--dry-run", "--idle-days", "7"]) == 0
    assert seen == {"idle_days": 7, "dry_run": True}
    with pytest.raises(SystemExit):
        entrypoint.main(["sweep", "--idle-days", "0"])


# --- settings ---


@pytest.mark.parametrize("value", ["off", "OFF", " false ", "0", "no", "disabled"])
def test_rlp_loops_off_values(value):
    assert Settings.from_env({"RLP_LOOPS": value}).loops_off == value


@pytest.mark.parametrize("value", ["", "on", "true", "yes"])
def test_rlp_loops_on_values(value):
    assert Settings.from_env({"RLP_LOOPS": value}).loops_off is None


def test_traffic_from_settings():
    source, why = sweep.traffic_from_settings(Settings.from_env({}))
    assert source is None and "not set" in why
    source, why = sweep.traffic_from_settings(Settings.from_env({"RLP_SWEEP_ROLE_ARN": "unset"}))
    assert source is None and "not an IAM role ARN" in why
    role = "arn:aws:iam::123456789012:role/rlp-sweep"
    source, why = sweep.traffic_from_settings(Settings.from_env({"RLP_SWEEP_ROLE_ARN": role}))
    assert isinstance(source, sweep.CloudWatchTraffic) and source.role_arn == role and why == ""


# --- CloudWatch ---

TG1 = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/dev-orders-api/aaa"
TG2 = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/prod-orders-api/bbb"
TG3 = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/dev-orders-old/ccc"
LB = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/rlp-dev/123"


class FakeAWS:
    """The tagging, elbv2 and cloudwatch calls CloudWatchTraffic makes."""

    def __init__(self, arns: list[str], values: list[list[float]], error: Exception | None = None) -> None:
        self.arns = arns
        self.values = values
        self.error = error
        self.tag_filters = None
        self.queries: list[dict] = []

    def __call__(self, service: str):
        return self

    # resourcegroupstaggingapi
    def get_paginator(self, name):
        assert name == "get_resources"
        return self

    def paginate(self, **kwargs):
        self.tag_filters = kwargs
        if self.error:
            raise self.error
        return [{"ResourceTagMappingList": [{"ResourceARN": a} for a in self.arns]}]

    # elbv2
    def describe_target_groups(self, TargetGroupArns):
        return {
            "TargetGroups": [
                {"TargetGroupArn": a, "LoadBalancerArns": [] if a == TG3 else [LB]} for a in TargetGroupArns
            ]
        }

    # cloudwatch
    def get_metric_data(self, **kwargs):
        self.queries.append(kwargs)
        if "NextToken" not in kwargs:
            first = [{"Id": q["Id"], "Values": self.values[0]} for q in kwargs["MetricDataQueries"]]
            return {"MetricDataResults": first, "NextToken": "page2"}
        return {"MetricDataResults": [{"Id": "q0", "Values": v} for v in self.values[1:]]}


def test_cloudwatch_sums_request_count_for_the_projects_target_groups():
    aws = FakeAWS([TG1, TG2, TG3], [[2.0, 3.0], [5.0]])
    source = sweep.CloudWatchTraffic("arn:aws:iam::123456789012:role/rlp-sweep", clients=aws)
    start, end = NOW - timedelta(days=30), NOW
    # Two target groups behind the load balancer, 5 each on the first page, then 5 more.
    assert source.requests("orders", ["dev", "prod"], start, end) == 15
    assert aws.tag_filters["TagFilters"] == [
        {"Key": "rlp:project", "Values": ["orders"]},
        {"Key": "rlp:environment", "Values": ["dev", "prod"]},
    ]
    assert aws.tag_filters["ResourceTypeFilters"] == ["elasticloadbalancing:targetgroup"]
    query = aws.queries[0]
    assert query["StartTime"] == start and query["EndTime"] == end
    dims = [q["MetricStat"]["Metric"]["Dimensions"] for q in query["MetricDataQueries"]]
    assert dims[0] == [
        {"Name": "TargetGroup", "Value": "targetgroup/dev-orders-api/aaa"},
        {"Name": "LoadBalancer", "Value": "app/rlp-dev/123"},
    ]
    assert len(dims) == 2  # the target group with no load balancer served nothing
    assert aws.queries[1]["NextToken"] == "page2"


def test_cloudwatch_zero_and_none():
    source = sweep.CloudWatchTraffic("arn:aws:iam::1:role/r", clients=FakeAWS([TG3], [[]]))
    assert source.requests("orders", ["dev"], NOW - timedelta(days=30), NOW) == 0
    source = sweep.CloudWatchTraffic("arn:aws:iam::1:role/r", clients=FakeAWS([], [[]]))
    assert source.requests("orders", ["dev"], NOW - timedelta(days=30), NOW) is None


def test_cloudwatch_errors_are_traffic_errors():
    denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetResources")
    source = sweep.CloudWatchTraffic("arn:aws:iam::1:role/r", clients=FakeAWS([], [[]], error=denied))
    with pytest.raises(sweep.TrafficError, match="AccessDenied"):
        source.requests("orders", ["dev"], NOW - timedelta(days=30), NOW)
    with pytest.raises(sweep.TrafficError, match="not a loadbalancer ARN"):
        sweep._dimension("arn:aws:elasticloadbalancing:us-east-1:1:targetgroup/x/y", "loadbalancer")


def test_cloudwatch_assumes_the_role(monkeypatch):
    import boto3

    made = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.region_name = kwargs.get("region_name") or "us-east-1"
            made.append(self)

        def client(self, name):
            if name == "sts":
                return self
            return (name, self.kwargs)

        def assume_role(self, RoleArn, RoleSessionName):
            assert RoleArn == "arn:aws:iam::1:role/r" and RoleSessionName == "rlp-retirement-sweep"
            return {"Credentials": {"AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t"}}

    monkeypatch.setattr(boto3, "Session", FakeSession)
    source = sweep.CloudWatchTraffic("arn:aws:iam::1:role/r")
    name, kwargs = source._client("cloudwatch")
    assert name == "cloudwatch" and kwargs["aws_access_key_id"] == "AKIA" and kwargs["region_name"] == "us-east-1"
    assert {"aws_secret_access_key", "aws_session_token"} <= set(kwargs)
    source._client("elbv2")
    assert len(made) == 2  # the role is assumed once
