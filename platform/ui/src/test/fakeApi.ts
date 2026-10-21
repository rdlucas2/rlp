// A fake platform API for the tests: seeded records behind a stubbed fetch that
// checks the bearer token and answers the routes the UI calls.
import type { Build, Component, Deployment, Project, Repo, Transition } from "../api";

export const TOKEN = "s3cret";

export interface Seed {
  projects: Project[];
  repos: Repo[];
  components: Component[];
  builds: Build[];
  deployments: Deployment[];
  transitions: Record<string, Transition[]>;
}

const AT = "2026-10-20T12:00:00Z";

export function project(name: string, phase: Project["phase"], extra: Partial<Project> = {}): Project {
  return {
    name,
    phase,
    description: "",
    owner: "",
    dashboard_url: null,
    created_at: AT,
    updated_at: AT,
    ...extra,
  };
}

export function seed(): Seed {
  return {
    projects: [
      project("orders", "in-dev", { owner: "rdlucas2", description: "Takes orders." }),
      project("billing", "proposed"),
      project("search", "in-dev", { owner: "sam" }),
      project("ledger", "live", { owner: "rdlucas2", dashboard_url: "https://grafana.example/d/ledger" }),
    ],
    repos: [
      {
        full_name: "acme/orders-api",
        project: "orders",
        default_branch: "main",
        template_source: "gh:rdlucas2/rlp-template-python-fastapi",
        template_version: "d5f0037",
        last_indexed_commit: "abcdef1234567890",
        index_error: null,
      },
      {
        full_name: "acme/orders-web",
        project: "orders",
        default_branch: "trunk",
        template_source: null,
        template_version: null,
        last_indexed_commit: null,
        index_error: "rlp.yaml is missing",
      },
    ],
    components: [
      {
        repo: "acme/orders-api",
        name: "api",
        language: "python",
        artifact_kind: "container-image",
        runtime_kind: "container-service",
        removed_at: null,
      },
    ],
    builds: [
      {
        id: 3,
        repo: "acme/orders-api",
        commit: "ccccccc9999",
        branch: "main",
        passed: false,
        failed_stages: ["lint", "test"],
        workflow_run_url: "https://github.com/acme/orders-api/actions/runs/3",
        started_at: "2026-10-20T13:00:00Z",
        finished_at: null,
      },
      {
        id: 2,
        repo: "acme/orders-web",
        commit: "bbbbbbb8888",
        branch: "trunk",
        passed: true,
        failed_stages: [],
        workflow_run_url: "javascript:alert(1)",
        started_at: "2026-10-20T12:30:00Z",
        finished_at: null,
      },
      {
        id: 1,
        repo: "acme/orders-api",
        commit: "aaaaaaa7777",
        branch: "main",
        passed: true,
        failed_stages: [],
        workflow_run_url: null,
        started_at: "2026-10-20T12:00:00Z",
        finished_at: null,
      },
    ],
    deployments: [
      {
        id: 2,
        repo: "acme/orders-api",
        component: "api",
        environment: "prod",
        status: "succeeded",
        artifact: { kind: "container-image", reference: "ghcr.io/acme/orders-api@sha256:2" },
        deployed_at: "2026-10-20T14:00:00Z",
      },
      {
        id: 1,
        repo: "acme/orders-api",
        component: "api",
        environment: "dev",
        status: "failed",
        artifact: { kind: "container-image", reference: "ghcr.io/acme/orders-api@sha256:1" },
        deployed_at: "2026-10-20T13:30:00Z",
      },
      {
        id: 0,
        repo: "acme/orders-api",
        component: "api",
        environment: "dev",
        status: "succeeded",
        artifact: { kind: "container-image", reference: "ghcr.io/acme/orders-api@sha256:0" },
        deployed_at: "2026-10-20T13:00:00Z",
      },
    ],
    transitions: {
      orders: [
        {
          from_phase: "scaffolded",
          to_phase: "in-dev",
          actor: "rdlucas2",
          reason: "first build",
          accepted: true,
          rule_results: [{ rule: "RLP-L101", description: "the move is allowed", passed: true, messages: [] }],
          at: "2026-10-20T12:10:00Z",
        },
        {
          from_phase: "in-dev",
          to_phase: "live",
          actor: "rdlucas2",
          reason: "launch",
          accepted: false,
          rule_results: [
            { rule: "RLP-L101", description: "the move is allowed", passed: true, messages: [] },
            {
              rule: "RLP-L105",
              description: "live needs a dashboard",
              passed: false,
              messages: ['[RLP-L105] rlp-transition.json project.dashboard_url: project "orders" has no dashboard'],
            },
          ],
          at: "2026-10-20T15:00:00Z",
        },
      ],
    },
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

export interface FakeApi {
  fetch: ReturnType<typeof vi.fn>;
  paths: () => string[];
}

/** Stub fetch with the seeded API. Requests with another token get 401. */
export function installApi(data: Seed = seed(), token = TOKEN): FakeApi {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://ui.test");
    const auth = new Headers(init?.headers).get("Authorization");
    if (auth !== `Bearer ${token}`) return json({ detail: "missing or wrong bearer token" }, 401);
    const limit = Number(url.searchParams.get("limit") ?? "100");
    const path = url.pathname.replace(/^\/api\/v1/, "");

    if (path === "/projects") {
      const phase = url.searchParams.get("phase");
      return json(data.projects.filter((p) => !phase || p.phase === phase));
    }
    const m = /^\/projects\/([^/]+)(\/[a-z]+)?$/.exec(path);
    const name = m?.[1] ? decodeURIComponent(m[1]) : undefined;
    const record = data.projects.find((p) => p.name === name);
    if (!m || !record) return json({ detail: `no project named ${name}` }, 404);
    const repos = data.repos.filter((r) => r.project === record.name);
    const names = new Set(repos.map((r) => r.full_name));
    switch (m[2]) {
      case undefined:
        return json(record);
      case "/repos":
        return json(repos);
      case "/components":
        return json(data.components.filter((c) => names.has(c.repo)));
      case "/builds":
        return json(data.builds.filter((b) => names.has(b.repo)).slice(0, limit));
      case "/deployments":
        return json(data.deployments.filter((d) => names.has(d.repo)).slice(0, limit));
      case "/transitions": {
        const history = data.transitions[record.name] ?? [];
        const all = url.searchParams.get("include_rejected") === "true";
        return json(all ? history : history.filter((t) => t.accepted));
      }
      default:
        return json({ detail: "Not Found" }, 404);
    }
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    fetch: fetchMock,
    paths: () => fetchMock.mock.calls.map(([input]) => String(input)),
  };
}
