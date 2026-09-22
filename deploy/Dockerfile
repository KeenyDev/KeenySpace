FROM python:3.14.1-slim-bookworm AS builder

RUN pip install --no-cache-dir uv==0.10.8

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/server/pyproject.toml packages/server/pyproject.toml
COPY packages/shared/pyproject.toml packages/shared/pyproject.toml
COPY packages/client/pyproject.toml packages/client/pyproject.toml

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package keenyspace-server

COPY packages/server/keenyspace_server packages/server/keenyspace_server
COPY packages/shared/keenyspace_shared packages/shared/keenyspace_shared

# Workspace members stay editable (installed as paths into /app/packages): the
# server resolves alembic.ini and the default blueprints dir relative to its
# source tree, so the runtime stage keeps that layout.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package keenyspace-server && \
    .venv/bin/python -m compileall -q packages/server/keenyspace_server packages/shared/keenyspace_shared

FROM python:3.14.1-slim-bookworm AS runtime

# The admin backup/restore endpoints shell out to pg_dump/psql. The client
# major must be >= the postgres server major (17), and bookworm ships 15 —
# pull postgresql-client-17 from PGDG (publishes amd64 + arm64).
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl && \
    install -d /usr/share/postgresql-common/pgdg && \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc && \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends postgresql-client-17 && \
    apt-get purge -y curl && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 nonroot && \
    useradd --uid 10001 --gid 10001 --no-create-home nonroot

WORKDIR /app

COPY --from=builder /app/.venv .venv
COPY --from=builder /app/packages/server/keenyspace_server packages/server/keenyspace_server
COPY --from=builder /app/packages/shared/keenyspace_shared packages/shared/keenyspace_shared
COPY packages/server/alembic.ini packages/server/alembic.ini
COPY packages/server/alembic packages/server/alembic
COPY blueprints blueprints

ENV TZ=UTC \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    KEENYSPACE_FS__ROOT=/var/lib/keenyspace

RUN mkdir -p /var/lib/keenyspace && chown nonroot:nonroot /var/lib/keenyspace

USER nonroot

# 8000: API. 9100: internal Prometheus listener (KEENYSPACE_METRICS_PORT); do not publish it.
EXPOSE 8000 9100

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz',timeout=2).status==200 else 1)"]

CMD [".venv/bin/uvicorn", "keenyspace_server.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
