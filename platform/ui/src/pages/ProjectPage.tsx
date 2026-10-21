import { useCallback } from "react";

import type { Build, Client, Component, Deployment, Project, Repo, Transition } from "../api";
import { formatTime, shortCommit } from "../format";
import { useLoad } from "../load";
import { Link } from "../router";
import { BuildStatus, ExternalLink, PhaseBadge } from "../widgets";

interface Props {
  client: Client;
  name: string;
  onUnauthorized: () => void;
}

interface ProjectRecord {
  project: Project;
  repos: Repo[];
  components: Component[];
  builds: Build[];
  deployments: Deployment[];
  transitions: Transition[];
}

async function loadRecord(client: Client, name: string): Promise<ProjectRecord> {
  const [project, repos, components, builds, deployments, transitions] = await Promise.all([
    client.getProject(name),
    client.listRepos(name),
    client.listComponents(name),
    client.listBuilds(name),
    client.listDeployments(name),
    client.listTransitions(name),
  ]);
  return { project, repos, components, builds, deployments, transitions };
}

/** The newest item per key; `items` come newest first, as the API lists them. */
export function newestBy<T>(items: T[], key: (item: T) => string): T[] {
  const seen = new Map<string, T>();
  for (const item of items) {
    const k = key(item);
    if (!seen.has(k)) seen.set(k, item);
  }
  return [...seen.values()];
}

export function ProjectPage({ client, name, onUnauthorized }: Props) {
  const fetcher = useCallback(() => loadRecord(client, name), [client, name]);
  const load = useLoad(fetcher, onUnauthorized);

  if (load.state === "loading") return <p aria-live="polite">Loading {name}…</p>;
  if (load.state === "error") {
    return (
      <section>
        <h1>{name}</h1>
        <p role="alert" className="error">
          Could not load the project: {load.message}
        </p>
        <p>
          <Link to="/">Back to the inventory</Link>
        </p>
      </section>
    );
  }

  const { project, repos, components, builds, deployments, transitions } = load.data;
  return (
    <article>
      <p>
        <Link to="/">← All projects</Link>
      </p>
      <h1>
        {project.name} <PhaseBadge phase={project.phase} />
      </h1>
      {project.description && <p className="description">{project.description}</p>}
      <dl className="facts">
        <dt>Owner</dt>
        <dd>{project.owner || "—"}</dd>
        <dt>Dashboard</dt>
        <dd>
          {project.dashboard_url ? (
            <ExternalLink href={project.dashboard_url}>{project.dashboard_url}</ExternalLink>
          ) : (
            "—"
          )}
        </dd>
      </dl>

      <History transitions={transitions} />
      <Repos repos={repos} components={components} />
      <LatestBuilds builds={builds} />
      <Deployments deployments={deployments} />
    </article>
  );
}

function History({ transitions }: { transitions: Transition[] }) {
  return (
    <section aria-labelledby="history">
      <h2 id="history">Lifecycle history</h2>
      {transitions.length === 0 ? (
        <p>No moves yet.</p>
      ) : (
        <ol className="history">
          {transitions.map((t, i) => {
            const failed = t.rule_results.filter((r) => !r.passed);
            return (
              <li key={`${t.at}-${i}`} className={t.accepted ? "accepted" : "rejected"}>
                <p>
                  <strong>
                    {t.from_phase ?? "—"} → {t.to_phase}
                  </strong>{" "}
                  <span className={t.accepted ? "status ok" : "status bad"}>{t.accepted ? "moved" : "rejected"}</span>{" "}
                  by {t.actor}, {formatTime(t.at)}
                  {t.reason && <> — {t.reason}</>}
                </p>
                {failed.length > 0 && (
                  <ul className="rules" aria-label="Failing rules">
                    {failed.map((r) => (
                      <li key={r.rule}>
                        <code>{r.rule}</code> {r.description}
                        {r.messages.map((m) => (
                          <div key={m} className="message">
                            {m}
                          </div>
                        ))}
                      </li>
                    ))}
                  </ul>
                )}
                <details>
                  <summary>
                    {t.rule_results.length - failed.length} of {t.rule_results.length} rules passed
                  </summary>
                  <ul className="rules">
                    {t.rule_results.map((r) => (
                      <li key={r.rule}>
                        <code>{r.rule}</code> {r.passed ? "passed" : "failed"}: {r.description}
                      </li>
                    ))}
                  </ul>
                </details>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

function Repos({ repos, components }: { repos: Repo[]; components: Component[] }) {
  return (
    <section aria-labelledby="repos">
      <h2 id="repos">Repos</h2>
      {repos.length === 0 && <p>No repos linked yet.</p>}
      {repos.map((repo) => {
        const own = components.filter((c) => c.repo === repo.full_name);
        return (
          <section key={repo.full_name} className="repo" aria-label={repo.full_name}>
            <h3>{repo.full_name}</h3>
            <dl className="facts">
              <dt>Default branch</dt>
              <dd>{repo.default_branch}</dd>
              <dt>Template</dt>
              <dd>
                {repo.template_source
                  ? `${repo.template_source}${repo.template_version ? ` @ ${repo.template_version}` : ""}`
                  : "—"}
              </dd>
              <dt>Last indexed commit</dt>
              <dd>{repo.last_indexed_commit ? <code>{shortCommit(repo.last_indexed_commit)}</code> : "—"}</dd>
              {repo.index_error && (
                <>
                  <dt>Index error</dt>
                  <dd className="error">{repo.index_error}</dd>
                </>
              )}
            </dl>
            {own.length === 0 ? (
              <p>No components.</p>
            ) : (
              <table>
                <caption>Components of {repo.full_name}</caption>
                <thead>
                  <tr>
                    <th scope="col">Component</th>
                    <th scope="col">Language</th>
                    <th scope="col">Artifact</th>
                    <th scope="col">Runtime</th>
                  </tr>
                </thead>
                <tbody>
                  {own.map((c) => (
                    <tr key={c.name}>
                      <th scope="row">{c.name}</th>
                      <td>{c.language}</td>
                      <td>{c.artifact_kind}</td>
                      <td>{c.runtime_kind}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        );
      })}
    </section>
  );
}

function LatestBuilds({ builds }: { builds: Build[] }) {
  const latest = newestBy(builds, (b) => b.repo);
  return (
    <section aria-labelledby="builds">
      <h2 id="builds">Latest builds</h2>
      {latest.length === 0 ? (
        <p>No builds reported yet.</p>
      ) : (
        <table>
          <caption className="visually-hidden">The latest build of each repo</caption>
          <thead>
            <tr>
              <th scope="col">Repo</th>
              <th scope="col">Result</th>
              <th scope="col">Commit</th>
              <th scope="col">Branch</th>
              <th scope="col">Started</th>
              <th scope="col">Run</th>
            </tr>
          </thead>
          <tbody>
            {latest.map((b) => (
              <tr key={b.id}>
                <th scope="row">{b.repo}</th>
                <td>
                  <BuildStatus build={b} />
                </td>
                <td>
                  <code>{shortCommit(b.commit)}</code>
                </td>
                <td>{b.branch}</td>
                <td>{formatTime(b.started_at)}</td>
                <td>{b.workflow_run_url ? <ExternalLink href={b.workflow_run_url}>View run</ExternalLink> : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function Deployments({ deployments }: { deployments: Deployment[] }) {
  const environments = [...new Set(deployments.map((d) => d.environment))].sort();
  return (
    <section aria-labelledby="deployments">
      <h2 id="deployments">Deployments</h2>
      {environments.length === 0 && <p>Nothing deployed yet.</p>}
      {environments.map((env) => {
        const current = newestBy(
          deployments.filter((d) => d.environment === env),
          (d) => `${d.repo}/${d.component}`,
        );
        return (
          <table key={env}>
            <caption>{env}</caption>
            <thead>
              <tr>
                <th scope="col">Component</th>
                <th scope="col">Status</th>
                <th scope="col">Artifact</th>
                <th scope="col">Deployed</th>
              </tr>
            </thead>
            <tbody>
              {current.map((d) => (
                <tr key={d.id}>
                  <th scope="row">
                    {d.repo} / {d.component}
                  </th>
                  <td>{d.status}</td>
                  <td>
                    <code>{d.artifact.reference}</code>
                  </td>
                  <td>{formatTime(d.deployed_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        );
      })}
    </section>
  );
}
