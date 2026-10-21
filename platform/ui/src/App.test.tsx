import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "./App";
import { TOKEN, installApi } from "./test/fakeApi";
import { TOKEN_KEY } from "./token";

function signedIn(path = "/") {
  window.sessionStorage.setItem(TOKEN_KEY, TOKEN);
  window.history.replaceState(null, "", path);
}

function projectNames(): string[] {
  const table = screen.getByRole("table");
  return within(table)
    .getAllByRole("rowheader")
    .map((cell) => cell.textContent ?? "");
}

describe("token", () => {
  it("asks for the token once and keeps it in sessionStorage", async () => {
    const api = installApi();
    const user = userEvent.setup();
    render(<App />);

    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(api.fetch).not.toHaveBeenCalled();
    await user.type(screen.getByLabelText("API token"), `  ${TOKEN} `);
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByRole("link", { name: "orders" })).toBeInTheDocument();
    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBe(TOKEN);
  });

  it("ignores an empty token", async () => {
    installApi();
    const user = userEvent.setup();
    render(<App />);
    const input = screen.getByLabelText("API token");
    await user.type(input, "   ");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBeNull();
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("sends a refused token back to the prompt", async () => {
    installApi();
    window.sessionStorage.setItem(TOKEN_KEY, "wrong");
    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent("The platform refused that token");
    expect(screen.getByLabelText("API token")).toBeInTheDocument();
    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBeNull();
  });

  it("sends a 401 on the project page back to the prompt", async () => {
    installApi(undefined, "rotated");
    signedIn("/projects/orders");
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("refused");
  });

  it("forgets the token on request", async () => {
    installApi();
    signedIn();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("link", { name: "orders" });
    await user.click(screen.getByRole("button", { name: "Forget token" }));
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBeNull();
  });
});

describe("inventory", () => {
  it("lists every project with its repos, latest build and last deployment", async () => {
    installApi();
    signedIn();
    render(<App />);

    await screen.findByRole("link", { name: "orders" });
    expect(projectNames()).toEqual(["orders", "billing", "search", "ledger"]);
    const orders = screen.getByRole("row", { name: /orders/ });
    expect(within(orders).getByText("rdlucas2")).toBeInTheDocument();
    expect(within(orders).getByText("in-dev")).toBeInTheDocument();
    expect(within(orders).getByText("2")).toBeInTheDocument();
    expect(within(orders).getByText("failed (lint, test)")).toBeInTheDocument();
    expect(within(orders).getByText("prod: succeeded, 2026-10-20 14:00 UTC")).toBeInTheDocument();
    const billing = screen.getByRole("row", { name: /billing/ });
    expect(within(billing).getAllByText("—")).toHaveLength(3);
  });

  it("filters by phase, keeps the filter in the URL and keeps it on reload", async () => {
    const api = installApi();
    signedIn();
    const user = userEvent.setup();
    const { unmount } = render(<App />);
    await screen.findByRole("link", { name: "orders" });

    await user.selectOptions(screen.getByLabelText("Phase"), "in-dev");

    await waitFor(() => expect(projectNames()).toEqual(["orders", "search"]));
    expect(window.location.search).toBe("?phase=in-dev");
    expect(api.paths()).toContain("/api/v1/projects?phase=in-dev");

    // Reload: a fresh app on the same URL.
    unmount();
    render(<App />);
    await screen.findByRole("link", { name: "orders" });
    expect(projectNames()).toEqual(["orders", "search"]);
    expect(screen.getByLabelText("Phase")).toHaveValue("in-dev");

    await user.selectOptions(screen.getByLabelText("Phase"), "");
    await waitFor(() => expect(projectNames()).toHaveLength(4));
    expect(window.location.search).toBe("");
  });

  it("searches by name and keeps the search in the URL", async () => {
    installApi();
    signedIn("/?phase=in-dev");
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("link", { name: "orders" });

    await user.type(screen.getByLabelText("Search by name"), "SEA");
    expect(projectNames()).toEqual(["search"]);
    expect(window.location.search).toBe("?phase=in-dev&q=SEA");

    // Enter doesn't reload the page.
    await user.type(screen.getByLabelText("Search by name"), "{Enter}");
    expect(projectNames()).toEqual(["search"]);

    await user.clear(screen.getByLabelText("Search by name"));
    await user.type(screen.getByLabelText("Search by name"), "nothing");
    expect(screen.getByText("No projects match.")).toBeInTheDocument();
  });

  it("ignores an unknown phase in the URL", async () => {
    const api = installApi();
    signedIn("/?phase=launched");
    render(<App />);
    await screen.findByRole("link", { name: "orders" });
    expect(screen.getByLabelText("Phase")).toHaveValue("");
    expect(api.paths()).toContain("/api/v1/projects");
  });

  it("shows an API error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("boom", { status: 500, statusText: "Server Error" })));
    signedIn();
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the projects: 500 Server Error");
  });

  it("opens a project from its row and comes back", async () => {
    installApi();
    signedIn();
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("link", { name: "orders" }));
    expect(await screen.findByRole("heading", { level: 1, name: /orders/ })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/projects/orders");

    await user.click(screen.getByRole("link", { name: "← All projects" }));
    expect(await screen.findByRole("heading", { name: "Projects" })).toBeInTheDocument();

    window.history.back();
    await waitFor(() => expect(window.location.pathname).toBe("/projects/orders"));
  });
});

describe("routing", () => {
  it("shows a not-found page for unknown paths", async () => {
    installApi();
    signedIn("/nowhere");
    render(<App />);
    expect(screen.getByRole("heading", { name: "Not found" })).toBeInTheDocument();
  });
});
