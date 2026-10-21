// The API token, entered once per browser session and kept in sessionStorage.
// Storage can be unavailable (private windows, blocked site data): then the
// token lives only as long as the page.

export const TOKEN_KEY = "rlp.token";

export function loadToken(): string | null {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function saveToken(token: string): void {
  try {
    window.sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    // keep it in memory only
  }
}

export function clearToken(): void {
  try {
    window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // nothing stored
  }
}
