"""Validate the deploy/windows/ artifacts on every PR.

We can't run PowerShell or register Windows services from CI without a
Windows runner doing it for real, so we validate the artifacts'
contents: each script declares the expected parameters, references
every service, and the env example holds the right Windows path
conventions. On the matrix's actual windows-latest job, the smoke
step in test.yml already exercises the CLI directly.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).parent.parent / "deploy" / "windows"

SERVICE_NAMES = ["automaton-worker", "automaton-scheduler", "automaton-ui"]


def _read(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def test_install_script_exists():
    assert (DEPLOY / "install.ps1").exists()


def test_uninstall_script_exists():
    assert (DEPLOY / "uninstall.ps1").exists()


def test_env_example_exists():
    assert (DEPLOY / "automaton.env.example").exists()


def test_readme_exists():
    assert (DEPLOY / "README.md").exists()


def test_install_references_every_service():
    src = _read("install.ps1")
    for name in SERVICE_NAMES:
        assert name in src, f"install.ps1 doesn't reference {name}"


def test_uninstall_references_every_service():
    src = _read("uninstall.ps1")
    for name in SERVICE_NAMES:
        assert name in src, f"uninstall.ps1 doesn't reference {name}"


def test_install_requires_administrator():
    """Both scripts must refuse to run unprivileged. Catches the regression
    where someone removes the elevation check and silently fails on a
    non-elevated PowerShell session."""
    src = _read("install.ps1")
    assert "Administrator" in src
    assert "WindowsBuiltInRole" in src


def test_uninstall_requires_administrator():
    src = _read("uninstall.ps1")
    assert "Administrator" in src
    assert "WindowsBuiltInRole" in src


def test_install_downloads_nssm():
    """We fetch NSSM from nssm.cc; the URL must be present + over HTTPS."""
    src = _read("install.ps1")
    assert "https://nssm.cc/release/nssm-" in src


def test_install_sets_auto_start_and_restart_delay():
    """Auto-start so services come up at boot; restart delay so a tight
    crash loop doesn't spin the CPU."""
    src = _read("install.ps1")
    assert "SERVICE_AUTO_START" in src
    assert "AppRestartDelay" in src


def test_install_pipes_stdout_and_stderr_to_log_files():
    src = _read("install.ps1")
    assert "AppStdout" in src
    assert "AppStderr" in src
    # And rotation enabled, so the logs don't grow without bound.
    assert "AppRotateFiles" in src


def test_install_depends_on_event_log():
    """Without DependOnService EventLog, services can fail to start at
    boot before the Event Log is available."""
    src = _read("install.ps1")
    assert "DependOnService" in src
    assert "EventLog" in src


def test_env_example_uses_windows_paths():
    """%APPDATA% and %ProgramData% are the Windows-native locations.
    Catches accidental Linux-style paths leaking into the template."""
    src = _read("automaton.env.example")
    assert "%APPDATA%" in src
    assert "%ProgramData%" in src
    assert "AUTOMATON_DB" in src


def test_env_example_documents_token_generation():
    src = _read("automaton.env.example")
    assert "secrets.token_urlsafe" in src


def test_powershell_param_blocks_present():
    """Both scripts use [CmdletBinding()] + param() so they accept named
    arguments cleanly. Catches accidental removal of the parameter section."""
    for s in ("install.ps1", "uninstall.ps1"):
        src = _read(s)
        assert "[CmdletBinding()]" in src, s
        assert re.search(r"^\s*param\(", src, re.MULTILINE), s


def test_uninstall_purge_flag_exists():
    """The optional -Purge switch is the only way to drop the DB; make
    sure it's still wired up."""
    src = _read("uninstall.ps1")
    assert "[switch]$Purge" in src
    assert "if ($Purge)" in src


def test_readme_mentions_setup_steps():
    """README must cover: install Python, run install.ps1 as admin,
    verify, uninstall, and the AV gotcha around NSSM."""
    src = _read("README.md")
    for needle in ["Python 3.10",
                   "install.ps1",
                   "Run as administrator",
                   "uninstall.ps1",
                   "NSSM",
                   "Event Viewer"]:
        assert needle in src, f"README missing reference to {needle!r}"
