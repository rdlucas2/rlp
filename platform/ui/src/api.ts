// A typed client for the platform API (/api/v1). The UI reads nothing else, so
// everything it shows is also available through `rlp`.

export const PHASES = ["proposed", "designed", "scaffolded", "in-dev", "live", "deprecated", "retired"] as const;
export type Phase = (typeof PHASES)[number];

export function isPhase(value: string | null): value is Phase {
  return value !== null && (PHASES as readonly string[]).includes(value);
}

export interface Project {
  name: string;
  description: string;
  owner: string;
  phase: Phase;
  dashboard_url: string | null;
  created_at: string;
  updated_at: string;
}

export interface Repo {
  full_name: string;
  project: string;
  default_branch: string;
  template_source: string | null;
  template_version: string | null;
  last_indexed_commit: string | null;
  index_error: string | null;
}

export interface Component {
  repo: string;
  name: string;
  language: string;
  artifact_kind: string;
  runtime_kind: string;
  removed_at: string | null;
}

export interface Build {
  id: number;
  repo: string;
  commit: string;
  branch: string;
  passed: boolean;
  failed_stages: string[];
  workflow_run_url: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface Deployment {
  id: number;
  repo: string;
  component: string;
  environment: string;
  status: string;
  artifact: { kind: string; reference: string };
  deployed_at: string;
}

export interface RuleResult {
  rule: string;
  description: string;
  passed: boolean;
  messages: string[];
}

export interface Transition {
  from_phase: Phase | null;
  to_phase: Phase;
  actor: string;
  reason: string;
  accepted: boolean;
  rule_results: RuleResult[];
  at: string;
}

/** The platform refused the token (401). */
export class Unauthorized extends Error {
  constructor() {
    super("The platform refused the token.");
    this.name = "Unauthorized";
  }
}

/** Any other failed request. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const API_BASE = "/api/v1";

export class Client {
  private readonly token: string;

  constructor(token: string) {
    this.token = token;
  }

  async get<T>(path: string, params?: Record<string, string | number | boolean | undefined>): Promise<T> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params ?? {})) {
      if (value !== undefined) query.set(key, String(value));
    }
    const qs = query.toString();
    const resp = await fetch(`${API_BASE}${path}${qs ? `?${qs}` : ""}`, {
      headers: { Authorization: `Bearer ${this.token}`, Accept: "application/json" },
    });
    if (resp.status === 401) throw new Unauthorized();
    if (!resp.ok) throw new ApiError(resp.status, await errorDetail(resp));
    return (await resp.json()) as T;
  }

  listProjects(phase?: Phase): Promise<Project[]> {
    return this.get<Project[]>("/projects", { phase });
  }

  getProject(name: string): Promise<Project> {
    return this.get<Project>(`/projects/${encodeURIComponent(name)}`);
  }

  listRepos(project: string): Promise<Repo[]> {
    return this.get<Repo[]>(`/projects/${encodeURIComponent(project)}/repos`);
  }

  listComponents(project: string): Promise<Component[]> {
    return this.get<Component[]>(`/projects/${encodeURIComponent(project)}/components`);
  }

  listBuilds(project: string, limit?: number): Promise<Build[]> {
    return this.get<Build[]>(`/projects/${encodeURIComponent(project)}/builds`, { limit });
  }

  listDeployments(project: string, limit?: number): Promise<Deployment[]> {
    return this.get<Deployment[]>(`/projects/${encodeURIComponent(project)}/deployments`, { limit });
  }

  listTransitions(project: string): Promise<Transition[]> {
    return this.get<Transition[]>(`/projects/${encodeURIComponent(project)}/transitions`, { include_rejected: true });
  }
}

async function errorDetail(resp: Response): Promise<string> {
  try {
    const body: unknown = await resp.json();
    if (body && typeof body === "object" && "detail" in body && typeof body.detail === "string") {
      return body.detail;
    }
  } catch {
    // not JSON: fall through to the status line
  }
  return `${resp.status} ${resp.statusText}`.trim();
}

/** Only http(s) links are rendered, so a stored URL can't run script. */
export function safeUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch {
    return null;
  }
}
