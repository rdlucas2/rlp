import { useState, type FormEvent } from "react";

interface Props {
  refused: boolean;
  onSubmit: (token: string) => void;
}

/** Asks for the platform's API token (RLP_API_TOKEN); it is kept for this browser session only. */
export function TokenPrompt({ refused, onSubmit }: Props) {
  const [value, setValue] = useState("");

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const token = value.trim();
    if (token) onSubmit(token);
  };

  return (
    <section className="token-prompt">
      <h1>Sign in</h1>
      {refused && (
        <p role="alert" className="error">
          The platform refused that token. Enter it again.
        </p>
      )}
      <form onSubmit={submit}>
        <label htmlFor="token">API token</label>
        <p id="token-help" className="hint">
          The platform&apos;s RLP_API_TOKEN. It is kept in this tab until you close it.
        </p>
        <input
          id="token"
          name="token"
          type="password"
          autoComplete="current-password"
          aria-describedby="token-help"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          required
        />
        <button type="submit">Continue</button>
      </form>
    </section>
  );
}
