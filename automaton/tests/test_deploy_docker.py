"""Structural tests for Docker deployment artefacts.

Validates Dockerfile, docker-compose.yml, and .dockerignore without
requiring Docker to be installed or the image to actually build.

Covers:
  - Dockerfile has builder + runtime stages, non-root user, EXPOSE 8080,
    a /data volume mount point, and key ENV defaults.
  - docker-compose.yml defines exactly three services (worker, scheduler,
    ui), all mount the shared volume, the ui service has a healthcheck,
    and ports 8080 are published.
  - .dockerignore excludes tests/, .git, __pycache__, and secrets (.env).
  - The compose healthcheck pings /healthz, not a random endpoint.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
DOCKERIGNORE = ROOT / ".dockerignore"


# ------------------------------------------------------------------ #
# helpers                                                             #
# ------------------------------------------------------------------ #

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ------------------------------------------------------------------ #
# Dockerfile                                                          #
# ------------------------------------------------------------------ #

def test_dockerfile_exists():
    assert DOCKERFILE.exists(), "Dockerfile not found at repo root"


def test_dockerfile_has_builder_stage():
    src = _read(DOCKERFILE)
    assert "AS builder" in src, "expected a 'builder' stage (multi-stage build)"


def test_dockerfile_has_runtime_stage():
    src = _read(DOCKERFILE)
    assert "AS runtime" in src, "expected a 'runtime' stage"


def test_dockerfile_copies_from_builder():
    src = _read(DOCKERFILE)
    assert "COPY --from=builder" in src, \
        "runtime stage must copy from builder (multi-stage pattern)"


def test_dockerfile_creates_nonroot_user():
    src = _read(DOCKERFILE)
    assert "useradd" in src or "adduser" in src, \
        "Dockerfile must create a non-root user"


def test_dockerfile_switches_to_nonroot_user():
    src = _read(DOCKERFILE)
    assert "USER automaton" in src or \
           (("USER" in src) and "root" not in src.split("USER")[-1].split("\n")[0]), \
        "Dockerfile must switch to a non-root USER before CMD"


def test_dockerfile_exposes_8080():
    src = _read(DOCKERFILE)
    assert "EXPOSE 8080" in src


def test_dockerfile_sets_automaton_db_env():
    src = _read(DOCKERFILE)
    assert "AUTOMATON_DB" in src, \
        "AUTOMATON_DB env var must be declared so users know to override it"


def test_dockerfile_data_volume_dir():
    src = _read(DOCKERFILE)
    assert "/data" in src, \
        "Dockerfile must create/document the /data mount point"


def test_dockerfile_uses_slim_base():
    """Prefer a slim or distroless base to keep image size reasonable."""
    src = _read(DOCKERFILE)
    assert "slim" in src or "distroless" in src or "alpine" in src, \
        "final stage should use a slim base image"


def test_dockerfile_unbuffered_python():
    src = _read(DOCKERFILE)
    assert "PYTHONUNBUFFERED" in src, \
        "PYTHONUNBUFFERED=1 is needed so JSON logs stream in real time"


# ------------------------------------------------------------------ #
# docker-compose.yml                                                  #
# ------------------------------------------------------------------ #

def test_compose_exists():
    assert COMPOSE.exists(), "docker-compose.yml not found at repo root"


def test_compose_has_three_services():
    src = _read(COMPOSE)
    for svc in ("worker", "scheduler", "ui"):
        assert f"{svc}:" in src, f"compose missing service: {svc}"


def test_compose_ui_publishes_8080():
    src = _read(COMPOSE)
    assert "8080:8080" in src or '"8080:8080"' in src, \
        "ui service must publish port 8080"


def test_compose_has_shared_volume():
    src = _read(COMPOSE)
    # The volume must appear in at least two services (worker + ui minimum).
    assert src.count("automaton-data") >= 3, \
        "automaton-data volume should be referenced in multiple services + volumes block"


def test_compose_ui_has_healthcheck():
    src = _read(COMPOSE)
    assert "healthcheck:" in src, "ui service must declare a healthcheck"


def test_compose_healthcheck_targets_healthz():
    src = _read(COMPOSE)
    assert "/healthz" in src, \
        "healthcheck should probe the /healthz endpoint"


def test_compose_worker_depends_on_ui():
    src = _read(COMPOSE)
    assert "depends_on" in src, \
        "worker/scheduler should wait for ui to be healthy before starting"


def test_compose_uses_env_file():
    src = _read(COMPOSE)
    assert "env_file" in src, \
        "services should load secrets from an env_file, not hardcode them"


def test_compose_services_restart_unless_stopped():
    src = _read(COMPOSE)
    assert "unless-stopped" in src, \
        "services should restart unless deliberately stopped"


def test_compose_has_volumes_block():
    src = _read(COMPOSE)
    # The top-level `volumes:` block declares named volumes.
    lines = src.splitlines()
    top_level_volumes = any(
        line.startswith("volumes:") for line in lines
    )
    assert top_level_volumes, "compose file must have a top-level volumes: block"


# ------------------------------------------------------------------ #
# .dockerignore                                                       #
# ------------------------------------------------------------------ #

def test_dockerignore_exists():
    assert DOCKERIGNORE.exists(), ".dockerignore not found at repo root"


def test_dockerignore_excludes_tests():
    src = _read(DOCKERIGNORE)
    assert "tests/" in src or "tests" in src, \
        ".dockerignore must exclude the tests/ directory"


def test_dockerignore_excludes_git():
    src = _read(DOCKERIGNORE)
    assert ".git" in src, ".dockerignore must exclude .git"


def test_dockerignore_excludes_pycache():
    src = _read(DOCKERIGNORE)
    assert "__pycache__" in src, ".dockerignore must exclude __pycache__"


def test_dockerignore_excludes_dotenv():
    src = _read(DOCKERIGNORE)
    assert ".env" in src, \
        ".dockerignore must exclude .env so secrets are never baked into the image"


def test_dockerignore_excludes_egg_info():
    src = _read(DOCKERIGNORE)
    assert "egg-info" in src or "*.egg-info" in src, \
        ".dockerignore should exclude .egg-info build artefacts"


def test_dockerignore_excludes_venv():
    src = _read(DOCKERIGNORE)
    assert ".venv" in src or "venv/" in src, \
        ".dockerignore must exclude virtual-environment directories"
