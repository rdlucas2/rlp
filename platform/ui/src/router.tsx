// A small client-side router: the inventory at /, a project at /projects/<name>.
// The platform answers every other non-API path with index.html, so these
// URLs survive a reload.
import { useEffect, useState, type AnchorHTMLAttributes, type MouseEvent } from "react";

export interface Location {
  pathname: string;
  search: string;
}

const NAVIGATE = "rlp:navigate";

function current(): Location {
  return { pathname: window.location.pathname, search: window.location.search };
}

export function useLocation(): Location {
  const [location, setLocation] = useState(current);
  useEffect(() => {
    const update = () => setLocation(current());
    window.addEventListener("popstate", update);
    window.addEventListener(NAVIGATE, update);
    return () => {
      window.removeEventListener("popstate", update);
      window.removeEventListener(NAVIGATE, update);
    };
  }, []);
  return location;
}

/** Go to `to`; `replace` swaps the history entry instead of adding one (for filter changes). */
export function navigate(to: string, { replace = false }: { replace?: boolean } = {}): void {
  if (replace) window.history.replaceState(null, "", to);
  else window.history.pushState(null, "", to);
  window.dispatchEvent(new Event(NAVIGATE));
}

export type Route = { page: "inventory" } | { page: "project"; name: string } | { page: "not-found" };

export function match(pathname: string): Route {
  if (pathname === "/" || pathname === "") return { page: "inventory" };
  const project = /^\/projects\/([^/]+)\/?$/.exec(pathname);
  if (project?.[1]) {
    try {
      return { page: "project", name: decodeURIComponent(project[1]) };
    } catch {
      return { page: "not-found" };
    }
  }
  return { page: "not-found" };
}

export function projectPath(name: string): string {
  return `/projects/${encodeURIComponent(name)}`;
}

type LinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & { to: string };

/** An anchor that navigates in the page; modified clicks (new tab, and so on) behave as usual. */
export function Link({ to, onClick, children, ...rest }: LinkProps) {
  const handle = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event);
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    navigate(to);
  };
  return (
    <a href={to} onClick={handle} {...rest}>
      {children}
    </a>
  );
}
