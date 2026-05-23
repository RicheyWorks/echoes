"""Structural tests for the MkDocs documentation site.

Validates mkdocs.yml and the docs/ source tree without building the site
or requiring mkdocs to be installed.

Covers:
  - mkdocs.yml exists and is valid YAML.
  - Required top-level keys are present (site_name, docs_dir, nav, theme).
  - Every path referenced in the nav exists on disk.
  - Key reference pages exist: CLI, API, Workflow YAML, Step Types.
  - Key deployment pages exist: linux, macos, windows, docker, ios, android.
  - Key operations pages exist: backup, restore, metrics, scale, readiness.
  - Home (index.md) exists.
  - Changelog page exists.
  - No nav entry references a file outside docs/.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

try:
    import tomllib as _toml  # noqa: F401 (unused, just checking availability)
except ModuleNotFoundError:
    pass

try:
    import yaml
except ModuleNotFoundError:
    yaml = None  # type: ignore[assignment]

ROOT    = Path(__file__).parent.parent
MKDOCS  = ROOT / "mkdocs.yml"
DOCS    = ROOT / "docs"


# ------------------------------------------------------------------ #
# helpers                                                             #
# ------------------------------------------------------------------ #

def _load_mkdocs() -> dict:
    if yaml is None:
        pytest.skip("pyyaml not installed — skipping mkdocs YAML parse tests")
    return yaml.safe_load(MKDOCS.read_text(encoding="utf-8"))


def _nav_paths(nav, acc=None):
    """Recursively collect all file paths referenced in the nav tree."""
    if acc is None:
        acc = []
    if isinstance(nav, list):
        for item in nav:
            _nav_paths(item, acc)
    elif isinstance(nav, dict):
        for key, val in nav.items():
            if isinstance(val, str):
                acc.append(val)
            else:
                _nav_paths(val, acc)
    return acc


# ------------------------------------------------------------------ #
# mkdocs.yml existence and structure                                  #
# ------------------------------------------------------------------ #

def test_mkdocs_yml_exists():
    assert MKDOCS.exists(), "mkdocs.yml not found at repo root"


def test_mkdocs_yml_parses():
    cfg = _load_mkdocs()
    assert isinstance(cfg, dict), "mkdocs.yml must parse to a dict"


def test_mkdocs_has_site_name():
    cfg = _load_mkdocs()
    assert "site_name" in cfg and cfg["site_name"], "site_name must be set"


def test_mkdocs_has_docs_dir():
    cfg = _load_mkdocs()
    assert "docs_dir" in cfg, "docs_dir must be declared"
    assert (ROOT / cfg["docs_dir"]).is_dir(), \
        f"docs_dir {cfg['docs_dir']!r} does not exist"


def test_mkdocs_has_nav():
    cfg = _load_mkdocs()
    assert "nav" in cfg and cfg["nav"], "nav must be declared and non-empty"


def test_mkdocs_has_theme():
    cfg = _load_mkdocs()
    assert "theme" in cfg, "theme must be declared"
    theme = cfg["theme"]
    name = theme if isinstance(theme, str) else theme.get("name", "")
    assert name, "theme.name must be set"


def test_mkdocs_theme_is_material():
    cfg = _load_mkdocs()
    theme = cfg.get("theme", {})
    name = theme if isinstance(theme, str) else theme.get("name", "")
    assert name == "material", \
        f"expected theme material, got {name!r}"


# ------------------------------------------------------------------ #
# nav paths all exist on disk                                         #
# ------------------------------------------------------------------ #

def test_all_nav_paths_exist():
    cfg = _load_mkdocs()
    docs_dir = ROOT / cfg.get("docs_dir", "docs")
    paths = _nav_paths(cfg.get("nav", []))
    missing = [p for p in paths if not (docs_dir / p).exists()]
    assert not missing, \
        f"nav references files that don't exist:\n" + "\n".join(f"  {p}" for p in missing)


def test_nav_paths_stay_inside_docs():
    cfg = _load_mkdocs()
    docs_dir = ROOT / cfg.get("docs_dir", "docs")
    paths = _nav_paths(cfg.get("nav", []))
    outside = [p for p in paths if ".." in p]
    assert not outside, \
        f"nav paths must not escape docs/: {outside}"


# ------------------------------------------------------------------ #
# required pages exist                                                #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("rel", [
    "index.md",
    "changelog.md",
])
def test_root_pages_exist(rel):
    assert (DOCS / rel).exists(), f"docs/{rel} is missing"


@pytest.mark.parametrize("rel", [
    "getting-started/install.md",
    "getting-started/quickstart.md",
    "getting-started/configuration.md",
])
def test_getting_started_pages_exist(rel):
    assert (DOCS / rel).exists(), f"docs/{rel} is missing"


@pytest.mark.parametrize("rel", [
    "deployment/overview.md",
    "deployment/linux.md",
    "deployment/macos.md",
    "deployment/windows.md",
    "deployment/docker.md",
    "deployment/ios.md",
    "deployment/android.md",
    "deployment/mesh.md",
    "deployment/tls.md",
])
def test_deployment_pages_exist(rel):
    assert (DOCS / rel).exists(), f"docs/{rel} is missing"


@pytest.mark.parametrize("rel", [
    "operations/backup.md",
    "operations/restore.md",
    "operations/scale.md",
    "operations/metrics.md",
    "operations/readiness.md",
])
def test_operations_pages_exist(rel):
    assert (DOCS / rel).exists(), f"docs/{rel} is missing"


@pytest.mark.parametrize("rel", [
    "reference/cli.md",
    "reference/api.md",
    "reference/workflow-yaml.md",
    "reference/step-types.md",
    "reference/templates.md",
])
def test_reference_pages_exist(rel):
    assert (DOCS / rel).exists(), f"docs/{rel} is missing"


# ------------------------------------------------------------------ #
# content spot checks                                                 #
# ------------------------------------------------------------------ #

def test_index_mentions_install():
    src = (DOCS / "index.md").read_text(encoding="utf-8")
    assert "pip install" in src or "Install" in src, \
        "index.md should mention how to install"


def test_cli_reference_covers_main_commands():
    src = (DOCS / "reference/cli.md").read_text(encoding="utf-8")
    for cmd in ("register", "trigger", "worker", "scheduler", "serve",
                "inspect", "cancel", "signal", "migrate", "backup"):
        assert cmd in src, f"CLI reference missing documentation for: {cmd}"


def test_api_reference_covers_main_routes():
    src = (DOCS / "reference/api.md").read_text(encoding="utf-8")
    for route in ("/healthz", "/metrics", "/api/runs", "/api/trigger",
                  "/api/signals", "/api/workflows"):
        assert route in src, f"API reference missing route: {route}"


def test_metrics_page_lists_all_families():
    src = (DOCS / "operations/metrics.md").read_text(encoding="utf-8")
    for metric in ("automaton_runs_total", "automaton_runs_active",
                   "automaton_queue_depth", "automaton_cron_triggers",
                   "automaton_db_size_bytes"):
        assert metric in src, f"metrics.md missing metric family: {metric}"


def test_workflow_yaml_covers_step_types():
    src = (DOCS / "reference/workflow-yaml.md").read_text(encoding="utf-8")
    for t in ("shell", "http_get", "file_append", "python", "wait_for_signal"):
        assert t in src, f"workflow-yaml.md missing step type: {t}"


def test_docker_page_mentions_compose():
    src = (DOCS / "deployment/docker.md").read_text(encoding="utf-8")
    assert "docker compose" in src or "docker-compose" in src, \
        "docker.md should mention docker compose"


def test_changelog_has_version_section():
    src = (DOCS / "changelog.md").read_text(encoding="utf-8")
    assert re.search(r"## \[\d+\.\d+\.\d+\]", src), \
        "changelog.md must contain at least one versioned section"
