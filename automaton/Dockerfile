# syntax=docker/dockerfile:1
# ---------------------------------------------------------------
# automaton — multi-stage Docker image
#
# Stage 1 (builder): installs the package + dependencies into a
#   wheel cache so the final stage needs no build tools.
# Stage 2 (runtime): slim Python image, non-root user, only what
#   the engine needs to run.
#
# Build:
#   docker build -t automaton:latest .
#
# The image does NOT bundle a database file. Mount a host directory
# or a named volume and point AUTOMATON_DB at it:
#   docker run -v automaton-data:/data \
#              -e AUTOMATON_DB=/data/automaton.db \
#              automaton:latest worker
# ---------------------------------------------------------------

# ---- builder -----------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies (needed for some wheels).
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy only the packaging manifests first to cache the dep-install
# layer independently from source changes.
COPY pyproject.toml ./
COPY automaton/__init__.py automaton/__init__.py
COPY automaton/migrations/ automaton/migrations/

# Install the package and all runtime deps into a prefix we can
# copy wholesale into the final stage.
RUN pip install --no-cache-dir --prefix=/install ".[tls,secrets-headless]" && \
    pip install --no-cache-dir --prefix=/install .

# ---- runtime -----------------------------------------------
FROM python:3.11-slim AS runtime

# Non-root user — never run the engine as root inside a container.
RUN useradd --system --create-home --uid 1000 automaton

# Copy installed packages from builder.
COPY --from=builder /install /usr/local

# Copy source (needed because we install in editable-compatible mode;
# the entry-point scripts reference the source tree).
WORKDIR /app
COPY --from=builder /build/ /app/
COPY automaton/ automaton/
COPY templates/ templates/
COPY pyproject.toml ./

# Re-install from the copied source so entry points resolve correctly.
RUN pip install --no-cache-dir --no-deps -e .

# Runtime defaults.
# Override AUTOMATON_DB to point at a persistent volume mount.
ENV AUTOMATON_DB=/data/automaton.db \
    AUTOMATON_LOG_FORMAT=json \
    AUTOMATON_LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1

# The data directory holds the SQLite file. Mount a volume here.
RUN mkdir /data && chown automaton:automaton /data

EXPOSE 8080

USER automaton

# Default: print help. Override CMD in compose or docker run.
# Common overrides:
#   worker     — automaton worker
#   scheduler  — automaton scheduler
#   ui         — automaton serve --host 0.0.0.0 --port 8080
CMD ["automaton", "--help"]
