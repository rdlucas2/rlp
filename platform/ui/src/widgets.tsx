import type { ReactNode } from "react";

import { safeUrl, type Build, type Phase } from "./api";

export function PhaseBadge({ phase }: { phase: Phase }) {
  return <span className={`badge phase-${phase}`}>{phase}</span>;
}

/** "passed", or "failed" with the failed stages. Status is in the text, not only the colour. */
export function BuildStatus({ build }: { build: Build }) {
  if (build.passed) return <span className="status ok">passed</span>;
  const stages = build.failed_stages.length ? ` (${build.failed_stages.join(", ")})` : "";
  return <span className="status bad">failed{stages}</span>;
}

/** A link to somewhere outside the UI; anything but http(s) is shown as plain text. */
export function ExternalLink({ href, children }: { href: string | null | undefined; children: ReactNode }) {
  const url = safeUrl(href);
  if (url === null) return <>{children}</>;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}
