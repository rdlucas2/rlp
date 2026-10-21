import { useCallback } from "react";

import { PHASES, isPhase, type Build, type Client, type Deployment, type Phase, type Project } from "../api";
import { formatTime } from "../format";
import { useLoad } from "../load";
import { Link, navigate, projectPath } from "../router";
import { BuildStatus, PhaseBadge } from "../widgets";

interface Props {
  client: Client;
  search: string;
  onUnauthorized: () => void;
}

export interface Row {
  project: Project;
  repos: number;
  latestBuild: Build | undefined;
  lastDeployment: Deployment | undefined;
}

/** The filter lives in the URL (?phase=in-dev&q=orders), so a reload or a shared link keeps it. */
export function readPhase(search: string): Phase | undefined {
  const phase = new URLSearchParams(search).get("phase");
  return isPhase(phase) ? phase : undefined;
}

export function readQuery(search: string): string {
  return new URLSearchParams(search).get("q") ?? "";
}

export function filterUrl(phase: Phase | undefined, q: string): string {
  const params = new URLSearchParams();
  if (phase) params.set("phase", phase);
  if (q) params.set("q", q);
  const qs = params.toString();
  return qs ? `/?${qs}` : "/";
}

async function loadRows(client: Client, phase: Phase | undefined): Promise<Row[]> {
  const projects = await client.listProjects(phase);
  return Promise.all(
    projects.map(async (project) => {
      const [repos, builds, deployments] = await Promise.all([
        client.listRepos(project.name),
        client.listBuilds(project.name, 1),
        client.listDeployments(project.name, 1),
      ]);
      return { project, repos: repos.length, latestBuild: builds[0], lastDeployment: deployments[0] };
    }),
  );
}

export function Inventory({ client, search, onUnauthorized }: Props) {
  const phase = readPhase(search);
  const q = readQuery(search);
  const fetcher = useCallback(() => loadRows(client, phase), [client, phase]);
  const load = useLoad(fetcher, onUnauthorized);
  const needle = q.trim().toLowerCase();

  return (
    <section>
      <h1>Projects</h1>
      <form className="filters" role="search" onSubmit={(event) => event.preventDefault()}>
        <div>
          <label htmlFor="phase-filter">Phase</label>
          <select
            id="phase-filter"
            value={phase ?? ""}
            onChange={(event) => {
              const value = event.target.value;
              navigate(filterUrl(isPhase(value) ? value : undefined, q), { replace: true });
            }}
          >
            <option value="">All phases</option>
            {PHASES.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="name-search">Search by name</label>
          <input
            id="name-search"
            type="search"
            value={q}
            onChange={(event) => navigate(filterUrl(phase, event.target.value), { replace: true })}
          />
        </div>
      </form>

      {load.state === "loading" && <p aria-live="polite">Loading projects…</p>}
      {load.state === "error" && (
        <p role="alert" className="error">
          Could not load the projects: {load.message}
        </p>
      )}
      {load.state === "ready" && (
        <ProjectTable rows={load.data.filter((row) => row.project.name.toLowerCase().includes(needle))} />
      )}
    </section>
  );
}

function ProjectTable({ rows }: { rows: Row[] }) {
  if (rows.length === 0) return <p>No projects match.</p>;
  return (
    <table>
      <caption className="visually-hidden">Projects, one row per project</caption>
      <thead>
        <tr>
          <th scope="col">Name</th>
          <th scope="col">Owner</th>
          <th scope="col">Phase</th>
          <th scope="col">Repos</th>
          <th scope="col">Latest build</th>
          <th scope="col">Last deployment</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(({ project, repos, latestBuild, lastDeployment }) => (
          <tr key={project.name}>
            <th scope="row">
              <Link to={projectPath(project.name)}>{project.name}</Link>
            </th>
            <td>{project.owner || "—"}</td>
            <td>
              <PhaseBadge phase={project.phase} />
            </td>
            <td>{repos}</td>
            <td>{latestBuild ? <BuildStatus build={latestBuild} /> : "—"}</td>
            <td>
              {lastDeployment
                ? `${lastDeployment.environment}: ${lastDeployment.status}, ${formatTime(lastDeployment.deployed_at)}`
                : "—"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
