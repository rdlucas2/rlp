import { render, screen, within } from "@testing-library/react";

import { App } from "../App";
import { TOKEN, installApi, project, seed } from "../test/fakeApi";
import { TOKEN_KEY } from "../token";
import { newestBy } from "./ProjectPage";

/** The element, or a test failure when it is missing. */
function present<T>(value: T | undefined): T {
  if (value === undefined) throw new Error("expected an element");
  return value;
}

function open(path: string) {
  window.sessionStorage.setItem(TOKEN_KEY, TOKEN);
  window.history.replaceState(null, "", path);
  render(<App />);
}

describe("project page", () => {
  it("shows the record: description, owner, phase", async () => {
    installApi();
    open("/projects/orders");

    const heading = await screen.findByRole("heading", { level: 1 });
    expect(heading).toHaveTextContent("orders");
    expect(heading).toHaveTextContent("in-dev");
    expect(screen.getByText("Takes orders.")).toBeInTheDocument();
    expect(screen.getByText("rdlucas2")).toBeInTheDocument();
  });

  it("shows a rejected transition's failing rules from the history", async () => {
    const api = installApi();
    open("/projects/orders");

    const history = await screen.findByRole("region", { name: "Lifecycle history" });
    const items = Array.from(history.querySelectorAll<HTMLElement>("ol.history > li"));
    expect(items).toHaveLength(2);
    const accepted = present(items[0]);
    const rejected = present(items[1]);
    expect(accepted).toHaveTextContent("scaffolded → in-dev moved by rdlucas2");
    expect(rejected).toHaveTextContent("in-dev → live rejected by rdlucas2, 2026-10-20 15:00 UTC — launch");
    const failing = within(rejected).getByRole("list", { name: "Failing rules" });
    expect(failing).toHaveTextContent("RLP-L105 live needs a dashboard");
    expect(failing).toHaveTextContent('project "orders" has no dashboard');
    expect(failing).not.toHaveTextContent("RLP-L101");
    expect(within(rejected).getByText("1 of 2 rules passed")).toBeInTheDocument();
    expect(api.paths()).toContain("/api/v1/projects/orders/transitions?include_rejected=true");
  });

  it("shows each repo with its components and template version", async () => {
    installApi();
    open("/projects/orders");

    const api = await screen.findByRole("region", { name: "acme/orders-api" });
    expect(api).toHaveTextContent("gh:rdlucas2/rlp-template-python-fastapi @ d5f0037");
    expect(api).toHaveTextContent("abcdef1");
    const components = within(api).getByRole("table", { name: "Components of acme/orders-api" });
    expect(within(components).getByRole("row", { name: /api/ })).toHaveTextContent(
      "apipythoncontainer-imagecontainer-service",
    );

    const web = screen.getByRole("region", { name: "acme/orders-web" });
    expect(web).toHaveTextContent("rlp.yaml is missing");
    expect(web).toHaveTextContent("No components.");
  });

  it("shows the latest build per repo with a link to its run", async () => {
    installApi();
    open("/projects/orders");

    const builds = await screen.findByRole("region", { name: "Latest builds" });
    const rows = within(builds).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    const [api, web] = [present(rows[0]), present(rows[1])];
    expect(api).toHaveTextContent("acme/orders-apifailed (lint, test)ccccccc");
    expect(within(api).getByRole("link", { name: "View run" })).toHaveAttribute(
      "href",
      "https://github.com/acme/orders-api/actions/runs/3",
    );
    // A run URL that isn't http(s) is not made a link.
    expect(web).toHaveTextContent("acme/orders-webpassedbbbbbbb");
    expect(within(web).queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows the current deployment per environment", async () => {
    installApi();
    open("/projects/orders");

    const deployments = await screen.findByRole("region", { name: "Deployments" });
    const dev = within(deployments).getByRole("table", { name: "dev" });
    const prod = within(deployments).getByRole("table", { name: "prod" });
    expect(within(dev).getAllByRole("row")).toHaveLength(2);
    expect(dev).toHaveTextContent("acme/orders-api / apifailedghcr.io/acme/orders-api@sha256:1");
    expect(prod).toHaveTextContent("succeeded");
  });

  it("links the dashboard", async () => {
    installApi();
    open("/projects/ledger");
    expect(await screen.findByRole("link", { name: "https://grafana.example/d/ledger" })).toHaveAttribute(
      "rel",
      "noopener noreferrer",
    );
  });

  it("says when there is nothing yet", async () => {
    installApi();
    open("/projects/billing");
    expect(await screen.findByText("No moves yet.")).toBeInTheDocument();
    expect(screen.getByText("No repos linked yet.")).toBeInTheDocument();
    expect(screen.getByText("No builds reported yet.")).toBeInTheDocument();
    expect(screen.getByText("Nothing deployed yet.")).toBeInTheDocument();
  });

  it("shows a dashboard URL that isn't http(s) as text", async () => {
    const data = seed();
    data.projects = [project("odd", "live", { dashboard_url: "javascript:alert(1)" })];
    installApi(data);
    open("/projects/odd");
    expect(await screen.findByText("javascript:alert(1)")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "javascript:alert(1)" })).not.toBeInTheDocument();
  });

  it("reports an unknown project", async () => {
    installApi();
    open("/projects/nope");
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the project: no project named nope");
    expect(screen.getByRole("link", { name: "Back to the inventory" })).toHaveAttribute("href", "/");
  });
});

describe("newestBy", () => {
  it("keeps the first item per key", () => {
    expect(newestBy([{ k: "a", n: 1 }, { k: "b", n: 2 }, { k: "a", n: 3 }], (i) => i.k)).toEqual([
      { k: "a", n: 1 },
      { k: "b", n: 2 },
    ]);
  });
});
