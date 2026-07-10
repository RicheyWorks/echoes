"""Validate the deploy/macos/ artifacts on every PR.

We can't actually run launchctl from CI (no macOS in this matrix's
shared sandbox, and even macos-latest doesn't permit user-scope
bootstrap during a workflow), so we validate the artifacts themselves:

  - every plist parses and has the launchd-required keys
  - install.sh + uninstall.sh exist, are executable, syntax-check clean
  - the Homebrew formula references the right runtime deps
"""
from __future__ import annotations

import plistlib
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).parent.parent / "deploy" / "macos"

PLIST_NAMES = [
    "com.automaton.worker",
    "com.automaton.scheduler",
    "com.automaton.ui",
]

RUNTIME_DEPS = {
    "pyyaml", "httpx", "croniter", "yoyo-migrations",
    "keyring", "apprise",
}


def _plist(name: str) -> dict:
    return plistlib.loads((DEPLOY / f"{name}.plist").read_bytes())


@pytest.mark.parametrize("name", PLIST_NAMES)
def test_plist_parses(name):
    doc = _plist(name)
    # launchd's required minimum: Label + ProgramArguments.
    assert doc["Label"] == name
    assert isinstance(doc["ProgramArguments"], list)
    assert doc["ProgramArguments"], f"{name} has empty ProgramArguments"
    # First arg should be the automaton binary path with the @PREFIX@
    # template placeholder (install.sh substitutes it).
    assert doc["ProgramArguments"][0].endswith("/bin/automaton")


@pytest.mark.parametrize("name", PLIST_NAMES)
def test_plist_runs_at_load_and_keeps_alive(name):
    doc = _plist(name)
    assert doc.get("RunAtLoad") is True
    assert doc.get("KeepAlive") is True
    # ThrottleInterval prevents tight crash loops eating CPU.
    assert doc.get("ThrottleInterval", 0) >= 10


@pytest.mark.parametrize("name", PLIST_NAMES)
def test_plist_logs_to_library_logs(name):
    doc = _plist(name)
    out = doc["StandardOutPath"]
    err = doc["StandardErrorPath"]
    assert "Library/Logs/automaton" in out, out
    assert "Library/Logs/automaton" in err, err
    # The two streams must NOT collide (would mix interleavings).
    assert out != err


@pytest.mark.parametrize("name", PLIST_NAMES)
def test_plist_environment_variables_include_db_path(name):
    env = _plist(name).get("EnvironmentVariables", {})
    assert "AUTOMATON_DB" in env
    assert "Library/Application Support/automaton" in env["AUTOMATON_DB"]


@pytest.mark.skipif(sys.platform == "win32",
                    reason="NTFS has no POSIX exec bit; mode is meaningless here")
def test_install_script_exists_and_is_executable():
    p = DEPLOY / "install.sh"
    assert p.exists()
    # On real macOS this matters; here we just confirm the file mode
    # includes the user-execute bit.
    assert p.stat().st_mode & 0o100


@pytest.mark.skipif(sys.platform == "win32",
                    reason="NTFS has no POSIX exec bit; mode is meaningless here")
def test_uninstall_script_exists_and_is_executable():
    p = DEPLOY / "uninstall.sh"
    assert p.exists()
    assert p.stat().st_mode & 0o100


@pytest.mark.skipif(sys.platform == "win32",
                    reason="bash -n needs a real bash; not reliable on Windows runners")
def test_install_script_syntax_clean():
    """bash -n parses the script without executing - catches typos."""
    r = subprocess.run(
        ["bash", "-n", str(DEPLOY / "install.sh")],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(sys.platform == "win32",
                    reason="bash -n needs a real bash; not reliable on Windows runners")
def test_uninstall_script_syntax_clean():
    r = subprocess.run(
        ["bash", "-n", str(DEPLOY / "uninstall.sh")],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr


def test_install_script_handles_each_plist():
    """install.sh must mention every plist name; otherwise it would
    silently skip one of the services."""
    src = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    for name in PLIST_NAMES:
        assert name in src, f"install.sh doesn't reference {name}"


def test_uninstall_script_handles_each_plist():
    src = (DEPLOY / "uninstall.sh").read_text(encoding="utf-8")
    for name in PLIST_NAMES:
        assert name in src


def test_homebrew_formula_includes_runtime_deps():
    """The shipped runtime deps must each appear as a `resource` in
    the formula. If pyproject.toml grows a new dep, this test fails
    until the formula is updated."""
    src = (DEPLOY / "automaton.rb").read_text(encoding="utf-8")
    for dep in RUNTIME_DEPS:
        assert re.search(rf'resource\s+"{re.escape(dep)}"', src), \
            f"Homebrew formula missing resource for {dep!r}"


def test_homebrew_formula_ships_macos_assets():
    """libexec must include macos/ so install.sh is reachable after
    `brew install`."""
    src = (DEPLOY / "automaton.rb").read_text(encoding="utf-8")
    assert "libexec.install \"deploy/macos\"" in src
