# UI stage: build the inventory UI (ui/) into static files; none of Node ships in the image.
FROM node:22-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ui/.npmrc ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

# Build stage: install the locked runtime dependencies and the app into /app/.venv.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src/ src/
COPY migrations/ migrations/
RUN uv sync --locked --no-dev --no-editable
# The lifecycle rules run on OPA: download the pinned, checksum-verified binary
# for this architecture (rlp_platform/opa.py pins it where the rlp CLI does).
RUN /app/.venv/bin/python -m rlp_platform.opa /opt/opa/opa

# Runtime stage: the virtualenv only, run as a non-root user.
FROM python:3.13-slim
# The app needs no installer at runtime; removing pip and setuptools keeps them
# (and the libraries pip vendors) out of the image the trivy gate scans.
RUN python -m pip uninstall --yes --no-input pip setuptools \
    && useradd --system --uid 10001 --no-create-home app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /opt/opa/opa /usr/local/bin/opa
# The platform serves the UI at / (rlp_platform.ui); the API stays under /api.
COPY --from=ui /ui/dist /app/ui
ENV PATH=/app/.venv/bin:$PATH \
    RLP_UI_DIR=/app/ui \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/healthz\", timeout=2)"]
CMD ["python", "-m", "rlp_platform"]
