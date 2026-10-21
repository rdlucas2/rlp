import { fireEvent, render, screen } from "@testing-library/react";

import { ApiError, Client, Unauthorized, isPhase, safeUrl } from "./api";
import { formatTime, shortCommit } from "./format";
import { Link, match, projectPath } from "./router";
import { clearToken, loadToken, saveToken } from "./token";

describe("api client", () => {
  it("sends the bearer token and leaves out unset parameters", async () => {
    const fetchMock = vi.fn(async () => new Response("[]", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await new Client("t0k").listProjects();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/projects", {
      headers: { Authorization: "Bearer t0k", Accept: "application/json" },
    });
  });

  it("encodes project names in paths", async () => {
    const fetchMock = vi.fn(async (input: string) => new Response(JSON.stringify({ input }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await new Client("t").getProject("a/b");
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/projects/a%2Fb");
  });

  it("raises Unauthorized on 401 and ApiError with the detail otherwise", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 401 })));
    await expect(new Client("t").listProjects()).rejects.toBeInstanceOf(Unauthorized);

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "no database" }), { status: 503 })),
    );
    const err = await new Client("t").listProjects().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(503);
    expect((err as ApiError).message).toBe("no database");

    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: [1] }), { status: 422 })));
    await expect(new Client("t").listProjects()).rejects.toThrow("422");
  });

  it("knows the phases", () => {
    expect(isPhase("in-dev")).toBe(true);
    expect(isPhase("launched")).toBe(false);
    expect(isPhase(null)).toBe(false);
  });

  it("only passes http(s) URLs", () => {
    expect(safeUrl("https://grafana.example/d/x")).toBe("https://grafana.example/d/x");
    expect(safeUrl("http://localhost:3000/")).toBe("http://localhost:3000/");
    expect(safeUrl("javascript:alert(1)")).toBeNull();
    expect(safeUrl("not a url")).toBeNull();
    expect(safeUrl(null)).toBeNull();
    expect(safeUrl("")).toBeNull();
  });
});

describe("format", () => {
  it("shortens commits and formats times in UTC", () => {
    expect(shortCommit("abcdef1234")).toBe("abcdef1");
    expect(formatTime("2026-10-20T12:34:56.789Z")).toBe("2026-10-20 12:34 UTC");
    expect(formatTime(null)).toBe("—");
    expect(formatTime("yesterday")).toBe("yesterday");
  });
});

describe("router", () => {
  it("matches the inventory and project pages", () => {
    expect(match("/")).toEqual({ page: "inventory" });
    expect(match("")).toEqual({ page: "inventory" });
    expect(match("/projects/orders")).toEqual({ page: "project", name: "orders" });
    expect(match("/projects/orders/")).toEqual({ page: "project", name: "orders" });
    expect(match("/projects/a%2Fb")).toEqual({ page: "project", name: "a/b" });
    expect(match("/projects/%E0%A4%A")).toEqual({ page: "not-found" });
    expect(match("/projects/a/b")).toEqual({ page: "not-found" });
    expect(match("/elsewhere")).toEqual({ page: "not-found" });
    expect(projectPath("a b")).toBe("/projects/a%20b");
  });

  it("navigates on a plain click and leaves modified clicks to the browser", () => {
    const onClick = vi.fn();
    render(
      <Link to="/projects/orders" onClick={onClick}>
        orders
      </Link>,
    );
    const link = screen.getByRole("link", { name: "orders" });

    const modified = fireEvent.click(link, { ctrlKey: true });
    expect(modified).toBe(true); // not prevented
    expect(window.location.pathname).toBe("/");

    const plain = fireEvent.click(link);
    expect(plain).toBe(false);
    expect(window.location.pathname).toBe("/projects/orders");
    expect(onClick).toHaveBeenCalledTimes(2);
  });
});

describe("token storage", () => {
  it("round-trips through sessionStorage", () => {
    saveToken("abc");
    expect(loadToken()).toBe("abc");
    clearToken();
    expect(loadToken()).toBeNull();
  });

  it("survives storage that throws", () => {
    const broken = () => {
      throw new Error("blocked");
    };
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(broken);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(broken);
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(broken);
    expect(() => saveToken("abc")).not.toThrow();
    expect(loadToken()).toBeNull();
    expect(() => clearToken()).not.toThrow();
  });
});
