import { useCallback, useMemo, useState } from "react";

import { Client } from "./api";
import { Inventory } from "./pages/Inventory";
import { ProjectPage } from "./pages/ProjectPage";
import { Link, match, useLocation } from "./router";
import { TokenPrompt } from "./TokenPrompt";
import { clearToken, loadToken, saveToken } from "./token";

export function App() {
  const [token, setToken] = useState<string | null>(loadToken);
  const [refused, setRefused] = useState(false);
  const location = useLocation();
  const client = useMemo(() => (token ? new Client(token) : null), [token]);

  const signIn = useCallback((value: string) => {
    saveToken(value);
    setRefused(false);
    setToken(value);
  }, []);

  const onUnauthorized = useCallback(() => {
    clearToken();
    setRefused(true);
    setToken(null);
  }, []);

  const signOut = useCallback(() => {
    clearToken();
    setRefused(false);
    setToken(null);
  }, []);

  const route = match(location.pathname);

  return (
    <>
      <header className="masthead">
        <Link to="/" className="brand">
          rlp inventory
        </Link>
        {client && (
          <button type="button" className="link-button" onClick={signOut}>
            Forget token
          </button>
        )}
      </header>
      <main id="main">
        {client === null ? (
          <TokenPrompt refused={refused} onSubmit={signIn} />
        ) : route.page === "inventory" ? (
          <Inventory client={client} search={location.search} onUnauthorized={onUnauthorized} />
        ) : route.page === "project" ? (
          <ProjectPage key={route.name} client={client} name={route.name} onUnauthorized={onUnauthorized} />
        ) : (
          <section>
            <h1>Not found</h1>
            <p>
              Nothing lives at this address. <Link to="/">Back to the inventory</Link>.
            </p>
          </section>
        )}
      </main>
    </>
  );
}
