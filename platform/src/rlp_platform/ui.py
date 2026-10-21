"""Serve the built inventory UI (platform/ui) at /.

The UI is a single-page app: a path that names a file in the build is that
file, and any other path outside /api gets index.html so the app's own routes
(/projects/<name>) survive a reload. /api stays the API's: an unknown API path
answers 404, never the page. The image builds the UI and sets RLP_UI_DIR.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse

log = logging.getLogger(__name__)

# Vite fingerprints everything under assets/, so those files never change.
IMMUTABLE = "public, max-age=31536000, immutable"
# index.html names the current assets; always revalidate it.
REVALIDATE = "no-cache"


def mount(app: FastAPI, ui_dir: str | Path) -> bool:
    """Add the UI routes to `app`. False (and nothing added) when `ui_dir` has no index.html."""
    root = Path(ui_dir).resolve()
    index = root / "index.html"
    if not index.is_file():
        log.warning("no UI to serve", extra={"ui_dir": str(root)})
        return False

    def page(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not Found")
        candidate = (root / path).resolve()
        if path and candidate.is_relative_to(root) and candidate.is_file():
            cache = IMMUTABLE if candidate.relative_to(root).parts[0] == "assets" else REVALIDATE
            return FileResponse(candidate, headers={"Cache-Control": cache})
        return FileResponse(index, headers={"Cache-Control": REVALIDATE})

    app.add_api_route("/{path:path}", page, methods=["GET", "HEAD"], include_in_schema=False)
    return True
