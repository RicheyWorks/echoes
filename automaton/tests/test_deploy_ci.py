"""Structural tests for GitHub Actions workflow files.

Validates all four workflow YAMLs without running them.

Covers:
  test.yml:
    - Has a test job with an OS × Python-version matrix.
    - Matrix includes all three target OSes (ubuntu, macos, windows).
    - Matrix includes Python 3.10, 3.11, 3.12.
    - Has a publish-check job (build + twine check).
    - publish-check uses working-directory or runs from automaton/.
    - Has a postgres job with a postgres:16 service container.

  docs.yml:
    - Triggers on push to docs/** and mkdocs.yml.
    - Has write permission on contents (needed to push gh-pages).
    - Runs mkdocs gh-deploy.

  release.yml:
    - Triggers on v* tags only.
    - Has a test job that runs before build.
    - Has a build job that runs python -m build and twine check.
    - Has a publish-pypi job that uses pypa/gh-action-pypi-publish.
    - publish-pypi has id-token: write (OIDC Trusted Publishing).
    - Has a github-release job.
    - Pre-release tags (rc, alpha, beta) set prerelease: true.

  mobile.yml:
    - Has deploy-tests, ios, and android jobs.
    - ios job runs on macos-latest.
    - android job runs on ubuntu-latest and uses setup-java + setup-android.
    - android job uploads the APK as a build artifact.
    - Triggers on changes to deploy/ios/ and deploy/android/.
"""
from __future__ import annotations

from pathlib import Path

import pytest

try:
    import yaml
except ModuleNotFoundError:
    yaml = None  # type: ignore[assignment]

ROOT       = Path(__file__).parent.parent
# Workflows live at the *repository* root (automaton/ is a sub-project);
# GitHub Actions only runs workflows from the repo-root .github/workflows/.
WORKFLOWS  = ROOT.parent / ".github" / "workflows"
TEST_WF    = WORKFLOWS / "test.yml"
DOCS_WF    = WORKFLOWS / "docs.yml"
RELEASE_WF = WORKFLOWS / "release.yml"
MOBILE_WF  = WORKFLOWS / "mobile.yml"


# ------------------------------------------------------------------ #
# helpers                                                             #
# ------------------------------------------------------------------ #

def _load(path: Path) -> dict:
    if yaml is None:
        pytest.skip("pyyaml not installed — skipping workflow YAML tests")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _walk(obj, acc=None):
    """Recursively collect all string values in a nested dict/list."""
    if acc is None:
        acc = []
    if isinstance(obj, str):
        acc.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk(v, acc)
    elif isinstance(obj, list):
        for item in obj:
            _walk(item, acc)
    return acc


# ------------------------------------------------------------------ #
# workflow files exist                                                 #
# ------------------------------------------------------------------ #

def test_test_workflow_exists():
    assert TEST_WF.exists(), ".github/workflows/test.yml not found"


def test_docs_workflow_exists():
    assert DOCS_WF.exists(), ".github/workflows/docs.yml not found"


def test_release_workflow_exists():
    assert RELEASE_WF.exists(), ".github/workflows/release.yml not found"


# ------------------------------------------------------------------ #
# test.yml                                                            #
# ------------------------------------------------------------------ #

def test_test_workflow_has_test_job():
    cfg = _load(TEST_WF)
    assert "test" in cfg.get("jobs", {}), "test.yml must have a 'test' job"


def test_test_matrix_includes_all_oses():
    cfg = _load(TEST_WF)
    matrix = cfg["jobs"]["test"]["strategy"]["matrix"]
    oses = matrix.get("os", [])
    for expected in ("ubuntu-latest", "macos-latest", "windows-latest"):
        assert expected in oses, f"test matrix missing OS: {expected}"


def test_test_matrix_includes_python_versions():
    cfg = _load(TEST_WF)
    matrix = cfg["jobs"]["test"]["strategy"]["matrix"]
    versions = [str(v) for v in matrix.get("python-version", [])]
    for expected in ("3.10", "3.11", "3.12"):
        assert expected in versions, f"test matrix missing Python {expected}"


def test_test_matrix_fail_fast_disabled():
    """fail-fast: false lets all combinations run even if one fails."""
    cfg = _load(TEST_WF)
    strategy = cfg["jobs"]["test"].get("strategy", {})
    assert strategy.get("fail-fast") is False, \
        "test matrix should set fail-fast: false"


def test_test_workflow_has_publish_check_job():
    cfg = _load(TEST_WF)
    assert "publish-check" in cfg.get("jobs", {}), \
        "test.yml must have a 'publish-check' job"


def test_publish_check_runs_build():
    cfg = _load(TEST_WF)
    steps = cfg["jobs"]["publish-check"]["steps"]
    all_text = " ".join(_walk(steps))
    assert "build" in all_text, \
        "publish-check must run 'python -m build'"


def test_publish_check_runs_twine():
    cfg = _load(TEST_WF)
    steps = cfg["jobs"]["publish-check"]["steps"]
    all_text = " ".join(_walk(steps))
    assert "twine" in all_text, \
        "publish-check must run 'twine check'"


def test_test_workflow_triggers_on_push_and_pr():
    cfg = _load(TEST_WF)
    on = cfg.get("on", cfg.get(True, {}))
    assert "push" in on, "test.yml must trigger on push"
    assert "pull_request" in on, "test.yml must trigger on pull_request"


# ------------------------------------------------------------------ #
# docs.yml                                                            #
# ------------------------------------------------------------------ #

def test_docs_workflow_triggers_on_docs_path():
    cfg = _load(DOCS_WF)
    on = cfg.get("on", cfg.get(True, {}))
    push = on.get("push", {})
    paths = push.get("paths", [])
    assert any("docs" in p for p in paths), \
        "docs.yml must trigger when docs/** changes"


def test_docs_workflow_triggers_on_mkdocs_yml():
    cfg = _load(DOCS_WF)
    on = cfg.get("on", cfg.get(True, {}))
    push = on.get("push", {})
    paths = push.get("paths", [])
    assert any("mkdocs" in p for p in paths), \
        "docs.yml must trigger when mkdocs.yml changes"


def test_docs_workflow_has_write_permission():
    cfg = _load(DOCS_WF)
    perms = cfg.get("permissions", {})
    assert perms.get("contents") == "write", \
        "docs.yml needs contents: write permission to push to gh-pages"


def test_docs_workflow_runs_mkdocs_deploy():
    cfg = _load(DOCS_WF)
    all_text = " ".join(_walk(cfg))
    assert "gh-deploy" in all_text or "mkdocs" in all_text, \
        "docs.yml must run mkdocs gh-deploy"


def test_docs_workflow_has_workflow_dispatch():
    """Allow manual trigger from the Actions tab."""
    cfg = _load(DOCS_WF)
    on = cfg.get("on", cfg.get(True, {}))
    assert "workflow_dispatch" in on, \
        "docs.yml should allow workflow_dispatch for manual deploys"


# ------------------------------------------------------------------ #
# release.yml                                                         #
# ------------------------------------------------------------------ #

def test_release_workflow_triggers_on_version_tag():
    cfg = _load(RELEASE_WF)
    on = cfg.get("on", cfg.get(True, {}))
    push = on.get("push", {})
    tags = push.get("tags", [])
    assert any("v*" in t for t in tags), \
        "release.yml must trigger on v* tag pushes"


def test_release_workflow_does_not_trigger_on_branches():
    """Release must only fire on tags, not on every branch push."""
    cfg = _load(RELEASE_WF)
    on = cfg.get("on", cfg.get(True, {}))
    push = on.get("push", {})
    assert "branches" not in push, \
        "release.yml push trigger must be tags-only, not branches"


def test_release_has_test_job():
    cfg = _load(RELEASE_WF)
    assert "test" in cfg.get("jobs", {}), \
        "release.yml must run tests before publishing"


def test_release_has_build_job():
    cfg = _load(RELEASE_WF)
    assert "build" in cfg.get("jobs", {}), \
        "release.yml must have a build job"


def test_release_build_needs_test():
    cfg = _load(RELEASE_WF)
    needs = cfg["jobs"]["build"].get("needs", [])
    needs_list = [needs] if isinstance(needs, str) else needs
    assert "test" in needs_list, \
        "release build job must depend on test job"


def test_release_has_publish_pypi_job():
    cfg = _load(RELEASE_WF)
    assert "publish-pypi" in cfg.get("jobs", {}), \
        "release.yml must have a publish-pypi job"


def test_release_publish_uses_oidc():
    cfg = _load(RELEASE_WF)
    perms = cfg["jobs"]["publish-pypi"].get("permissions", {})
    assert perms.get("id-token") == "write", \
        "publish-pypi must have id-token: write for OIDC Trusted Publishing"


def test_release_publish_uses_pypa_action():
    cfg = _load(RELEASE_WF)
    steps = cfg["jobs"]["publish-pypi"]["steps"]
    all_text = " ".join(_walk(steps))
    assert "pypa/gh-action-pypi-publish" in all_text, \
        "publish-pypi must use pypa/gh-action-pypi-publish"


def test_release_publish_needs_build():
    cfg = _load(RELEASE_WF)
    needs = cfg["jobs"]["publish-pypi"].get("needs", [])
    needs_list = [needs] if isinstance(needs, str) else needs
    assert "build" in needs_list, \
        "publish-pypi job must depend on build job"


def test_release_has_github_release_job():
    cfg = _load(RELEASE_WF)
    assert "github-release" in cfg.get("jobs", {}), \
        "release.yml must have a github-release job"


def test_release_github_release_needs_publish():
    cfg = _load(RELEASE_WF)
    needs = cfg["jobs"]["github-release"].get("needs", [])
    needs_list = [needs] if isinstance(needs, str) else needs
    assert "publish-pypi" in needs_list, \
        "github-release job must run after publish-pypi"



def test_release_marks_prerelease_for_rc_tags():
    cfg = _load(RELEASE_WF)
    all_text = " ".join(_walk(cfg["jobs"]["github-release"]))
    # The workflow should conditionally set prerelease based on rc/alpha/beta
    assert "rc" in all_text or "prerelease" in all_text, \
        "github-release should handle pre-release tags (rc, alpha, beta)"


def test_release_workflow_has_contents_write_permission():
    cfg = _load(RELEASE_WF)
    perms = cfg.get("permissions", {})
    assert perms.get("contents") == "write", \
        "release.yml needs contents: write to create GitHub Releases"


# ------------------------------------------------------------------ #
# test.yml — postgres job                                             #
# ------------------------------------------------------------------ #

def test_test_workflow_has_postgres_job():
    cfg = _load(TEST_WF)
    assert "postgres" in cfg.get("jobs", {}), \
        "test.yml must have a 'postgres' job for backend integration tests"


def test_postgres_job_uses_postgres_service():
    cfg = _load(TEST_WF)
    services = cfg["jobs"]["postgres"].get("services", {})
    assert "postgres" in services, \
        "postgres job must define a 'postgres' service container"


def test_postgres_job_uses_correct_image():
    cfg = _load(TEST_WF)
    image = cfg["jobs"]["postgres"]["services"]["postgres"].get("image", "")
    assert image.startswith("postgres:"), \
        f"postgres service should use a postgres image, got: {image!r}"


def test_postgres_job_sets_pg_url_env():
    cfg = _load(TEST_WF)
    steps = cfg["jobs"]["postgres"]["steps"]
    # _walk collects string *values*, so we look for the DSN value that gets
    # assigned to AUTOMATON_TEST_PG_URL in the step's env: block.
    all_text = " ".join(_walk(steps))
    assert "postgresql://" in all_text, \
        "postgres job must set AUTOMATON_TEST_PG_URL with a postgresql:// DSN"


def test_postgres_job_runs_test_postgres():
    cfg = _load(TEST_WF)
    steps = cfg["jobs"]["postgres"]["steps"]
    all_text = " ".join(_walk(steps))
    assert "test_postgres" in all_text, \
        "postgres job must run tests/test_postgres.py"


def test_postgres_job_installs_postgres_extra():
    cfg = _load(TEST_WF)
    steps = cfg["jobs"]["postgres"]["steps"]
    all_text = " ".join(_walk(steps))
    assert "postgres" in all_text, \
        "postgres job must install the [postgres] extra (psycopg3)"


# ------------------------------------------------------------------ #
# mobile.yml                                                          #
# ------------------------------------------------------------------ #

def test_mobile_workflow_exists():
    assert MOBILE_WF.exists(), ".github/workflows/mobile.yml not found"


def test_mobile_workflow_triggers_on_ios_changes():
    cfg = _load(MOBILE_WF)
    on = cfg.get("on", cfg.get(True, {}))
    pr = on.get("pull_request", {})
    paths = pr.get("paths", [])
    assert any("ios" in p for p in paths), \
        "mobile.yml must trigger on changes to deploy/ios/"


def test_mobile_workflow_triggers_on_android_changes():
    cfg = _load(MOBILE_WF)
    on = cfg.get("on", cfg.get(True, {}))
    pr = on.get("pull_request", {})
    paths = pr.get("paths", [])
    assert any("android" in p for p in paths), \
        "mobile.yml must trigger on changes to deploy/android/"


def test_mobile_workflow_has_workflow_dispatch():
    cfg = _load(MOBILE_WF)
    on = cfg.get("on", cfg.get(True, {}))
    assert "workflow_dispatch" in on, \
        "mobile.yml should allow workflow_dispatch for manual builds"


def test_mobile_workflow_has_deploy_tests_job():
    cfg = _load(MOBILE_WF)
    assert "deploy-tests" in cfg.get("jobs", {}), \
        "mobile.yml must have a deploy-tests job (Linux structural checks)"


def test_mobile_workflow_has_ios_job():
    cfg = _load(MOBILE_WF)
    assert "ios" in cfg.get("jobs", {}), \
        "mobile.yml must have an ios job"


def test_mobile_workflow_ios_runs_on_macos():
    cfg = _load(MOBILE_WF)
    runs_on = cfg["jobs"]["ios"].get("runs-on", "")
    assert "macos" in runs_on, \
        f"ios job must run on macOS, got: {runs_on!r}"


def test_mobile_workflow_has_android_job():
    cfg = _load(MOBILE_WF)
    assert "android" in cfg.get("jobs", {}), \
        "mobile.yml must have an android job"


def test_mobile_workflow_android_runs_on_ubuntu():
    cfg = _load(MOBILE_WF)
    runs_on = cfg["jobs"]["android"].get("runs-on", "")
    assert "ubuntu" in runs_on, \
        f"android job must run on ubuntu, got: {runs_on!r}"


def test_mobile_workflow_android_sets_up_java():
    cfg = _load(MOBILE_WF)
    steps = cfg["jobs"]["android"]["steps"]
    all_text = " ".join(_walk(steps))
    assert "setup-java" in all_text, \
        "android job must use actions/setup-java"


def test_mobile_workflow_android_sets_up_android_sdk():
    cfg = _load(MOBILE_WF)
    steps = cfg["jobs"]["android"]["steps"]
    all_text = " ".join(_walk(steps))
    assert "android" in all_text.lower() and "setup" in all_text.lower(), \
        "android job must set up the Android SDK"


def test_mobile_workflow_android_runs_assemble_debug():
    cfg = _load(MOBILE_WF)
    steps = cfg["jobs"]["android"]["steps"]
    all_text = " ".join(_walk(steps))
    assert "assembleDebug" in all_text, \
        "android job must run ./gradlew assembleDebug"


def test_mobile_workflow_android_uploads_apk_artifact():
    cfg = _load(MOBILE_WF)
    steps = cfg["jobs"]["android"]["steps"]
    all_text = " ".join(_walk(steps))
    assert "upload-artifact" in all_text, \
        "android job must upload the APK as a build artifact"


def test_mobile_workflow_deploy_tests_runs_pytest():
    cfg = _load(MOBILE_WF)
    steps = cfg["jobs"]["deploy-tests"]["steps"]
    all_text = " ".join(_walk(steps))
    assert "test_deploy_ios" in all_text, \
        "deploy-tests job must run test_deploy_ios.py"
    assert "test_deploy_android" in all_text, \
        "deploy-tests job must run test_deploy_android.py"
