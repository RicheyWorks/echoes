"""Validate the iOS Swift sources on every PR.

The Linux CI runners can't build Swift; what we can do is sanity-check
the file structure + method names so the iOS app stays in sync with
the Python client and the server's HTTP surface. The actual `swift
build` should happen on a Mac.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent / "deploy" / "ios"
KIT = ROOT / "Sources" / "AutomatonKit"
APP = ROOT / "Sources" / "AutomatonApp"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ----- package layout ---------------------------------------------

def test_package_swift_exists_with_both_products():
    src = _read("Package.swift")
    assert ".library(name: \"AutomatonKit\"" in src
    assert ".executable(name: \"AutomatonApp\"" in src


def test_package_targets_ios17():
    src = _read("Package.swift")
    assert ".iOS(.v17)" in src


def test_required_swift_files_exist():
    expected = [
        "Sources/AutomatonKit/Models.swift",
        "Sources/AutomatonKit/AutomatonClient.swift",
        "Sources/AutomatonApp/AutomatonApp.swift",
        "Sources/AutomatonApp/Settings.swift",
        "Sources/AutomatonApp/Screens/RunsListView.swift",
        "Sources/AutomatonApp/Screens/RunDetailView.swift",
        "Sources/AutomatonApp/Screens/WorkflowsView.swift",
        "Sources/AutomatonApp/Screens/SettingsView.swift",
    ]
    for rel in expected:
        assert (ROOT / rel).exists(), rel


# ----- AutomatonClient surface stays aligned with Python --------

EXPECTED_CLIENT_METHODS = [
    "health",
    "runs",
    "runDetail",
    "workflows",
    "trigger",
    "signal",
    "cancel",
]


@pytest.mark.parametrize("method", EXPECTED_CLIENT_METHODS)
def test_client_method_present(method):
    src = _read("Sources/AutomatonKit/AutomatonClient.swift")
    # Swift method signatures look like `public func methodName(...)`.
    assert re.search(rf"\bfunc\s+{method}\s*\(", src), \
        f"AutomatonClient missing `{method}`"


def test_client_uses_async_await():
    src = _read("Sources/AutomatonKit/AutomatonClient.swift")
    # Every public network method should be `async throws`.
    for method in EXPECTED_CLIENT_METHODS:
        m = re.search(rf"\bfunc\s+{method}[^{{]+", src)
        assert m, f"{method} not found"
        assert "async throws" in m.group(0), \
            f"{method} should be async throws, got: {m.group(0).strip()}"


def test_client_pins_cert_when_fingerprint_set():
    src = _read("Sources/AutomatonKit/AutomatonClient.swift")
    assert "pinnedCertSHA256" in src
    assert "PinnedCertDelegate" in src
    assert "CryptoKit" in src
    assert "SHA256.hash" in src


def test_client_uses_snake_case_decoding():
    src = _read("Sources/AutomatonKit/AutomatonClient.swift")
    # The server emits snake_case JSON.
    assert "convertFromSnakeCase" in src


def test_run_status_enum_matches_server():
    src = _read("Sources/AutomatonKit/Models.swift")
    for status in ["pending", "running", "completed", "failed", "cancelled"]:
        assert f"case {status}" in src, f"RunStatus missing {status}"


# ----- SwiftUI screens exist as Views ------------------------------

@pytest.mark.parametrize("file,struct_name", [
    ("Screens/RunsListView.swift", "RunsListView"),
    ("Screens/RunDetailView.swift", "RunDetailView"),
    ("Screens/WorkflowsView.swift", "WorkflowsView"),
    ("Screens/SettingsView.swift", "SettingsView"),
])
def test_screen_declares_view(file, struct_name):
    src = _read(f"Sources/AutomatonApp/{file}")
    assert re.search(rf"struct\s+{struct_name}\s*:\s*View\b", src), \
        f"{file} doesn't declare `struct {struct_name}: View`"


# ----- Settings persists token in Keychain, not UserDefaults ------

def test_token_goes_to_keychain_not_user_defaults():
    src = _read("Sources/AutomatonApp/Settings.swift")
    assert "kSecClassGenericPassword" in src
    assert "SecItemAdd" in src
    # Sanity: token isn't being persisted via UserDefaults.
    assert "UserDefaults.standard.set(token" not in src


def test_app_entry_point_is_a_tab_view():
    src = _read("Sources/AutomatonApp/AutomatonApp.swift")
    assert "@main" in src
    assert "TabView" in src
    for tab in ("RunsListView()", "WorkflowsView()", "SettingsView()"):
        assert tab in src, f"Missing tab: {tab}"


def test_run_detail_polls_when_not_terminal():
    """RunDetailView should poll while the run is pending/running and
    stop on terminal status. Catches "we removed the polling" regressions."""
    src = _read("Sources/AutomatonApp/Screens/RunDetailView.swift")
    assert "Task.sleep" in src
    assert "isTerminal" in src


def test_no_implicit_force_unwrap_on_user_input():
    """A naive `URL(string: serverURL)!` would crash the app on a typo.
    `URL(string:)` returns an Optional; user-typed strings must go
    through guard let / if let, never a force-unwrap."""
    settings_view = _read("Sources/AutomatonApp/Screens/SettingsView.swift")
    settings_kit = _read("Sources/AutomatonApp/Settings.swift")
    combined = settings_view + settings_kit
    assert re.search(r"guard let url = URL\(string:", combined), \
        "Expected `guard let url = URL(string:` in makeClient"
    # The forbidden pattern is a force-unwrap of a user-supplied URL.
    assert "URL(string: serverURL)!" not in combined
    assert "URL(string: settings.serverURL)!" not in combined


def test_readme_describes_testflight_path():
    src = _read("README.md").lower()
    for needle in ["testflight",
                   "apple developer program",
                   "self-signed",
                   "push notifications"]:
        assert needle in src, f"README missing: {needle}"
