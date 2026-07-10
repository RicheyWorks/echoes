"""Structural tests for pyproject.toml and packaging artefacts.

Ensures the package metadata required for a clean PyPI publish is
present and internally consistent. Runs without installing the package
or touching the network.

Covers:
  - Distribution name is automaton-engine (automaton is taken on PyPI).
  - Version string is present and semver-shaped.
  - Required metadata fields: description, readme, license, authors,
    keywords, classifiers, requires-python, project URLs.
  - Python version classifiers match requires-python lower bound.
  - Entry-point script declared (`automaton`).
  - Package data includes migrations/*.sql.
  - py.typed marker exists (PEP 561).
  - CHANGELOG.md exists and follows keep-a-changelog conventions.
  - README.md referenced in pyproject exists on disk.
"""
from __future__ import annotations

import re
try:
    import tomllib
except ModuleNotFoundError:          # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]
from pathlib import Path

ROOT = Path(__file__).parent.parent
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"


# ------------------------------------------------------------------ #
# helpers                                                             #
# ------------------------------------------------------------------ #

def _load() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ #
# pyproject.toml — identity                                           #
# ------------------------------------------------------------------ #

def test_distribution_name_is_automaton_engine():
    """PyPI name must be automaton-engine; plain 'automaton' is taken."""
    cfg = _load()
    assert cfg["project"]["name"] == "automaton-engine", (
        "Distribution name must be 'automaton-engine' — 'automaton' is "
        "already occupied on PyPI."
    )


def test_dunder_version_matches_pyproject():
    """__version__ drifted to 0.1.0 once; pin it to pyproject forever."""
    import automaton
    cfg = _load()
    assert automaton.__version__ == cfg["project"]["version"]


def test_version_is_semver():
    cfg = _load()
    version = cfg["project"]["version"]
    assert re.match(r"^\d+\.\d+\.\d+", version), \
        f"version {version!r} is not semver-shaped"


def test_description_present():
    cfg = _load()
    desc = cfg["project"].get("description", "")
    assert desc and len(desc) > 10, "description is missing or too short"


def test_readme_field_present():
    cfg = _load()
    assert "readme" in cfg["project"], "readme field required for PyPI long description"


def test_readme_file_exists():
    cfg = _load()
    readme = cfg["project"]["readme"]
    # readme can be a string path or a dict with {file: ...}
    path = readme if isinstance(readme, str) else readme.get("file", "")
    assert (ROOT / path).exists(), f"readme file {path!r} not found"


def test_license_present():
    cfg = _load()
    assert "license" in cfg["project"], "license field required"


def test_authors_present():
    cfg = _load()
    authors = cfg["project"].get("authors", [])
    assert authors, "authors list must not be empty"
    assert all("name" in a for a in authors), "each author needs a 'name'"


def test_keywords_present():
    cfg = _load()
    kw = cfg["project"].get("keywords", [])
    assert len(kw) >= 3, "at least 3 keywords expected for discoverability"


# ------------------------------------------------------------------ #
# classifiers                                                         #
# ------------------------------------------------------------------ #

def test_classifiers_include_license():
    cfg = _load()
    classifiers = cfg["project"].get("classifiers", [])
    assert any("License" in c for c in classifiers), \
        "classifiers must include a License entry"


def test_classifiers_include_programming_language():
    cfg = _load()
    classifiers = cfg["project"].get("classifiers", [])
    assert any("Programming Language :: Python :: 3" in c for c in classifiers)


def test_classifiers_include_development_status():
    cfg = _load()
    classifiers = cfg["project"].get("classifiers", [])
    assert any("Development Status" in c for c in classifiers), \
        "classifiers must include a Development Status entry"


def test_python_version_classifiers_match_requires():
    """Every Python :: 3.X classifier must be >= requires-python floor."""
    cfg = _load()
    requires = cfg["project"].get("requires-python", ">=3.10")
    # Extract the minimum minor version from ">=3.X"
    m = re.search(r"3\.(\d+)", requires)
    min_minor = int(m.group(1)) if m else 10

    classifiers = cfg["project"].get("classifiers", [])
    py_classifiers = [c for c in classifiers if "Python :: 3." in c]
    for c in py_classifiers:
        minor_m = re.search(r"Python :: 3\.(\d+)", c)
        if minor_m:
            assert int(minor_m.group(1)) >= min_minor, \
                f"classifier {c!r} is below requires-python floor 3.{min_minor}"


# ------------------------------------------------------------------ #
# project URLs                                                        #
# ------------------------------------------------------------------ #

def test_project_urls_present():
    cfg = _load()
    urls = cfg["project"].get("urls", {})
    assert urls, "project.urls must be declared for PyPI"


def test_changelog_url_present():
    cfg = _load()
    urls = cfg["project"].get("urls", {})
    assert any("changelog" in k.lower() for k in urls), \
        "a Changelog URL should be listed in project.urls"


def test_repository_url_present():
    cfg = _load()
    urls = cfg["project"].get("urls", {})
    has_repo = any(
        k.lower() in ("repository", "source", "homepage") for k in urls
    )
    assert has_repo, "a Repository or Homepage URL should be listed"


# ------------------------------------------------------------------ #
# entry points & scripts                                              #
# ------------------------------------------------------------------ #

def test_automaton_script_declared():
    cfg = _load()
    scripts = cfg["project"].get("scripts", {})
    assert "automaton" in scripts, \
        "the 'automaton' CLI entry point must be declared under [project.scripts]"


def test_script_points_to_cli_main():
    cfg = _load()
    target = cfg["project"]["scripts"]["automaton"]
    assert "automaton.cli" in target and "main" in target, \
        f"automaton script should point at automaton.cli:main, got {target!r}"


def test_step_types_entry_point_section_exists():
    cfg = _load()
    ep = cfg.get("project", {}).get("entry-points", {})
    assert "automaton.step_types" in ep, \
        "automaton.step_types entry-point group must be declared so external plugins work"


# ------------------------------------------------------------------ #
# package data                                                        #
# ------------------------------------------------------------------ #

def test_migrations_sql_included_in_package_data():
    cfg = _load()
    pkg_data = cfg.get("tool", {}).get("setuptools", {}).get("package-data", {})
    automaton_data = pkg_data.get("automaton", [])
    assert any("migrations" in entry for entry in automaton_data), \
        "migrations/*.sql must be listed in package-data so they ship with the wheel"


def test_py_typed_marker_exists():
    """PEP 561: py.typed must be present for type checkers to use inline types."""
    marker = ROOT / "automaton" / "py.typed"
    assert marker.exists(), "automaton/py.typed is missing (PEP 561)"


def test_py_typed_in_package_data():
    cfg = _load()
    pkg_data = cfg.get("tool", {}).get("setuptools", {}).get("package-data", {})
    automaton_data = pkg_data.get("automaton", [])
    assert "py.typed" in automaton_data, \
        "py.typed must be listed in package-data so it ships in the wheel"


# ------------------------------------------------------------------ #
# CHANGELOG.md                                                        #
# ------------------------------------------------------------------ #

def test_changelog_exists():
    assert CHANGELOG.exists(), "CHANGELOG.md not found at repo root"


def test_changelog_has_unreleased_section():
    src = CHANGELOG.read_text(encoding="utf-8")
    assert "## [Unreleased]" in src, \
        "CHANGELOG must have an [Unreleased] section per keep-a-changelog"


def test_changelog_has_version_entries():
    src = CHANGELOG.read_text(encoding="utf-8")
    # Expect at least two versioned entries like ## [0.x.y]
    versions = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", src, re.MULTILINE)
    assert len(versions) >= 2, \
        f"CHANGELOG should have at least 2 version entries, found: {versions}"


def test_changelog_version_matches_pyproject():
    """Latest changelog version should match pyproject version."""
    cfg = _load()
    pyproject_ver = cfg["project"]["version"]
    src = CHANGELOG.read_text(encoding="utf-8")
    versions = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", src, re.MULTILINE)
    assert versions, "no version entries found in CHANGELOG"
    assert versions[0] == pyproject_ver, (
        f"Latest CHANGELOG version {versions[0]!r} does not match "
        f"pyproject.toml version {pyproject_ver!r}"
    )


def test_changelog_has_comparison_links():
    """Keep-a-changelog convention: comparison links at the bottom."""
    src = CHANGELOG.read_text(encoding="utf-8")
def test_changelog_has_comparison_links():
    """Keep-a-changelog convention: comparison links at the bottom."""
    src = CHANGELOG.read_text(encoding="utf-8")
    assert "compare/" in src or "releases/tag/" in src, \
        "CHANGELOG should have version comparison links at the bottom"
